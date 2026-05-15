"""Wheel backtest-lite — runs over synthetic SPY bars."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from trading_bot.research.historical_bars import DailyBar
from trading_bot.strategies.spy_wheel_v1.backtest_lite import (
    run_wheel_backtest,
)


def _bars(start: dt.date, days: int, daily_pct: float, base: float = 400):
    bars = []
    price = base
    for i in range(days):
        bars.append(DailyBar(
            symbol="SPY", bar_date=start + dt.timedelta(days=i),
            open=price, high=price * 1.01, low=price * 0.99,
            close=price, volume=1_000_000,
        ))
        price *= (1 + daily_pct + ((i % 11) - 5) * 0.0005)
    return bars


def test_wheel_produces_trades_over_year():
    """Run a year of SPY with mild uptrend; cycle is monthly (~12/year).

    The runner picks the next 30-DTE Friday; after each cycle resolves we
    advance to the following Monday, so realistic cadence is ~4 weeks.
    """
    start = dt.date(2024, 1, 1)
    bars = _bars(start, 365, 0.0003, base=400.0)
    result = run_wheel_backtest(
        bars=bars, start=start, end=start + dt.timedelta(days=360),
        starting_equity=100_000.0,
    )
    assert 8 <= result.n_trades <= 18
    assert result.starting_equity == 100_000.0
    assert result.final_equity > 0
    assert result.win_rate >= 0.0


def test_wheel_assignments_recorded():
    """A crashing market should produce assignments on the put leg."""
    start = dt.date(2024, 1, 1)
    bars = _bars(start, 365, -0.003, base=400.0)
    result = run_wheel_backtest(
        bars=bars, start=start, end=start + dt.timedelta(days=360),
    )
    assert result.n_assignments >= 1


def test_wheel_collects_premium():
    """Every trade should record some premium_collected > 0 (BS price is
    monotonic in IV, and our IV proxy is always >= 0.10)."""
    start = dt.date(2024, 1, 1)
    bars = _bars(start, 120, 0.0001, base=400.0)
    result = run_wheel_backtest(
        bars=bars, start=start, end=start + dt.timedelta(days=119),
    )
    assert all(t.premium_collected > 0 for t in result.trades)
