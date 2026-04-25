import pytest
from trading_bot.exceptions import (
    TradingBotError,
    RiskRuleViolation,
    LiveModeDisabled,
    ConfigError,
    AlpacaClientError,
)


def test_all_exceptions_inherit_base():
    for exc in (RiskRuleViolation, LiveModeDisabled, ConfigError, AlpacaClientError):
        assert issubclass(exc, TradingBotError)


def test_risk_rule_violation_carries_rule_name():
    err = RiskRuleViolation(rule="daily_loss_limit", detail="-3.1% breach")
    assert err.rule == "daily_loss_limit"
    assert "daily_loss_limit" in str(err)
    assert "-3.1%" in str(err)


def test_live_mode_disabled_message():
    err = LiveModeDisabled()
    assert "paper" in str(err).lower()
