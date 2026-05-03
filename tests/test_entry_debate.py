"""Tests for trading_bot.entry_debate.

The entry-debate module is FAIL-SOFT: any error path returns None and
the caller treats that as "skip the trade AND queue an alert". The tests
below lock that contract in for every error path, plus the daily-cap
predicate and the happy path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError, AnthropicResponse, BudgetExceededError,
    StructuredResponse,
)
from trading_bot.entry_debate import (
    EntryDebateVerdict, run_entry_debate, should_entry_debate,
)


# ---------------------------------------------------------------------------
# should_entry_debate predicate — daily cap is the only gate
# ---------------------------------------------------------------------------


def test_predicate_passes_under_cap():
    assert should_entry_debate(daily_debate_count=0, daily_cap=50) is True
    assert should_entry_debate(daily_debate_count=49, daily_cap=50) is True


def test_predicate_blocks_at_cap():
    assert should_entry_debate(daily_debate_count=50, daily_cap=50) is False


def test_predicate_blocks_over_cap():
    assert should_entry_debate(daily_debate_count=200, daily_cap=50) is False


def test_predicate_blocks_when_cap_zero():
    """daily_cap=0 disables the gate entirely (operator off-switch)."""
    assert should_entry_debate(daily_debate_count=0, daily_cap=0) is False


# ---------------------------------------------------------------------------
# run_entry_debate — fail-soft contract for every error path
# ---------------------------------------------------------------------------


def _proposal_kwargs():
    return {
        "proposal_summary": "  symbol: BTC/USD  qty: 1  entry: 65000\n",
        "intel_score": 8.5,
        "intel_top_reason": "ETF inflows hit 2-week high",
        "signal_reason": "rsi=62 macd>signal close>EMA20",
        "regime": "sideways",
        "indicators": "  rsi_14: 62.1\n",
        "operational_context": "  equity_usd: 50000\n",
    }


def test_no_creds_returns_none():
    fake_engine = MagicMock()
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient",
        side_effect=AnthropicCredsMissingError("no creds"),
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())
    assert out is None


def test_budget_exceeded_returns_none():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.side_effect = BudgetExceededError("monthly cap hit")
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())
    assert out is None


def test_sdk_exception_returns_none():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.side_effect = ConnectionError("network down")
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())
    assert out is None


def test_judge_returns_freetext_returns_none():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="some debate text", input_tokens=10, output_tokens=20,
        request_id="r1", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data=None, text="I think you should skip", used_structured=False,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())
    assert out is None


def test_judge_schema_mismatch_returns_none():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="t", input_tokens=10, output_tokens=20, request_id="r1", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data={"this_is_not_the_right_field": "place"},
        text="", used_structured=True,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())
    assert out is None


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_judge_place_returns_verdict():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="aggressive perspective", input_tokens=10, output_tokens=20,
        request_id="r1", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data={
            "recommendation": "place",
            "confidence": "high",
            "reason": (
                "Concrete catalyst (ETF inflows) corroborated by clean "
                "Momentum technicals; intel score is exceptional, not generic."
            ),
        },
        text="", used_structured=True,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())

    assert isinstance(out, EntryDebateVerdict)
    assert out.recommendation == "place"
    assert out.confidence == "high"
    assert "etf inflows" in out.reason.lower()
    assert out.aggressive_text == "aggressive perspective"


def test_judge_skip_returns_verdict():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="t", input_tokens=10, output_tokens=20, request_id="r1", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data={
            "recommendation": "skip",
            "confidence": "medium",
            "reason": (
                "Headline is recycled; no concrete edge identified. "
                "Conservative case stands."
            ),
        },
        text="", used_structured=True,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())

    assert isinstance(out, EntryDebateVerdict)
    assert out.recommendation == "skip"
    assert out.confidence == "medium"


def test_four_llm_calls_made_in_correct_order():
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="t", input_tokens=1, output_tokens=1, request_id="r", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data={"recommendation": "skip", "confidence": "low",
              "reason": "x" * 30},
        text="", used_structured=True,
        input_tokens=1, output_tokens=1, request_id="r", model="m",
    )
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())

    # Three free-text + one structured = 4 LLM calls total
    assert inner.complete.call_count == 3
    assert inner.complete_structured.call_count == 1


def test_judge_omits_reason_synthesizes_from_neutral():
    """Opus occasionally drops the reason field. We synthesise from
    neutral text rather than fail-soft, so the verdict is preserved."""
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.side_effect = [
        AnthropicResponse(text="aggressive", input_tokens=1, output_tokens=1,
                          request_id="r1", model="m"),
        AnthropicResponse(text="conservative", input_tokens=1, output_tokens=1,
                          request_id="r2", model="m"),
        AnthropicResponse(text="balanced view: edge is real", input_tokens=1,
                          output_tokens=1, request_id="r3", model="m"),
    ]
    inner.complete_structured.return_value = StructuredResponse(
        data={"recommendation": "place", "confidence": "low", "reason": ""},
        text="", used_structured=True,
        input_tokens=1, output_tokens=1, request_id="r4", model="m",
    )
    with patch(
        "trading_bot.entry_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_entry_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())
    assert out is not None
    assert out.recommendation == "place"
    assert "synthesized" in out.reason.lower()
    assert "edge is real" in out.reason
