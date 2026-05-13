"""Phase 2 — account caps: daily DD, trailing DD, intraday floor."""
from __future__ import annotations

from trading_bot.risk.account_caps import (
    check_daily_drawdown, check_intraday_pnl_floor, check_trailing_drawdown,
)
from trading_bot.risk.limits import AccountLimits
from trading_bot.risk.types import AccountState


L = AccountLimits(
    daily_drawdown_pct=1.0, trailing_drawdown_pct=5.0,
    trailing_drawdown_window_days=60, intraday_pnl_floor_pct=-1.5,
)


def test_daily_dd_pass() -> None:
    acct = AccountState(equity=15000, cash=10000,
                        equity_at_session_start=15100, day_trade_count=0)
    d = check_daily_drawdown(acct, L)
    assert d.verdict == "accept"


def test_daily_dd_breach() -> None:
    acct = AccountState(equity=14800, cash=10000,
                        equity_at_session_start=15000, day_trade_count=0)
    d = check_daily_drawdown(acct, L)
    assert d.verdict == "halt"
    assert "daily_drawdown" in d.reason


def test_intraday_floor_pass() -> None:
    acct = AccountState(equity=14900, cash=10000,
                        equity_at_session_start=15000, day_trade_count=0)
    d = check_intraday_pnl_floor(acct, L)
    assert d.verdict == "accept"


def test_intraday_floor_breach() -> None:
    acct = AccountState(equity=14770, cash=10000,
                        equity_at_session_start=15000, day_trade_count=0)
    d = check_intraday_pnl_floor(acct, L)
    assert d.verdict == "halt"
    assert "intraday_pnl_floor" in d.reason


def test_trailing_dd_pass_small_swing() -> None:
    series = [10000, 10100, 9950, 10200, 10150]
    d = check_trailing_drawdown(series, L)
    assert d.verdict == "accept"


def test_trailing_dd_breach() -> None:
    series = [10000, 10100, 9500]      # ~5.94% peak-to-trough
    d = check_trailing_drawdown(series, L)
    assert d.verdict == "halt"


def test_trailing_dd_empty_series() -> None:
    assert check_trailing_drawdown([], L).verdict == "accept"
