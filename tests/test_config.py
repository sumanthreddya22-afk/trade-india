# tests/test_config.py
from pathlib import Path

import pytest

from trading_bot.config import Settings, load_config
from trading_bot.exceptions import ConfigError, LiveModeDisabled


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("GMAIL_USER", "x@y.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
    monkeypatch.setenv("BOT_MODE", "paper")

    s = Settings()
    assert s.alpaca_api_key == "key"
    assert s.bot_mode == "paper"


def test_live_mode_raises(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("GMAIL_USER", "x@y.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
    monkeypatch.setenv("BOT_MODE", "live")

    with pytest.raises(LiveModeDisabled):
        Settings()


def test_load_config_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
risk:
  daily_loss_limit_pct: 2.0
  weekly_loss_limit_pct: 5.0
  per_trade_risk_pct: 1.0
  max_position_pct: 10.0
  max_symbol_concentration_pct: 5.0
  max_consecutive_losing_days: 3
allocation:
  stocks_max_pct: 70.0
  crypto_max_pct: 30.0
  options_max_pct: 20.0
  cash_floor_pct: 10.0
regime_allocations:
  trending_up: {stocks: 60.0, crypto: 25.0, options: 15.0, cash: 0.0}
  trending_down: {stocks: 30.0, crypto: 15.0, options: 10.0, cash: 45.0}
  sideways: {stocks: 40.0, crypto: 20.0, options: 20.0, cash: 20.0}
  risk_off: {stocks: 10.0, crypto: 5.0, options: 0.0, cash: 85.0}
email:
  to: bharath8887@gmail.com
  daily_summary_time_et: "16:30"
  weekly_summary_day: Sunday
storage:
  trade_journal_path: data/trade_journal.db
"""
    )
    cfg = load_config(config_path)
    assert cfg.risk.daily_loss_limit_pct == 2.0
    assert cfg.regime_allocations["trending_up"].stocks == 60.0
    assert cfg.email.to == "bharath8887@gmail.com"


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nonexistent.yaml")


def test_risk_config_unprotected_stop_pct_default():
    """Defaults to 0.05 (5%) when not present in yaml — matches MomentumStrategy default."""
    from trading_bot.config import RiskConfig
    cfg = RiskConfig(
        daily_loss_limit_pct=2.0,
        weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0,
        max_position_pct=10.0,
        max_symbol_concentration_pct=5.0,
        max_consecutive_losing_days=3,
    )
    assert cfg.unprotected_stop_pct == 0.05


def test_risk_config_unprotected_stop_pct_override():
    from trading_bot.config import RiskConfig
    cfg = RiskConfig(
        daily_loss_limit_pct=2.0,
        weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0,
        max_position_pct=10.0,
        max_symbol_concentration_pct=5.0,
        max_consecutive_losing_days=3,
        unprotected_stop_pct=0.03,
    )
    assert cfg.unprotected_stop_pct == 0.03
