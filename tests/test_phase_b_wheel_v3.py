"""Phase B — multi-underlying Wheel v3."""
from __future__ import annotations

import datetime as dt

from trading_bot.strategies.spy_wheel_v3 import evaluate_strategy
from trading_bot.strategies.spy_wheel_v3.state_machine import (
    WheelState, current_state, snapshot_underlying,
)


def test_state_machine_per_underlying() -> None:
    positions = [
        {"symbol": "SPY", "qty": 200, "asset_class": "us_equity"},
        {"symbol": "QQQ", "qty": 50, "asset_class": "us_equity"},
        {"symbol": "IWM", "qty": 0, "asset_class": "us_equity"},
    ]
    assert current_state(positions, "SPY") == WheelState.LONG_STOCK
    assert current_state(positions, "QQQ") == WheelState.FLAT
    assert current_state(positions, "IWM") == WheelState.FLAT


def test_state_machine_short_put_detected() -> None:
    # OCC ticker: SPY 2026-05-16 P 450 → SPY260516P00450000
    positions = [
        {"symbol": "SPY260516P00450000", "qty": -1, "asset_class": "us_option"},
    ]
    assert current_state(positions, "SPY") == WheelState.SHORT_PUT_OPEN


def test_state_machine_short_call_with_shares() -> None:
    positions = [
        {"symbol": "QQQ", "qty": 100, "asset_class": "us_equity"},
        {"symbol": "QQQ260516C00400000", "qty": -1, "asset_class": "us_option"},
    ]
    assert current_state(positions, "QQQ") == WheelState.SHORT_CALL_OPEN


def test_wheel_v3_falls_back_without_fetcher() -> None:
    # No asset_fetcher, no chain fetcher → returns underlyings list + empty intents.
    result = evaluate_strategy(decision_date=dt.date(2026, 5, 15))
    assert isinstance(result.underlyings, tuple)
    assert all(isinstance(u, str) for u in result.underlyings)
    # Without an option chain fetcher we can't emit intents.
    assert result.intents == []


def test_wheel_v3_should_rebalance_daily() -> None:
    from trading_bot.strategies.spy_wheel_v3 import should_rebalance_today
    today = dt.date(2026, 5, 15)
    assert should_rebalance_today(today, None) is True
    assert should_rebalance_today(today, today - dt.timedelta(days=1)) is True
