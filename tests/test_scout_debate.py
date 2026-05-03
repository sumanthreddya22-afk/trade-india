"""Phase B — Scout Debate tests.

Covers:
  * Predicate / daily-cap gating
  * New-candidate selection (threshold, batch limit, scout_verdict IS NULL)
  * apply_verdicts mutates intel_candidates correctly (elevate/dismiss)
  * write_audit_rows persists the right shape
  * Pool filter respects scout_dismissed_until TTL
  * SEC 8-K override clears active dismissals
  * Sequential 3-call run_scout_debate happy path (place + skip + drop bad symbol)
  * Fail-soft: no creds / SDK error / unstructured judge / schema mismatch
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.intel import pool, scout_debate
from trading_bot.intel.scout_debate import ScoutVerdict
from trading_bot.state_db import (
    Base, IntelCandidate, IntelEvent, ScoutDebateRun, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


def _seed_candidate(
    engine, *, symbol: str, score: float = 5.0, asset_class: str = "stock",
    scout_verdict: str | None = None, scout_dismissed_until: dt.datetime | None = None,
    sources_json: str = '{"alpaca_news": 2, "polygon_news": 1}',
    top_reason: str = "Test catalyst", sentiment_avg: float | None = 0.4,
):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(IntelCandidate(
            symbol=symbol, asset_class=asset_class,
            score=score, n_mentions=3, n_sources=2,
            first_seen=now, last_seen=now, top_reason=top_reason,
            sources_json=sources_json, sentiment_avg=sentiment_avg,
            rolled_up_at=now,
            scout_verdict=scout_verdict,
            scout_dismissed_until=scout_dismissed_until,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# Predicate / daily cap
# ---------------------------------------------------------------------------


def test_should_scout_debate_under_cap():
    assert scout_debate.should_scout_debate(daily_debate_count=0, daily_cap=10) is True
    assert scout_debate.should_scout_debate(daily_debate_count=9, daily_cap=10) is True


def test_should_scout_debate_at_cap():
    assert scout_debate.should_scout_debate(daily_debate_count=10, daily_cap=10) is False


def test_should_scout_debate_zero_cap_disables():
    assert scout_debate.should_scout_debate(daily_debate_count=0, daily_cap=0) is False


def test_count_todays_scout_debates_returns_zero_on_empty(engine):
    assert scout_debate.count_todays_scout_debates(engine) == 0


def test_count_todays_scout_debates_counts_today_only(engine):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    yesterday = now - dt.timedelta(days=2)
    with Session(engine) as s:
        s.add_all([
            ScoutDebateRun(
                run_at=now, asset_class="stock", symbol="A",
                verdict="elevate", confidence="high",
            ),
            ScoutDebateRun(
                run_at=yesterday, asset_class="stock", symbol="B",
                verdict="dismiss", confidence="medium",
            ),
        ])
        s.commit()
    assert scout_debate.count_todays_scout_debates(engine) == 1


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def test_new_candidates_for_debate_filters_threshold(engine):
    _seed_candidate(engine, symbol="HIGH", score=6.0)
    _seed_candidate(engine, symbol="MID", score=4.0)
    _seed_candidate(engine, symbol="LOW", score=1.0)
    out = scout_debate._new_candidates_for_debate(engine, threshold=3.0, batch_limit=10)
    syms = {c.symbol for c in out}
    assert syms == {"HIGH", "MID"}


def test_new_candidates_skips_already_debated(engine):
    """scout_verdict IS NOT NULL → skip (already debated)."""
    _seed_candidate(engine, symbol="DEBATED", score=10.0, scout_verdict="elevate")
    _seed_candidate(engine, symbol="NEW", score=5.0, scout_verdict=None)
    out = scout_debate._new_candidates_for_debate(engine, threshold=3.0, batch_limit=10)
    syms = {c.symbol for c in out}
    assert syms == {"NEW"}


def test_new_candidates_orders_by_score_and_caps_batch(engine):
    for i, sc in enumerate([8.0, 4.0, 6.0, 5.0, 7.0]):
        _seed_candidate(engine, symbol=f"S{i}", score=sc)
    out = scout_debate._new_candidates_for_debate(engine, threshold=3.0, batch_limit=3)
    assert len(out) == 3
    # Top 3 by score: 8.0 (S0), 7.0 (S4), 6.0 (S2)
    assert [c.symbol for c in out] == ["S0", "S4", "S2"]


# ---------------------------------------------------------------------------
# apply_verdicts
# ---------------------------------------------------------------------------


def test_apply_verdicts_elevate_multiplies_score(engine):
    _seed_candidate(engine, symbol="UP", score=4.0)
    summary = scout_debate.apply_verdicts(
        engine,
        verdicts=[ScoutVerdict(symbol="UP", verdict="elevate", confidence="high", reason="r")],
        elevate_boost=1.5,
    )
    assert summary["elevated"] == 1
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.query(IntelCandidate).filter(IntelCandidate.symbol == "UP").first()
    assert row.score == pytest.approx(6.0)
    assert row.scout_verdict == "elevate"
    assert row.scout_dismissed_until is None


def test_apply_verdicts_dismiss_sets_ttl(engine):
    _seed_candidate(engine, symbol="DOWN", score=4.0)
    now = dt.datetime(2026, 5, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
    summary = scout_debate.apply_verdicts(
        engine,
        verdicts=[ScoutVerdict(symbol="DOWN", verdict="dismiss", confidence="high", reason="r")],
        dismiss_ttl_hours=24, now=now,
    )
    assert summary["dismissed"] == 1
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.query(IntelCandidate).filter(IntelCandidate.symbol == "DOWN").first()
    # Score unchanged on dismiss
    assert row.score == pytest.approx(4.0)
    assert row.scout_verdict == "dismiss"
    expected_until = now + dt.timedelta(hours=24)
    actual_until = row.scout_dismissed_until
    if actual_until.tzinfo is None:
        actual_until = actual_until.replace(tzinfo=dt.timezone.utc)
    assert actual_until == expected_until


def test_apply_verdicts_counts_missing_rows(engine):
    summary = scout_debate.apply_verdicts(
        engine,
        verdicts=[ScoutVerdict(symbol="GHOST", verdict="elevate", confidence="low", reason="r")],
    )
    assert summary["missing_rows"] == 1
    assert summary["elevated"] == 0


# ---------------------------------------------------------------------------
# write_audit_rows
# ---------------------------------------------------------------------------


def test_write_audit_rows_persists_per_verdict(engine):
    _seed_candidate(engine, symbol="A", score=5.0, top_reason="A reason")
    _seed_candidate(engine, symbol="B", score=4.0, top_reason="B reason")
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        cands = s.query(IntelCandidate).all()
        for c in cands:
            s.expunge(c)
    n = scout_debate.write_audit_rows(
        engine,
        verdicts=[
            ScoutVerdict(symbol="A", verdict="elevate", confidence="high", reason="catalyst confirmed"),
            ScoutVerdict(symbol="B", verdict="dismiss", confidence="medium", reason="single-source"),
        ],
        candidates=cands,
        skeptic_text="skeptic notes",
        analyst_text="analyst notes",
        prompt_version="test-v1",
    )
    assert n == 2
    with Session(engine) as s:
        rows = s.query(ScoutDebateRun).order_by(ScoutDebateRun.symbol).all()
    assert {r.symbol for r in rows} == {"A", "B"}
    a_row = next(r for r in rows if r.symbol == "A")
    assert a_row.verdict == "elevate"
    assert a_row.judge_reason == "catalyst confirmed"
    assert a_row.skeptic_text == "skeptic notes"
    assert a_row.analyst_text == "analyst notes"
    assert a_row.prompt_version == "test-v1"


# ---------------------------------------------------------------------------
# Pool filter respects scout_dismissed_until
# ---------------------------------------------------------------------------


def test_pool_lookup_hides_dismissed(engine):
    now = dt.datetime.now(dt.timezone.utc)
    future = now + dt.timedelta(hours=12)
    _seed_candidate(engine, symbol="DISMISSED", score=10.0,
                    scout_verdict="dismiss", scout_dismissed_until=future)
    out = pool.lookup(engine, "DISMISSED", "stock")
    assert out is None


def test_pool_lookup_surfaces_after_ttl_expires(engine):
    now = dt.datetime.now(dt.timezone.utc)
    expired = now - dt.timedelta(minutes=1)
    _seed_candidate(engine, symbol="EXPIRED", score=10.0,
                    scout_verdict="dismiss", scout_dismissed_until=expired)
    out = pool.lookup(engine, "EXPIRED", "stock")
    assert out is not None


def test_pool_lookup_respect_dismissal_can_be_disabled(engine):
    """Override path needs to see dismissed rows."""
    now = dt.datetime.now(dt.timezone.utc)
    future = now + dt.timedelta(hours=12)
    _seed_candidate(engine, symbol="DISMISSED", score=10.0,
                    scout_verdict="dismiss", scout_dismissed_until=future)
    out = pool.lookup(engine, "DISMISSED", "stock", respect_scout_dismissal=False)
    assert out is not None
    assert out.scout_verdict == "dismiss"


def test_pool_top_for_asset_class_hides_dismissed(engine):
    now = dt.datetime.now(dt.timezone.utc)
    future = now + dt.timedelta(hours=12)
    _seed_candidate(engine, symbol="OK", score=8.0)
    _seed_candidate(engine, symbol="HIDDEN", score=10.0,
                    scout_verdict="dismiss", scout_dismissed_until=future)
    out = pool.top_for_asset_class(engine, "stock", n=10)
    syms = {e.symbol for e in out}
    assert syms == {"OK"}


# ---------------------------------------------------------------------------
# SEC 8-K override
# ---------------------------------------------------------------------------


def test_override_dismissals_clears_when_recent_8k(engine):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    future = now + dt.timedelta(hours=12)
    _seed_candidate(engine, symbol="NVDA", score=4.0,
                    scout_verdict="dismiss", scout_dismissed_until=future)
    # Add a fresh sec_8k event
    with Session(engine) as s:
        s.add(IntelEvent(
            symbol="NVDA", asset_class="stock", source="sec_8k",
            headline="Q3 results", url="https://x/8k",
            ingested_at=now, event_at=now,
            event_hash="abc123",
        ))
        s.commit()
    summary = scout_debate.override_dismissals_for_sec_8k(engine, lookback_minutes=60)
    assert "NVDA" in summary["overrode"]
    out = pool.lookup(engine, "NVDA", "stock")
    assert out is not None
    assert out.scout_verdict is None


def test_override_dismissals_ignores_old_8k(engine):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    future = now + dt.timedelta(hours=12)
    _seed_candidate(engine, symbol="NVDA", score=4.0,
                    scout_verdict="dismiss", scout_dismissed_until=future)
    old = now - dt.timedelta(hours=3)
    with Session(engine) as s:
        s.add(IntelEvent(
            symbol="NVDA", asset_class="stock", source="sec_8k",
            headline="Old filing", ingested_at=old, event_at=old,
            event_hash="oldhash",
        ))
        s.commit()
    summary = scout_debate.override_dismissals_for_sec_8k(engine, lookback_minutes=60)
    assert summary["n_overrode"] == 0
    out = pool.lookup(engine, "NVDA", "stock")
    assert out is None  # still dismissed


def test_override_dismissals_skips_already_expired(engine):
    """If TTL already expired, no override needed (and we don't bump)."""
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    expired = now - dt.timedelta(minutes=10)
    _seed_candidate(engine, symbol="NVDA", score=4.0,
                    scout_verdict="dismiss", scout_dismissed_until=expired)
    with Session(engine) as s:
        s.add(IntelEvent(
            symbol="NVDA", asset_class="stock", source="sec_8k",
            headline="Fresh filing", ingested_at=now, event_at=now,
            event_hash="freshhash",
        ))
        s.commit()
    summary = scout_debate.override_dismissals_for_sec_8k(engine, lookback_minutes=60)
    assert summary["n_overrode"] == 0


# ---------------------------------------------------------------------------
# run_scout_debate — happy path + fail-soft
# ---------------------------------------------------------------------------


def _fake_response(text: str = "", structured_data: dict | None = None,
                   used_structured: bool = False):
    r = MagicMock()
    r.text = text
    r.data = structured_data
    r.used_structured = used_structured
    return r


@pytest.fixture
def fake_mailbox_client():
    """Patch MailboxBackedClient to a controllable mock for the duration of test."""
    with patch("trading_bot.intel.scout_debate.MailboxBackedClient") as ctor:
        client = MagicMock()
        ctor.return_value = client
        yield client


def test_run_scout_debate_no_candidates_returns_skipped(engine, fake_mailbox_client):
    out = scout_debate.run_scout_debate(engine, threshold=3.0, batch_limit=5)
    assert out.verdicts == []
    assert "no new" in out.skipped_reason


def test_run_scout_debate_no_creds_returns_skipped(engine):
    _seed_candidate(engine, symbol="A", score=5.0)
    from trading_bot.anthropic_client import AnthropicCredsMissingError
    with patch(
        "trading_bot.intel.scout_debate.MailboxBackedClient",
        side_effect=AnthropicCredsMissingError("no key"),
    ):
        out = scout_debate.run_scout_debate(engine, threshold=3.0)
    assert out.verdicts == []
    assert out.skipped_reason == "no anthropic creds"


def test_run_scout_debate_sdk_error_fails_soft(engine, fake_mailbox_client):
    _seed_candidate(engine, symbol="A", score=5.0)
    fake_mailbox_client.complete.side_effect = ConnectionError("dns")
    out = scout_debate.run_scout_debate(engine, threshold=3.0)
    assert out.verdicts == []
    assert "sdk error" in out.skipped_reason
    # State unchanged
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.query(IntelCandidate).first()
    assert row.score == pytest.approx(5.0)
    assert row.scout_verdict is None


def test_run_scout_debate_unstructured_judge_fails_soft(engine, fake_mailbox_client):
    _seed_candidate(engine, symbol="A", score=5.0)
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="skeptic"),
        _fake_response(text="analyst"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        text="judge free text", structured_data=None, used_structured=False,
    )
    out = scout_debate.run_scout_debate(engine, threshold=3.0)
    assert out.verdicts == []
    assert "unstructured" in out.skipped_reason


def test_run_scout_debate_happy_path_applies_verdicts(engine, fake_mailbox_client):
    _seed_candidate(engine, symbol="ALPHA", score=5.0)
    _seed_candidate(engine, symbol="BETA", score=4.0)
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="skeptic notes ..."),
        _fake_response(text="analyst notes ..."),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={
            "verdicts": [
                {"symbol": "ALPHA", "verdict": "elevate", "confidence": "high",
                 "reason": "8-K + cross-source confirmation"},
                {"symbol": "BETA", "verdict": "dismiss", "confidence": "medium",
                 "reason": "single-source noise"},
            ],
        },
        used_structured=True,
    )
    out = scout_debate.run_scout_debate(engine, threshold=3.0, elevate_boost=2.0)
    assert len(out.verdicts) == 2
    syms = {v.symbol: v.verdict for v in out.verdicts}
    assert syms == {"ALPHA": "elevate", "BETA": "dismiss"}
    # Verify state mutations
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        alpha = s.query(IntelCandidate).filter(IntelCandidate.symbol == "ALPHA").first()
        beta = s.query(IntelCandidate).filter(IntelCandidate.symbol == "BETA").first()
        rows = s.query(ScoutDebateRun).all()
    assert alpha.score == pytest.approx(10.0)  # 5.0 * 2.0 boost
    assert alpha.scout_verdict == "elevate"
    assert beta.scout_verdict == "dismiss"
    assert beta.scout_dismissed_until is not None
    # Audit rows written
    assert len(rows) == 2
    # Sequential calls: skeptic → analyst → judge (3 calls total)
    assert fake_mailbox_client.complete.call_count == 2
    assert fake_mailbox_client.complete_structured.call_count == 1


def test_run_scout_debate_drops_judge_hallucinated_symbols(engine, fake_mailbox_client):
    """Judge invents a symbol not in the brief → that verdict is dropped."""
    _seed_candidate(engine, symbol="REAL", score=5.0)
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="skeptic"),
        _fake_response(text="analyst"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={
            "verdicts": [
                {"symbol": "REAL", "verdict": "elevate", "confidence": "high", "reason": "ok"},
                {"symbol": "INVENTED", "verdict": "elevate", "confidence": "high", "reason": "ok"},
            ],
        },
        used_structured=True,
    )
    out = scout_debate.run_scout_debate(engine, threshold=3.0)
    assert len(out.verdicts) == 1
    assert out.verdicts[0].symbol == "REAL"


def test_run_scout_debate_sequential_call_order(engine, fake_mailbox_client):
    """Skeptic must run before analyst (analyst reads skeptic's text in user
    message); both must run before judge."""
    _seed_candidate(engine, symbol="A", score=5.0)
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="SKEPTIC_TEXT"),
        _fake_response(text="ANALYST_TEXT"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={
            "verdicts": [
                {"symbol": "A", "verdict": "elevate", "confidence": "high", "reason": "ok"},
            ],
        },
        used_structured=True,
    )
    scout_debate.run_scout_debate(engine, threshold=3.0)
    # Inspect the analyst call: its user message must contain SKEPTIC_TEXT
    analyst_call = fake_mailbox_client.complete.call_args_list[1]
    analyst_user = analyst_call.kwargs["messages"][0]["content"]
    assert "SKEPTIC_TEXT" in analyst_user
    # Inspect the judge call: its user message must contain BOTH
    judge_call = fake_mailbox_client.complete_structured.call_args
    judge_user = judge_call.kwargs["messages"][0]["content"]
    assert "SKEPTIC_TEXT" in judge_user
    assert "ANALYST_TEXT" in judge_user
