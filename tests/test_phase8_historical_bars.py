"""Historical-bars store: schema + upsert + load."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.research.historical_bars import (
    DailyBar, load_bars, open_store, upsert_bars,
)


@pytest.fixture()
def store(tmp_path):
    return open_store(tmp_path / "bars.db")


def test_upsert_and_load_roundtrip(store):
    today = dt.date(2024, 1, 15)
    bars = [
        DailyBar(symbol="SPY", bar_date=today, open=400, high=401,
                 low=399, close=400.5, volume=1_000_000),
        DailyBar(symbol="QQQ", bar_date=today, open=350, high=351,
                 low=349, close=350.5, volume=2_000_000),
    ]
    n = upsert_bars(store, bars)
    assert n == 2
    out = load_bars(store, symbols=("SPY", "QQQ"),
                    start=today, end=today)
    assert len(out["SPY"]) == 1
    assert out["SPY"][0].close == 400.5
    assert out["QQQ"][0].close == 350.5


def test_upsert_replaces_existing(store):
    today = dt.date(2024, 1, 15)
    upsert_bars(store, [DailyBar("SPY", today, 100, 100, 100, 100, 1)])
    upsert_bars(store, [DailyBar("SPY", today, 200, 200, 200, 200, 2)])
    out = load_bars(store, symbols=("SPY",), start=today, end=today)
    assert out["SPY"][0].close == 200


def test_load_missing_symbol_returns_empty_list(store):
    out = load_bars(store, symbols=("NOPE",),
                    start=dt.date(2020, 1, 1), end=dt.date(2024, 1, 1))
    assert out == {"NOPE": []}
