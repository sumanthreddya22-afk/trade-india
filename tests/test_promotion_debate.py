"""promotion_debate module tests — verify the three-call sequence and
fail-open semantics."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError,
    AnthropicResponse,
    BudgetExceededError,
    StructuredResponse,
)
from trading_bot.promotion import PromotionCandidate
from trading_bot.promotion_debate import DebateVerdict, run_promotion_debate
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _candidate():
    return PromotionCandidate(
        template="momentum_v3",
        params={"rsi_lower": 30, "rsi_upper": 70},
        fitness=2.4,
        alpha_vs_spy_x=1.8,
        sortino=1.6,
        max_dd_pct=12.0,
    )


def _resp(text: str) -> AnthropicResponse:
    return AnthropicResponse(
        text=text, input_tokens=10, output_tokens=20, request_id="r", model="claude-opus-4-7"
    )


def _judge(verdict: str = "promote", confidence: str = "high") -> StructuredResponse:
    return StructuredResponse(
        data={
            "bear_addressed": verdict == "promote",
            "confidence": confidence,
            "reason": "Backtest fold metrics are consistent and lessons are clean.",
            "recommendation": verdict,
        },
        text="",
        used_structured=True,
        input_tokens=10, output_tokens=20, request_id="r", model="claude-opus-4-7",
    )


def test_debate_returns_promote_verdict_on_happy_path(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = [_resp("bull says yes"), _resp("bear pushes back")]
    instance.complete_structured.return_value = _judge("promote", "high")

    with patch("trading_bot.promotion_debate.AnthropicClient", return_value=instance):
        v = run_promotion_debate(engine, _candidate())

    assert isinstance(v, DebateVerdict)
    assert v.recommendation == "promote"
    assert v.bull_text == "bull says yes"
    assert v.bear_text == "bear pushes back"
    assert instance.complete.call_count == 2
    assert instance.complete_structured.call_count == 1


def test_debate_returns_block_verdict(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = [_resp("b"), _resp("b")]
    instance.complete_structured.return_value = _judge("block", "high")

    with patch("trading_bot.promotion_debate.AnthropicClient", return_value=instance):
        v = run_promotion_debate(engine, _candidate())

    assert v.recommendation == "block"
    assert v.confidence == "high"


def test_debate_fails_open_on_missing_creds(engine, monkeypatch):
    """No creds → returns None (caller falls back to prior behaviour)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch(
        "trading_bot.promotion_debate.AnthropicClient",
        side_effect=AnthropicCredsMissingError("no key"),
    ):
        assert run_promotion_debate(engine, _candidate()) is None


def test_debate_fails_open_on_budget_halt(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = BudgetExceededError("cap hit")
    with patch("trading_bot.promotion_debate.AnthropicClient", return_value=instance):
        assert run_promotion_debate(engine, _candidate()) is None


def test_debate_fails_open_on_sdk_exception(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = RuntimeError("network blip")
    with patch("trading_bot.promotion_debate.AnthropicClient", return_value=instance):
        assert run_promotion_debate(engine, _candidate()) is None


def test_debate_fails_open_when_judge_returns_text_only(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = [_resp("b"), _resp("b")]
    instance.complete_structured.return_value = StructuredResponse(
        data=None, text="judge ignored the schema", used_structured=False,
        input_tokens=1, output_tokens=1, request_id="r", model="claude-opus-4-7",
    )
    with patch("trading_bot.promotion_debate.AnthropicClient", return_value=instance):
        assert run_promotion_debate(engine, _candidate()) is None
