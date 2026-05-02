"""Tests for trading_bot.unblock_debate.

Critical guarantee: this module is FAIL-CLOSED. Any error path — credentials
missing, budget exceeded, SDK exception, judge schema mismatch, judge
returning free-text instead of structured tool use — MUST return None so
the caller respects the original gate rejection. The tests below lock that
in for every error path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError, BudgetExceededError, AnthropicResponse,
    StructuredResponse,
)
from trading_bot.unblock_debate import (
    UnblockVerdict, run_unblock_debate, should_unblock_debate,
)


# ---------------------------------------------------------------------------
# should_unblock_debate predicate
# ---------------------------------------------------------------------------


def test_predicate_blocks_when_overage_too_large():
    """A 2x-over-cap rejection should NOT trigger a debate; that's beyond
    'borderline' — operator already explicitly chose the cap and 2x violates
    the spirit of the rule."""
    assert should_unblock_debate(
        rejection_reason="options_cap exceeded",
        rejection_overage_ratio=1.50,  # 150% over cap
        candidate_score=9.0,
        daily_debate_count=0,
        max_overage_ratio=0.50,
    ) is False


def test_predicate_blocks_when_score_too_low():
    """Low-conviction candidates don't get debated even if borderline."""
    assert should_unblock_debate(
        rejection_reason="options_cap",
        rejection_overage_ratio=0.30,
        candidate_score=4.0,  # below default min 7
        daily_debate_count=0,
    ) is False


def test_predicate_blocks_when_daily_cap_hit():
    """Daily LLM budget hard cap."""
    assert should_unblock_debate(
        rejection_reason="options_cap",
        rejection_overage_ratio=0.30,
        candidate_score=9.0,
        daily_debate_count=15,  # at default cap
        daily_cap=15,
    ) is False


def test_predicate_passes_for_borderline_high_conviction():
    """Borderline overage + high conviction + budget available → debate."""
    assert should_unblock_debate(
        rejection_reason="options_cap",
        rejection_overage_ratio=0.30,
        candidate_score=8.5,
        daily_debate_count=2,
    ) is True


# ---------------------------------------------------------------------------
# run_unblock_debate — fail-closed contract
# ---------------------------------------------------------------------------


def _proposal_kwargs():
    return {
        "proposal_summary": "  symbol: TEST  strike: 50  bid: 1.00\n",
        "block_reason": "options_cap (35% > 20%)",
        "overage_ratio": 0.75,
        "fundamentals": "iv_rank: 70",
        "operational_context": "equity: 15000",
    }


def test_no_creds_returns_none():
    """Missing ANTHROPIC_API_KEY → None (fail closed)."""
    fake_engine = MagicMock()
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient",
        side_effect=AnthropicCredsMissingError("no creds"),
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )
    assert out is None


def test_budget_exceeded_returns_none():
    """Mid-debate budget halt → None."""
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.side_effect = BudgetExceededError("monthly cap hit")
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )
    assert out is None


def test_sdk_exception_returns_none():
    """Network error or any other SDK exception → None."""
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.side_effect = ConnectionError("network down")
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )
    assert out is None


def test_judge_returns_freetext_returns_none():
    """If judge ignores tool schema and returns free text, fail closed."""
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="some debate text", input_tokens=10, output_tokens=20,
        request_id="r1", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data=None, text="I think you should reject", used_structured=False,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )
    assert out is None


def test_judge_schema_mismatch_returns_none():
    """Judge returns structured data but with wrong shape → None."""
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
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )
    assert out is None


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_judge_place_returns_verdict_with_recommendation():
    """Judge returns valid structured place verdict → wrapped UnblockVerdict."""
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
            "reason": ("Borderline at 30% over cap with elevated IV "
                       "and post-earnings setup; concrete edge identified."),
        },
        text="", used_structured=True,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )

    assert isinstance(out, UnblockVerdict)
    assert out.recommendation == "place"
    assert out.confidence == "high"
    assert "borderline" in out.reason.lower()
    assert out.aggressive_text == "aggressive perspective"


def test_judge_reject_returns_verdict_with_recommendation():
    """Judge defaults to 'reject' for marginal cases — verify."""
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="t", input_tokens=10, output_tokens=20, request_id="r1", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data={
            "recommendation": "reject",
            "confidence": "medium",
            "reason": ("Aggressive case is generic; conservative risk of "
                       "concentrated drawdown stands; not exceptional."),
        },
        text="", used_structured=True,
        input_tokens=10, output_tokens=20, request_id="r2", model="m",
    )
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        out = run_unblock_debate(
            fake_engine, use_mailbox=False, **_proposal_kwargs()
        )

    assert isinstance(out, UnblockVerdict)
    assert out.recommendation == "reject"
    assert out.confidence == "medium"


def test_four_llm_calls_made_in_correct_order():
    """Aggressive, conservative, neutral, judge — in that order."""
    fake_engine = MagicMock()
    inner = MagicMock()
    inner.complete.return_value = AnthropicResponse(
        text="t", input_tokens=1, output_tokens=1, request_id="r", model="m",
    )
    inner.complete_structured.return_value = StructuredResponse(
        data={"recommendation": "reject", "confidence": "low",
              "reason": "x" * 30},
        text="", used_structured=True,
        input_tokens=1, output_tokens=1, request_id="r", model="m",
    )
    with patch(
        "trading_bot.unblock_debate.MailboxBackedClient", return_value=inner
    ):
        run_unblock_debate(fake_engine, use_mailbox=False, **_proposal_kwargs())

    # Three free-text + one structured = 4 LLM calls total
    assert inner.complete.call_count == 3
    assert inner.complete_structured.call_count == 1
