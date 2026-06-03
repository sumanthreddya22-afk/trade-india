"""Data router asset-class routing — India-first (NSE/BSE via yfinance)."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from trading_bot.ingest import data_router


def test_empty_symbols_short_circuits():
    out = data_router.fetch_daily_bars(
        symbols=(), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="nse_equity",
    )
    assert out == []


def test_nse_equity_routes_to_yfinance(monkeypatch):
    """nse_equity (and its alias us_equity) should route to _fetch_nse_bars_yfinance."""
    called = {}
    def fake_yf(*, symbols, start, end):
        called["yf"] = (symbols, start, end)
        return ["fake"]
    monkeypatch.setattr(data_router, "_fetch_nse_bars_yfinance", fake_yf)
    out = data_router.fetch_daily_bars(
        symbols=("RELIANCE",), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="nse_equity",
    )
    assert out == ["fake"]
    assert called["yf"] == (("RELIANCE",), dt.date(2024, 1, 1), dt.date(2024, 1, 2))


def test_us_equity_alias_routes_to_nse(monkeypatch):
    """us_equity is an alias for nse_equity — same code path."""
    called = {}
    def fake_yf(*, symbols, start, end):
        called["yf"] = True
        return ["fake"]
    monkeypatch.setattr(data_router, "_fetch_nse_bars_yfinance", fake_yf)
    out = data_router.fetch_daily_bars(
        symbols=("NIFTYBEES",), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="us_equity",
    )
    assert out == ["fake"]
    assert called["yf"]


def test_crypto_routes_to_yfinance(monkeypatch):
    """crypto / crypto_inr should route to _fetch_crypto_bars_yfinance."""
    called = {}
    def fake_crypto(*, symbols, start, end):
        called["crypto"] = (symbols, start, end)
        return ["fake"]
    monkeypatch.setattr(data_router, "_fetch_crypto_bars_yfinance", fake_crypto)
    out = data_router.fetch_daily_bars(
        symbols=("BTC/INR",), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="crypto",
    )
    assert out == ["fake"]
    assert called["crypto"] == (("BTC/INR",), dt.date(2024, 1, 1), dt.date(2024, 1, 2))


def test_crypto_inr_alias(monkeypatch):
    """crypto_inr is an explicit alias for crypto."""
    called = {}
    def fake_crypto(*, symbols, start, end):
        called["hit"] = True
        return []
    monkeypatch.setattr(data_router, "_fetch_crypto_bars_yfinance", fake_crypto)
    data_router.fetch_daily_bars(
        symbols=("ETH/INR",), start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 2),
        asset_class="crypto_inr",
    )
    assert called["hit"]
