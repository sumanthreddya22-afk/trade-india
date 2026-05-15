"""Dual Momentum SPY-vs-TLT signal."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.research.historical_bars import DailyBar
from trading_bot.strategies.dual_momentum_v1 import signal_fn, DEFAULT_PARAMS


def _flat_bars(symbol: str, start: dt.date, days: int, close: float):
    return [
        DailyBar(symbol=symbol, bar_date=start + dt.timedelta(days=i),
                 open=close, high=close, low=close, close=close, volume=1)
        for i in range(days)
    ]


def _trending_bars(symbol: str, start: dt.date, days: int, daily_pct: float):
    bars = []
    price = 100.0
    for i in range(days):
        bars.append(DailyBar(
            symbol=symbol, bar_date=start + dt.timedelta(days=i),
            open=price, high=price, low=price, close=price, volume=1,
        ))
        price *= (1 + daily_pct)
    return bars


def test_winner_is_strictly_max_return():
    start = dt.date(2024, 1, 1)
    history = {
        "SPY": _trending_bars("SPY", start, 200, 0.002),
        "TLT": _trending_bars("TLT", start, 200, 0.0005),
    }
    w = signal_fn(
        history, decision_date=start + dt.timedelta(days=199),
        params=DEFAULT_PARAMS,
    )
    assert w == {"SPY": 1.0}


def test_rotates_to_tlt_when_spy_falls():
    start = dt.date(2024, 1, 1)
    history = {
        "SPY": _trending_bars("SPY", start, 200, -0.002),
        "TLT": _trending_bars("TLT", start, 200, 0.0005),
    }
    w = signal_fn(
        history, decision_date=start + dt.timedelta(days=199),
        params=DEFAULT_PARAMS,
    )
    assert w == {"TLT": 1.0}


def test_returns_empty_with_insufficient_history():
    start = dt.date(2024, 1, 1)
    history = {
        "SPY": _flat_bars("SPY", start, 30, 100),
        "TLT": _flat_bars("TLT", start, 30, 100),
    }
    w = signal_fn(history, decision_date=start + dt.timedelta(days=29))
    assert w == {}


def test_no_lookahead():
    """Bars after decision_date must not change the winner."""
    start = dt.date(2024, 1, 1)
    spy_base = _trending_bars("SPY", start, 200, 0.001)
    tlt_base = _trending_bars("TLT", start, 200, 0.002)
    spy_with_future = spy_base + [
        DailyBar(symbol="SPY", bar_date=start + dt.timedelta(days=200 + i),
                 open=1000, high=1000, low=1000, close=1000, volume=1)
        for i in range(30)
    ]
    decision = start + dt.timedelta(days=199)
    base = signal_fn({"SPY": spy_base, "TLT": tlt_base}, decision)
    spike = signal_fn({"SPY": spy_with_future, "TLT": tlt_base}, decision)
    assert base == spike
