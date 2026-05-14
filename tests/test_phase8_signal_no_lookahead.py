"""CRITICAL: the signal_fn must never use a bar dated after decision_date.

This is the single most important test in the backtest pipeline. A
look-ahead bug here would produce 'validated' strategies that don't
work in production.
"""
from __future__ import annotations

import datetime as dt
import pytest

from trading_bot.research.historical_bars import DailyBar
from trading_bot.strategies.etf_momentum_v1.signal import (
    DEFAULT_PARAMS, signal_fn,
)


def _make_bars(symbol: str, start: dt.date, n_days: int, base_close: float = 100.0):
    return [
        DailyBar(symbol=symbol, bar_date=start + dt.timedelta(days=i),
                 open=base_close, high=base_close * 1.01, low=base_close * 0.99,
                 close=base_close * (1 + i * 0.001),    # gentle uptrend
                 volume=1_000_000)
        for i in range(n_days)
    ]


def test_signal_does_not_use_future_bars():
    """Build history where bars AFTER decision_date are huge spikes.
    If the signal uses those, it would pick the spiky symbol; it shouldn't."""
    start = dt.date(2024, 1, 1)
    end = dt.date(2025, 1, 1)
    decision = dt.date(2024, 12, 1)

    # Build a "clean" series for SPY (small uptrend) ending at decision.
    spy_clean = _make_bars("SPY", start, 365, base_close=100.0)
    # Now ADD bars after decision with a huge spike — these must not be used.
    spy_with_future = spy_clean + [
        DailyBar(symbol="SPY", bar_date=decision + dt.timedelta(days=i),
                 open=100, high=1000, low=99, close=1000, volume=1)
        for i in range(1, 31)
    ]
    # And TROUGH for QQQ post-decision to test the other side.
    qqq_clean = _make_bars("QQQ", start, 365, base_close=100.0)
    qqq_with_future = qqq_clean + [
        DailyBar(symbol="QQQ", bar_date=decision + dt.timedelta(days=i),
                 open=100, high=101, low=0.1, close=0.5, volume=1)
        for i in range(1, 31)
    ]
    history = {"SPY": spy_with_future, "QQQ": qqq_with_future}

    # Signal as of decision must produce the same weights as if we had
    # only the pre-decision bars (look-ahead-free).
    weights_with_future = signal_fn(
        history, decision, universe=("SPY", "QQQ"), params=DEFAULT_PARAMS,
    )
    history_clean = {"SPY": spy_clean, "QQQ": qqq_clean}
    weights_clean = signal_fn(
        history_clean, decision, universe=("SPY", "QQQ"), params=DEFAULT_PARAMS,
    )
    assert weights_with_future == weights_clean, (
        "look-ahead detected: weights changed when future bars were added"
    )


def test_signal_abstains_with_insufficient_history():
    """If a symbol has < min_history_days of bars, no weight is assigned."""
    start = dt.date(2024, 1, 1)
    short = _make_bars("SPY", start, 100, base_close=100.0)
    weights = signal_fn(
        {"SPY": short}, decision_date=dt.date(2024, 5, 1),
        universe=("SPY",), params=DEFAULT_PARAMS,
    )
    assert weights == {}


def test_signal_only_buys_positive_momentum():
    """A symbol with negative 12-1 return must not appear."""
    start = dt.date(2020, 1, 1)
    n_days = 365 * 2
    # Falling close: starts at 200, falls 0.1/day → ends near 127
    bars_falling = [
        DailyBar(symbol="DOWN", bar_date=start + dt.timedelta(days=i),
                 open=200, high=200, low=190, close=200 - i * 0.1, volume=1_000)
        for i in range(n_days)
    ]
    weights = signal_fn(
        {"DOWN": bars_falling},
        decision_date=start + dt.timedelta(days=n_days - 1),
        universe=("DOWN",), params=DEFAULT_PARAMS,
    )
    assert "DOWN" not in weights


def test_signal_top_n_equal_weight():
    """Top N winners get equal weight summing to ≤ 1.0."""
    start = dt.date(2020, 1, 1)
    n_days = 365 * 2
    # 5 symbols with different upward trends.
    symbols = ("A", "B", "C", "D", "E")
    history = {}
    for i, sym in enumerate(symbols):
        slope = 0.001 * (i + 1)    # different growth rates
        history[sym] = [
            DailyBar(symbol=sym, bar_date=start + dt.timedelta(days=k),
                     open=100, high=100, low=100,
                     close=100 * (1 + k * slope), volume=1_000)
            for k in range(n_days)
        ]
    params = {**DEFAULT_PARAMS, "top_n": 3}
    weights = signal_fn(
        history, decision_date=start + dt.timedelta(days=n_days - 1),
        universe=symbols, params=params,
    )
    assert len(weights) == 3
    assert all(abs(w - 1.0 / 3.0) < 1e-9 for w in weights.values())
    # Winners should be the top 3 by growth rate (C, D, E).
    assert set(weights.keys()) == {"C", "D", "E"}
