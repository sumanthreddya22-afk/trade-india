"""Phase C — Hold Debate tests.

Covers:
  * Predicate / daily-cap gating
  * Snapshot helper (write_intel_snapshot, lookup_snapshot, idempotency)
  * Sequential 4-call run_hold_debate (happy path: hold/tighten/exit)
  * Persona ordering (aggressive → conservative → neutral → judge)
  * Fail-soft contracts (no creds, SDK error, unstructured judge, schema mismatch)
  * persist_run writes audit row even on fail-soft (verdict='fail_soft')
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from trading_bot import hold_debate
from trading_bot.hold_debate import HoldDebateVerdict
from trading_bot.state_db import (
    Base, HoldDebateRun, TradeIntelSnapshot, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Predicate / daily cap
# ---------------------------------------------------------------------------


def test_should_hold_debate_under_cap():
    assert hold_debate.should_hold_debate(daily_debate_count=0, daily_cap=10) is True
    assert hold_debate.should_hold_debate(daily_debate_count=9, daily_cap=10) is True


def test_should_hold_debate_at_cap():
    assert hold_debate.should_hold_debate(daily_debate_count=10, daily_cap=10) is False


def test_should_hold_debate_zero_cap_disables():
    assert hold_debate.should_hold_debate(daily_debate_count=0, daily_cap=0) is False


def test_count_todays_returns_zero_on_empty(engine):
    assert hold_debate.count_todays_hold_debates(engine) == 0


# ---------------------------------------------------------------------------
# Snapshot helper
# ---------------------------------------------------------------------------


def test_write_snapshot_persists_row(engine):
    n = hold_debate.write_intel_snapshot(
        engine,
        entry_order_id="ord-1",
        symbol="NVDA", asset_class="stock",
        entry_intel_score=22.4,
        entry_top_reason="NVIDIA 8-K Item 2.02",
        entry_sentiment_avg=0.65,
        entry_top_sources=["sec_8k", "polygon_news", "alpaca_news"],
    )
    assert n == 1
    snap = hold_debate.lookup_snapshot(engine, "ord-1")
    assert snap.symbol == "NVDA"
    assert snap.entry_intel_score == 22.4
    assert "sec_8k" in snap.entry_top_sources_json


def test_write_snapshot_idempotent(engine):
    hold_debate.write_intel_snapshot(
        engine, entry_order_id="ord-1", symbol="A",
        asset_class="stock", entry_intel_score=5.0,
    )
    n = hold_debate.write_intel_snapshot(
        engine, entry_order_id="ord-1", symbol="A",
        asset_class="stock", entry_intel_score=99.0,  # different value
    )
    assert n == 0  # idempotent — already exists
    snap = hold_debate.lookup_snapshot(engine, "ord-1")
    # Original value preserved (not overwritten)
    assert snap.entry_intel_score == 5.0


def test_lookup_snapshot_returns_none_for_unknown(engine):
    assert hold_debate.lookup_snapshot(engine, "missing") is None


# ---------------------------------------------------------------------------
# run_hold_debate — happy path + fail-soft
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
    with patch("trading_bot.hold_debate.MailboxBackedClient") as ctor:
        client = MagicMock()
        ctor.return_value = client
        yield client


def _common_brief_kwargs():
    return dict(
        symbol="NVDA", asset_class="stock", qty=50, entry_price=875.0,
        current_price=862.0, stop_price=850.0, take_profit_price=912.5,
        days_held=1, unrealized_pnl_usd=-650.0, unrealized_pnl_pct=-0.4,
        entry_thesis="NVDA 8-K Item 2.02 Q3 beat",
        entry_intel_score=22.4, entry_sentiment=0.65,
        entry_top_sources=["sec_8k", "polygon_news"],
        current_intel_score=9.2, current_sentiment=-0.3,
        trigger_reason="8k_hard_trigger",
        new_events_summary="  - sec_8k Item 2.06 Material Impairment (-0.8)",
        lessons_block="hold_debate exit_now on Item 2.06 8-Ks: 4/4 saved 1.7%",
    )


def test_run_hold_debate_no_creds_returns_none(engine):
    from trading_bot.anthropic_client import AnthropicCredsMissingError
    with patch(
        "trading_bot.hold_debate.MailboxBackedClient",
        side_effect=AnthropicCredsMissingError("no key"),
    ):
        out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out is None


def test_run_hold_debate_sdk_error_returns_none(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = ConnectionError("dns")
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out is None


def test_run_hold_debate_unstructured_judge_returns_none(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="agg"),
        _fake_response(text="cons"),
        _fake_response(text="neu"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        text="judge free text", structured_data=None, used_structured=False,
    )
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out is None


def test_run_hold_debate_schema_mismatch_returns_none(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="agg"), _fake_response(text="cons"), _fake_response(text="neu"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={"recommendation": "BLOW_IT_UP"},  # invalid enum
        used_structured=True,
    )
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out is None


def test_run_hold_debate_exit_now_happy_path(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="AGG_TEXT - hold the line"),
        _fake_response(text="CONS_TEXT - exit, source flipped"),
        _fake_response(text="NEU_TEXT - capital efficiency favors exit"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={
            "recommendation": "exit_now", "confidence": "high",
            "reason": "sec_8k Item 2.06 inverts entry catalyst",
        },
        used_structured=True,
    )
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out is not None
    assert out.recommendation == "exit_now"
    assert out.confidence == "high"
    assert "Item 2.06" in out.reason
    assert out.aggressive_text == "AGG_TEXT - hold the line"
    assert out.conservative_text == "CONS_TEXT - exit, source flipped"
    assert out.neutral_text == "NEU_TEXT - capital efficiency favors exit"
    # Sequential: 3 free-text + 1 structured = 4 total
    assert fake_mailbox_client.complete.call_count == 3
    assert fake_mailbox_client.complete_structured.call_count == 1


def test_run_hold_debate_tighten_stop_verdict(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="agg"), _fake_response(text="cons"), _fake_response(text="neu"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={
            "recommendation": "tighten_stop", "confidence": "medium",
            "reason": "borderline material; protect gains",
        },
        used_structured=True,
    )
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out.recommendation == "tighten_stop"


def test_run_hold_debate_hold_verdict(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="agg"), _fake_response(text="cons"), _fake_response(text="neu"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={
            "recommendation": "hold", "confidence": "high",
            "reason": "thesis intact; new event is noise",
        },
        used_structured=True,
    )
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert out.recommendation == "hold"


def test_run_hold_debate_omitted_reason_synthesises_from_neutral(engine, fake_mailbox_client):
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="agg"), _fake_response(text="cons"),
        _fake_response(text="NEUTRAL_REASONING_HERE"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={"recommendation": "exit_now", "confidence": "low", "reason": ""},
        used_structured=True,
    )
    out = hold_debate.run_hold_debate(engine, **_common_brief_kwargs())
    assert "synthesized from neutral reviewer" in out.reason
    assert "NEUTRAL_REASONING_HERE" in out.reason


# ---------------------------------------------------------------------------
# Sequential persona ordering
# ---------------------------------------------------------------------------


def test_run_hold_debate_sequential_persona_order(engine, fake_mailbox_client):
    """Conservative reads aggressive's text; neutral reads both; judge reads all three."""
    fake_mailbox_client.complete.side_effect = [
        _fake_response(text="AGGRESSIVE_TEXT_X"),
        _fake_response(text="CONSERVATIVE_TEXT_Y"),
        _fake_response(text="NEUTRAL_TEXT_Z"),
    ]
    fake_mailbox_client.complete_structured.return_value = _fake_response(
        structured_data={"recommendation": "hold", "confidence": "low", "reason": "ok"},
        used_structured=True,
    )
    hold_debate.run_hold_debate(engine, **_common_brief_kwargs())

    # Neutral call (3rd) must include AGGRESSIVE_TEXT_X + CONSERVATIVE_TEXT_Y
    neutral_call = fake_mailbox_client.complete.call_args_list[2]
    neutral_user = neutral_call.kwargs["messages"][0]["content"]
    assert "AGGRESSIVE_TEXT_X" in neutral_user
    assert "CONSERVATIVE_TEXT_Y" in neutral_user

    # Judge call must include all three
    judge_call = fake_mailbox_client.complete_structured.call_args
    judge_user = judge_call.kwargs["messages"][0]["content"]
    assert "AGGRESSIVE_TEXT_X" in judge_user
    assert "CONSERVATIVE_TEXT_Y" in judge_user
    assert "NEUTRAL_TEXT_Z" in judge_user


# ---------------------------------------------------------------------------
# persist_run
# ---------------------------------------------------------------------------


def test_persist_run_records_verdict(engine):
    v = HoldDebateVerdict(
        recommendation="exit_now", confidence="high",
        reason="catalyst inverted",
        aggressive_text="agg", conservative_text="cons", neutral_text="neu",
    )
    n = hold_debate.persist_run(
        engine, verdict=v, symbol="NVDA", asset_class="stock",
        entry_order_id="ord-1", trigger_reason="8k_hard_trigger",
        current_score=9.2, current_sentiment=-0.3,
        entry_score=22.4, entry_sentiment=0.65,
        action_taken="flattened",
    )
    assert n == 1
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.query(HoldDebateRun).first()
    assert row.symbol == "NVDA"
    assert row.verdict == "exit_now"
    assert row.action_taken == "flattened"
    assert row.entry_order_id == "ord-1"
    assert "agg=" in row.prompt_version  # composed persona version


def test_persist_run_records_fail_soft_verdict(engine):
    """When LLM gate is unreachable, we still persist a row to audit the trigger."""
    n = hold_debate.persist_run(
        engine, verdict=None, symbol="NVDA", asset_class="stock",
        entry_order_id="ord-2", trigger_reason="sentiment_flip",
        current_score=4.0, current_sentiment=-0.5,
        entry_score=10.0, entry_sentiment=0.5,
        action_taken="none",
    )
    assert n == 1
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.query(HoldDebateRun).first()
    assert row.verdict == "fail_soft"
    assert row.action_taken == "none"
    assert "unreachable" in row.judge_reason
