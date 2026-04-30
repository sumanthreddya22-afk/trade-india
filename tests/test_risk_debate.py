"""risk_debate module tests — trigger predicate + four-call debate +
fail-open semantics."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError,
    AnthropicResponse,
    BudgetExceededError,
    StructuredResponse,
)
from trading_bot.risk_debate import (
    RiskDebateVerdict,
    run_risk_debate,
    should_debate,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


# --- should_debate predicate ----------------------------------------------


def test_should_debate_streak():
    assert should_debate(consecutive_losing_days=2, size_multiplier=1.0) is True
    assert should_debate(consecutive_losing_days=1, size_multiplier=1.0) is False


def test_should_debate_size_throttle():
    assert should_debate(consecutive_losing_days=0, size_multiplier=0.5) is True
    assert should_debate(consecutive_losing_days=0, size_multiplier=Decimal("0.25")) is True
    assert should_debate(consecutive_losing_days=0, size_multiplier=1.0) is False


def test_should_debate_var():
    assert should_debate(consecutive_losing_days=0, size_multiplier=1.0,
                         trade_var=0.01) is True
    assert should_debate(consecutive_losing_days=0, size_multiplier=1.0,
                         trade_var=0.001) is False


def test_should_debate_handles_invalid_size_multiplier_gracefully():
    # Non-numeric size_multiplier should not crash the predicate.
    assert should_debate(consecutive_losing_days=0,
                         size_multiplier="not-a-number") is False


# --- run_risk_debate ------------------------------------------------------


_ORDER_KW = dict(
    symbol="AAPL", action="buy", qty=Decimal("10"),
    entry_price=Decimal("180"), stop_loss_price=Decimal("171"),
    strategy="momentum", regime="trending_up",
    consecutive_losing_days=2,
)


def _resp(text: str) -> AnthropicResponse:
    return AnthropicResponse(
        text=text, input_tokens=10, output_tokens=20, request_id="r", model="claude-opus-4-7"
    )


def _verdict(rec: str = "place", conf: str = "high") -> StructuredResponse:
    return StructuredResponse(
        data={
            "confidence": conf,
            "reason": "Reviewed by all three reviewers; no immediate concrete risk.",
            "recommendation": rec,
        },
        text="", used_structured=True,
        input_tokens=10, output_tokens=10, request_id="r", model="claude-opus-4-7",
    )


def test_runs_full_four_call_sequence(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = [_resp("aggressive"), _resp("conservative"), _resp("neutral")]
    instance.complete_structured.return_value = _verdict("place", "high")

    with patch("trading_bot.risk_debate.AnthropicClient", return_value=instance):
        v = run_risk_debate(engine, **_ORDER_KW)

    assert isinstance(v, RiskDebateVerdict)
    assert v.recommendation == "place"
    assert v.aggressive_text == "aggressive"
    assert v.conservative_text == "conservative"
    assert v.neutral_text == "neutral"
    assert instance.complete.call_count == 3
    assert instance.complete_structured.call_count == 1


def test_returns_reject_verdict(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = [_resp("a"), _resp("c"), _resp("n")]
    instance.complete_structured.return_value = _verdict("reject", "high")

    with patch("trading_bot.risk_debate.AnthropicClient", return_value=instance):
        v = run_risk_debate(engine, **_ORDER_KW)
    assert v.recommendation == "reject"


def test_fails_open_on_missing_creds(engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch(
        "trading_bot.risk_debate.AnthropicClient",
        side_effect=AnthropicCredsMissingError("no key"),
    ):
        assert run_risk_debate(engine, **_ORDER_KW) is None


def test_fails_open_on_budget_halt(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = BudgetExceededError("cap hit")
    with patch("trading_bot.risk_debate.AnthropicClient", return_value=instance):
        assert run_risk_debate(engine, **_ORDER_KW) is None


def test_fails_open_on_sdk_exception(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = RuntimeError("network blip")
    with patch("trading_bot.risk_debate.AnthropicClient", return_value=instance):
        assert run_risk_debate(engine, **_ORDER_KW) is None


def test_fails_open_when_judge_returns_text_only(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    instance = MagicMock()
    instance.complete.side_effect = [_resp("a"), _resp("c"), _resp("n")]
    instance.complete_structured.return_value = StructuredResponse(
        data=None, text="judge ignored the schema", used_structured=False,
        input_tokens=1, output_tokens=1, request_id="r", model="claude-opus-4-7",
    )
    with patch("trading_bot.risk_debate.AnthropicClient", return_value=instance):
        assert run_risk_debate(engine, **_ORDER_KW) is None
