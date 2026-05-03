"""Phase D — Lesson loop + analyzer + brief injection tests.

Covers:
  * aggregate_outcomes joins entry/unblock/hold debate runs with closed P&L
  * Per-verdict and per-source winrate computation
  * Shadow-tracked skipped trades surface
  * write_lesson + latest_lesson + latest_lesson_block (freshness gate)
  * DebateOutcomeAnalyzerRole writes a row even on LLM failure (fail-soft)
  * Scout debate brief includes RECENT LESSONS when a fresh lesson exists
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from trading_bot import lesson_loop
from trading_bot.lesson_loop import OutcomeReport
from trading_bot.roles.debate_outcome_analyzer import DebateOutcomeAnalyzerRole
from trading_bot.state_db import (
    Base, DebateLesson, EntryDebateRun, HoldDebateRun,
    UnblockDebateRun, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


def _seed_entry_debate(engine, *, symbol="A", verdict="place",
                        closed_pnl_pct=None, run_at=None,
                        signal_reason="sec_8k catalyst",
                        judge_reason="placed on multi-source confirmation"):
    from sqlalchemy.orm import Session
    run_at = run_at or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(EntryDebateRun(
            run_at=run_at, asset_class="stock", symbol=symbol,
            intel_score=10.0, signal_reason=signal_reason, regime="sideways",
            verdict=verdict, confidence="high",
            judge_reason=judge_reason,
            closed_pnl_pct=closed_pnl_pct,
        ))
        s.commit()


def _seed_hold_debate(engine, *, symbol="A", verdict="exit_now",
                      resulting_pnl_pct=None, run_at=None):
    from sqlalchemy.orm import Session
    run_at = run_at or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(HoldDebateRun(
            run_at=run_at, asset_class="stock", symbol=symbol,
            verdict=verdict, confidence="high",
            judge_reason="exit on 8-K inversion",
            resulting_pnl_pct=resulting_pnl_pct,
            trigger_reason="8k_hard_trigger",
            action_taken="flattened",
        ))
        s.commit()


# ---------------------------------------------------------------------------
# aggregate_outcomes
# ---------------------------------------------------------------------------


def test_aggregate_empty_returns_zero_counts(engine):
    out = lesson_loop.aggregate_outcomes(engine)
    assert out.n_trades_closed == 0
    assert out.n_entry_debates == 0
    assert out.overall_place_winrate is None


def test_aggregate_counts_winrate_correctly(engine):
    # 3 wins, 2 losses → 60% winrate
    for sym, pnl in [("A", 1.0), ("B", 2.0), ("C", 0.5),
                     ("D", -1.0), ("E", -0.5)]:
        _seed_entry_debate(engine, symbol=sym, closed_pnl_pct=pnl)
    out = lesson_loop.aggregate_outcomes(engine)
    assert out.n_entry_debates == 5
    assert out.overall_place_winrate == pytest.approx(0.6)
    assert "place" in out.per_verdict_winrate
    assert out.per_verdict_winrate["place"]["n"] == 5
    assert out.per_verdict_winrate["place"]["winrate"] == pytest.approx(0.6)


def test_aggregate_skips_open_trades(engine):
    """Rows with closed_pnl_pct=None aren't counted as outcomes."""
    _seed_entry_debate(engine, symbol="A", closed_pnl_pct=1.0)
    _seed_entry_debate(engine, symbol="B", closed_pnl_pct=None)  # open
    out = lesson_loop.aggregate_outcomes(engine)
    assert out.per_verdict_winrate["place"]["n"] == 1


def test_aggregate_filters_outside_lookback(engine):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    _seed_entry_debate(engine, symbol="OLD", closed_pnl_pct=1.0, run_at=old)
    _seed_entry_debate(engine, symbol="NEW", closed_pnl_pct=1.0)
    out = lesson_loop.aggregate_outcomes(engine, lookback_days=14)
    assert out.n_entry_debates == 1
    assert out.per_verdict_winrate["place"]["n"] == 1


def test_aggregate_per_source_attribution_extracts_tokens(engine):
    """signal_reason mentioning a source name → that source gets credit."""
    _seed_entry_debate(engine, symbol="A", closed_pnl_pct=2.0,
                        signal_reason="strong sec_8k catalyst")
    _seed_entry_debate(engine, symbol="B", closed_pnl_pct=-1.0,
                        signal_reason="googlenews_rss only — risky")
    out = lesson_loop.aggregate_outcomes(engine)
    assert "sec_8k" in out.per_source_winrate
    assert out.per_source_winrate["sec_8k"]["winrate"] == 1.0
    assert "googlenews_rss" in out.per_source_winrate
    assert out.per_source_winrate["googlenews_rss"]["winrate"] == 0.0


def test_aggregate_shadow_tracks_skipped_trades(engine):
    """Verdict='skip' with no closed_pnl gets surfaced for false-negative analysis."""
    _seed_entry_debate(engine, symbol="MAYBE", verdict="skip", closed_pnl_pct=None)
    out = lesson_loop.aggregate_outcomes(engine)
    assert any(s["symbol"] == "MAYBE" for s in out.shadow_skips)


def test_aggregate_includes_hold_debates(engine):
    _seed_hold_debate(engine, symbol="A", verdict="exit_now", resulting_pnl_pct=-0.5)
    _seed_hold_debate(engine, symbol="B", verdict="hold", resulting_pnl_pct=2.0)
    out = lesson_loop.aggregate_outcomes(engine)
    assert out.n_hold_debates == 2
    assert "hold_exit_now" in out.per_verdict_winrate
    assert "hold_hold" in out.per_verdict_winrate


def test_aggregate_losing_patterns_sample_sorted_worst_first(engine):
    for sym, pnl in [("WORST", -5.0), ("MID", -2.0), ("MILD", -0.5)]:
        _seed_entry_debate(engine, symbol=sym, closed_pnl_pct=pnl)
    out = lesson_loop.aggregate_outcomes(engine, sample_losing=2)
    assert len(out.losing_patterns) == 2
    assert out.losing_patterns[0]["symbol"] == "WORST"
    assert out.losing_patterns[1]["symbol"] == "MID"


# ---------------------------------------------------------------------------
# write_lesson + latest_lesson_block
# ---------------------------------------------------------------------------


def test_write_lesson_persists_row(engine):
    report = OutcomeReport(lookback_days=14, n_trades_closed=10,
                           overall_place_winrate=0.6)
    n = lesson_loop.write_lesson(
        engine, report=report, summary_text="68% place winrate; sec_8k strong",
        candidate_edits=[{"edit": "discount stale catalysts"}],
        prompt_version="lesson_analyst=v1",
    )
    assert n == 1
    row = lesson_loop.latest_lesson(engine)
    assert row is not None
    assert row.n_trades_closed == 10
    assert row.overall_place_winrate == 0.6


def test_latest_lesson_block_returns_empty_when_no_lessons(engine):
    assert lesson_loop.latest_lesson_block(engine) == ""


def test_latest_lesson_block_returns_populated_summary(engine):
    report = OutcomeReport(lookback_days=14, n_trades_closed=10,
                           overall_place_winrate=0.7)
    lesson_loop.write_lesson(engine, report=report,
                              summary_text="SUMMARY: 70% winrate; sec_8k strong")
    out = lesson_loop.latest_lesson_block(engine)
    assert "70%" in out
    assert "SUMMARY" in out
    assert "10 closed trades" in out


def test_latest_lesson_block_returns_empty_when_stale(engine):
    """Lesson older than max_age_days (default 7) → no injection."""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    report = OutcomeReport(lookback_days=14, n_trades_closed=5)
    lesson_loop.write_lesson(engine, report=report, summary_text="stale", now=old)
    assert lesson_loop.latest_lesson_block(engine) == ""


def test_latest_lesson_block_returns_empty_when_summary_blank(engine):
    report = OutcomeReport(lookback_days=14, n_trades_closed=5)
    lesson_loop.write_lesson(engine, report=report, summary_text="")
    assert lesson_loop.latest_lesson_block(engine) == ""


# ---------------------------------------------------------------------------
# DebateOutcomeAnalyzerRole
# ---------------------------------------------------------------------------


def test_analyzer_skips_when_no_data(engine):
    role = DebateOutcomeAnalyzerRole(engine=engine)
    out = role._do_work({})
    assert out["wrote_lesson"] is False
    assert "no debates" in out["skipped_reason"]


def test_analyzer_writes_row_on_success(engine):
    _seed_entry_debate(engine, symbol="A", closed_pnl_pct=1.0)
    fake_resp = MagicMock()
    fake_resp.text = ("SUMMARY:\nGreat period.\n\nWHAT WORKED:\n- 100% winrate\n\n"
                      "CANDIDATE EDITS:\n- Be more aggressive on multi-source\n")
    fake_client = MagicMock()
    fake_client.complete.return_value = fake_resp
    with patch(
        "trading_bot.mailbox_backed_client.MailboxBackedClient",
        return_value=fake_client,
    ) as ctor:
        role = DebateOutcomeAnalyzerRole(engine=engine)
        out = role._do_work({})
    assert out["wrote_lesson"] is True
    assert out["n_candidate_edits"] >= 1
    row = lesson_loop.latest_lesson(engine)
    assert row is not None
    assert "SUMMARY" in row.summary_text
    # Candidate edits parsed
    import json as _json
    edits = _json.loads(row.candidate_edits_json)
    assert any("aggressive" in e["edit"].lower() for e in edits)


def test_analyzer_writes_placeholder_on_llm_error(engine):
    _seed_entry_debate(engine, symbol="A", closed_pnl_pct=1.0)
    fake_client = MagicMock()
    fake_client.complete.side_effect = ConnectionError("dns")
    with patch(
        "trading_bot.mailbox_backed_client.MailboxBackedClient",
        return_value=fake_client,
    ):
        role = DebateOutcomeAnalyzerRole(engine=engine)
        out = role._do_work({})
    assert out["wrote_lesson"] is True
    row = lesson_loop.latest_lesson(engine)
    assert "analyzer LLM error" in row.summary_text


def test_analyzer_writes_placeholder_on_no_creds(engine):
    _seed_entry_debate(engine, symbol="A", closed_pnl_pct=1.0)
    from trading_bot.anthropic_client import AnthropicCredsMissingError
    with patch(
        "trading_bot.mailbox_backed_client.MailboxBackedClient",
        side_effect=AnthropicCredsMissingError("no key"),
    ):
        role = DebateOutcomeAnalyzerRole(engine=engine)
        out = role._do_work({})
    assert out["wrote_lesson"] is True
    row = lesson_loop.latest_lesson(engine)
    assert "no anthropic creds" in row.summary_text


# ---------------------------------------------------------------------------
# Brief integration: scout debate sees latest lesson
# ---------------------------------------------------------------------------


def test_scout_debate_brief_includes_lesson_block(engine):
    """When a fresh lesson exists, scout_debate's brief contains it."""
    from trading_bot.intel import scout_debate
    from trading_bot.state_db import IntelCandidate
    from sqlalchemy.orm import Session

    # Seed a fresh lesson
    report = OutcomeReport(lookback_days=14, n_trades_closed=10,
                           overall_place_winrate=0.7)
    lesson_loop.write_lesson(engine, report=report,
                              summary_text="SUMMARY: 70% winrate; sec_8k strong")

    # Seed a candidate
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(IntelCandidate(
            symbol="NVDA", asset_class="stock", score=8.0,
            n_mentions=3, n_sources=2,
            first_seen=now, last_seen=now,
            top_reason="x", sources_json="{}",
            sentiment_avg=0.5, rolled_up_at=now,
        ))
        s.commit()

    fake_resp = MagicMock()
    fake_resp.text = "skeptic"
    fake_resp.data = None
    fake_resp.used_structured = False
    fake_judge = MagicMock()
    fake_judge.text = ""
    fake_judge.used_structured = True
    fake_judge.data = {
        "verdicts": [
            {"symbol": "NVDA", "verdict": "elevate",
             "confidence": "high", "reason": "ok"},
        ],
    }
    fake_client = MagicMock()
    fake_client.complete.side_effect = [fake_resp, fake_resp]
    fake_client.complete_structured.return_value = fake_judge
    with patch(
        "trading_bot.intel.scout_debate.MailboxBackedClient",
        return_value=fake_client,
    ):
        scout_debate.run_scout_debate(engine, threshold=3.0)

    # Inspect skeptic call's user message to confirm lesson injected
    skeptic_call = fake_client.complete.call_args_list[0]
    skeptic_user = skeptic_call.kwargs["messages"][0]["content"]
    assert "RECENT LESSONS" in skeptic_user
    assert "70%" in skeptic_user
    assert "SUMMARY" in skeptic_user
