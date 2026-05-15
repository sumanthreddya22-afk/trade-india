"""yfinance adapter tests — uses pytest.importorskip + monkeypatch so
the suite runs without network."""
from __future__ import annotations

import datetime as dt

import pytest


def test_module_imports():
    from trading_bot.ingest import yfinance_adapter   # noqa: F401


def test_find_contract_by_delta_picks_closest(monkeypatch):
    """Pure-Python helper: doesn't need yfinance, just the BS module."""
    from trading_bot.ingest.yfinance_adapter import (
        ChainSnapshot, OptionContract, find_contract_by_delta,
    )

    spot = 400.0
    today = dt.date.today()
    expiry = today + dt.timedelta(days=30)
    iv = 0.20
    # Build a few synthetic puts with varying strike.
    puts = tuple(
        OptionContract(
            underlying="SPY", expiry=expiry, strike=k, side="put",
            bid=1.0, ask=1.1, last_price=1.05, volume=10,
            open_interest=100, implied_volatility=iv, in_the_money=False,
        )
        for k in (370, 380, 390, 400, 410)
    )
    chain = ChainSnapshot(
        underlying="SPY", underlying_price=spot,
        fetched_at=dt.datetime.now(dt.timezone.utc),
        expiry=expiry, calls=(), puts=puts,
    )
    contract = find_contract_by_delta(
        chain, side="put", target_delta=0.30,
    )
    assert contract is not None
    # 0.30-delta put at 30 DTE / 20% IV is roughly 5-8% OTM → strike ~380-390.
    assert 370 <= contract.strike <= 395


def test_find_contract_by_delta_returns_none_for_empty_chain(monkeypatch):
    from trading_bot.ingest.yfinance_adapter import ChainSnapshot, find_contract_by_delta
    chain = ChainSnapshot(
        underlying="SPY", underlying_price=400.0,
        fetched_at=dt.datetime.now(dt.timezone.utc),
        expiry=dt.date.today() + dt.timedelta(days=30),
        calls=(), puts=(),
    )
    assert find_contract_by_delta(chain, side="put", target_delta=0.30) is None
