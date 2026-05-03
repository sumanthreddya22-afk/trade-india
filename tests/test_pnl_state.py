from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.shared.config import (
    AllocationConfig,
    AppConfig,
    EmailConfig,
    RegimeAllocation,
    RiskConfig,
    StorageConfig,
)
from trading_bot.pnl_state import PnlStateBuilder


def _cfg() -> AppConfig:
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0, weekly_loss_limit_pct=5.0, per_trade_risk_pct=1.0,
            max_position_pct=10.0, max_symbol_concentration_pct=5.0, max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(stocks_max_pct=70, crypto_max_pct=30, options_max_pct=20, cash_floor_pct=10),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
            "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
            "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
            "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
        },
        email=EmailConfig(to="x@y.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )


@pytest.fixture
def fake_settings():
    return MagicMock(
        alpaca_api_key="k",
        alpaca_api_secret="s",
        alpaca_base_url="https://paper-api.alpaca.markets/v2",
    )


def test_pnl_no_data_returns_zeros(fake_settings):
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(equity=[])
        builder = PnlStateBuilder(fake_settings, _cfg())
        r = builder.read()
        assert r.daily_pnl_pct == Decimal("0")
        assert r.weekly_pnl_pct == Decimal("0")
        assert r.halted is False


def test_pnl_computes_daily_and_weekly(fake_settings):
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        # 5 days of equity: 15000, 15050, 15100, 15080, 15150
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=[15000.0, 15050.0, 15100.0, 15080.0, 15150.0]
        )
        builder = PnlStateBuilder(fake_settings, _cfg())
        r = builder.read()
        # daily: 15150 vs 15080 = +0.46%
        assert r.daily_pnl_pct == Decimal("0.46")
        # weekly: 15150 vs 15000 = +1.00%
        assert r.weekly_pnl_pct == Decimal("1.00")
        assert r.halted is False


def test_pnl_triggers_daily_halt(fake_settings):
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=[15000.0, 15000.0, 14600.0]  # -2.67% daily
        )
        builder = PnlStateBuilder(fake_settings, _cfg())
        r = builder.read()
        assert r.halted is True
        assert "daily" in r.halt_reason


def test_pnl_triggers_weekly_halt(fake_settings):
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        # Slow bleed over a week: starts 15000, ends 14200 = -5.33%, but no single day breaches 2%
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=[15000.0, 14850.0, 14700.0, 14550.0, 14400.0, 14200.0]
        )
        builder = PnlStateBuilder(fake_settings, _cfg())
        r = builder.read()
        assert r.halted is True
        assert "weekly" in r.halt_reason


def test_pnl_counts_consecutive_losing_days(fake_settings):
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        # up, up, down, down, down — last 3 are losing
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=[15000.0, 15050.0, 15100.0, 15080.0, 15060.0, 15050.0]
        )
        builder = PnlStateBuilder(fake_settings, _cfg())
        r = builder.read()
        assert r.consecutive_losing_days == 3


def test_pnl_skips_none_values(fake_settings):
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=[15000.0, None, 15050.0, None, 15100.0]
        )
        builder = PnlStateBuilder(fake_settings, _cfg())
        r = builder.read()
        # Filtered to [15000, 15050, 15100]; daily = (15100-15050)/15050 ≈ 0.33
        assert r.daily_pnl_pct == Decimal("0.33")


# ---- Bucket A: throttle ladder ----


def _equity_series_with_streak(streak_len: int) -> list[float]:
    """Return a 7-day series whose tail has `streak_len` strict down-days."""
    series = [15000.0, 15050.0, 15100.0, 15150.0, 15200.0, 15250.0, 15300.0]
    # Replace the tail with a strictly decreasing sequence
    base = series[-(streak_len + 1)]
    for i in range(1, streak_len + 1):
        series[-(streak_len + 1) + i] = base - i  # tiny down moves so daily/weekly halts don't fire
    return series


def test_throttle_below_cap_keeps_full_size(fake_settings):
    """2 losing days < cap=3 → multiplier stays 1.0."""
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=_equity_series_with_streak(2)
        )
        r = PnlStateBuilder(fake_settings, _cfg()).read()
        assert r.consecutive_losing_days == 2
        assert r.size_multiplier == Decimal("1")
        assert r.halted is False


def test_throttle_at_cap_halves_size(fake_settings):
    """3 losing days at cap=3 → multiplier 0.5, not halted."""
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=_equity_series_with_streak(3)
        )
        r = PnlStateBuilder(fake_settings, _cfg()).read()
        assert r.consecutive_losing_days == 3
        assert r.size_multiplier == Decimal("0.5")
        assert r.halted is False


def test_throttle_one_over_cap_quarters_size(fake_settings):
    """4 losing days = cap+1 → multiplier 0.25, still not halted."""
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=_equity_series_with_streak(4)
        )
        r = PnlStateBuilder(fake_settings, _cfg()).read()
        assert r.consecutive_losing_days == 4
        assert r.size_multiplier == Decimal("0.25")
        assert r.halted is False


def test_throttle_two_over_cap_halts(fake_settings):
    """5 losing days = cap+2 → halted=True (manual reset required)."""
    with patch("trading_bot.pnl_state.TradingClient") as MockTC:
        MockTC.return_value.get_portfolio_history.return_value = MagicMock(
            equity=_equity_series_with_streak(5)
        )
        r = PnlStateBuilder(fake_settings, _cfg()).read()
        assert r.consecutive_losing_days == 5
        assert r.halted is True
        assert "consecutive losing days" in r.halt_reason
