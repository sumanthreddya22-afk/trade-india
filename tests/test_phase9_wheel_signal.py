"""Wheel signal — strike helpers + occ_ticker."""
from __future__ import annotations

import datetime as dt

from trading_bot.strategies.spy_wheel_v1.signal import (
    DEFAULT_PARAMS, occ_ticker, pick_expiry,
)


def test_pick_expiry_closest_to_target():
    today = dt.date(2026, 5, 18)   # Monday
    expiries = [
        today + dt.timedelta(days=d)
        for d in (5, 14, 21, 28, 35, 49, 60, 90)
    ]
    e = pick_expiry(
        expiries, today=today, target_days=30, min_days=21, max_days=45,
    )
    # 28 days is in window and closest to 30.
    assert (e - today).days == 28


def test_pick_expiry_returns_none_if_window_empty():
    today = dt.date(2026, 5, 18)
    expiries = [today + dt.timedelta(days=d) for d in (5, 10)]
    assert pick_expiry(expiries, today=today,
                        target_days=30, min_days=21, max_days=45) is None


def test_occ_ticker_format():
    """SPY 2026-06-19 P 450.00 → SPY260619P00450000"""
    s = occ_ticker("SPY", dt.date(2026, 6, 19), "put", 450.0)
    assert s == "SPY260619P00450000"
    s2 = occ_ticker("SPY", dt.date(2026, 5, 23), "call", 425.5)
    assert s2 == "SPY260523C00425500"


def test_occ_ticker_handles_decimals():
    """Strikes can be half-dollar; encoded as ×1000 with 8 digits."""
    s = occ_ticker("AAPL", dt.date(2026, 12, 18), "call", 187.5)
    assert s == "AAPL261218C00187500"
