"""Data router asset-class routing."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from trading_bot.ingest import data_router


def test_empty_symbols_short_circuits():
    out = data_router.fetch_daily_bars(
        symbols=(), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="us_equity",
    )
    assert out == []


def test_us_equity_routes_to_yfinance(monkeypatch):
    called = {}
    def fake_yf(*, symbols, start, end):
        called["yf"] = (symbols, start, end)
        return ["fake"]
    monkeypatch.setattr(data_router, "_fetch_stock_bars_yfinance", fake_yf)
    out = data_router.fetch_daily_bars(
        symbols=("SPY",), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="us_equity",
    )
    assert out == ["fake"]
    assert called["yf"] == (("SPY",), dt.date(2024, 1, 1), dt.date(2024, 1, 2))


def test_crypto_routes_to_alpaca(monkeypatch):
    called = {}
    def fake_alp(*, symbols, start, end):
        called["alp"] = (symbols, start, end)
        return ["fake"]
    monkeypatch.setattr(data_router, "_fetch_crypto_bars_alpaca", fake_alp)
    out = data_router.fetch_daily_bars(
        symbols=("BTC/USD",), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="crypto",
    )
    assert out == ["fake"]
    assert called["alp"] == (("BTC/USD",), dt.date(2024, 1, 1), dt.date(2024, 1, 2))
