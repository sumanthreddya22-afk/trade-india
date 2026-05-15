"""Crypto Momentum BTC/ETH signal + runner."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.research.historical_bars import DailyBar
from trading_bot.strategies.crypto_momentum_v1 import signal_fn, DEFAULT_PARAMS


def _trending(sym: str, start: dt.date, days: int, daily_pct: float):
    bars = []
    price = 30_000.0 if sym.startswith("BTC") else 2_000.0
    for i in range(days):
        bars.append(DailyBar(symbol=sym, bar_date=start + dt.timedelta(days=i),
                              open=price, high=price, low=price, close=price,
                              volume=1))
        price *= (1 + daily_pct)
    return bars


def test_btc_wins_when_outperforming():
    start = dt.date(2024, 1, 1)
    history = {
        "BTC/USD": _trending("BTC/USD", start, 200, 0.005),
        "ETH/USD": _trending("ETH/USD", start, 200, 0.002),
    }
    w = signal_fn(history, decision_date=start + dt.timedelta(days=199))
    assert "BTC/USD" in w
    assert w["BTC/USD"] == 1.0


def test_no_buy_when_both_negative():
    start = dt.date(2024, 1, 1)
    history = {
        "BTC/USD": _trending("BTC/USD", start, 200, -0.005),
        "ETH/USD": _trending("ETH/USD", start, 200, -0.003),
    }
    w = signal_fn(history, decision_date=start + dt.timedelta(days=199))
    assert w == {}


def test_eth_wins_when_outperforming():
    start = dt.date(2024, 1, 1)
    history = {
        "BTC/USD": _trending("BTC/USD", start, 200, 0.001),
        "ETH/USD": _trending("ETH/USD", start, 200, 0.005),
    }
    w = signal_fn(history, decision_date=start + dt.timedelta(days=199))
    assert "ETH/USD" in w
