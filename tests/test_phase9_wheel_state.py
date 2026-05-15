"""Wheel state machine."""
from __future__ import annotations

from trading_bot.strategies.spy_wheel_v1.state_machine import (
    WheelState, advance_state, current_state, snapshot_positions,
)


def test_flat_when_no_positions():
    assert current_state([]) == WheelState.FLAT


def test_flat_when_only_unrelated_positions():
    positions = [
        {"symbol": "QQQ", "qty": 50, "asset_class": "us_equity"},
        {"symbol": "BTCUSD", "qty": 0.01, "asset_class": "crypto"},
    ]
    assert current_state(positions) == WheelState.FLAT


def test_short_put_open_when_short_spy_put_held():
    positions = [
        # OCC ticker for SPY $420 put expiring 2026-06-19
        {"symbol": "SPY260619P00420000", "qty": -1, "asset_class": "us_option"},
    ]
    assert current_state(positions) == WheelState.SHORT_PUT_OPEN


def test_long_stock_when_100_shares_no_call():
    positions = [
        {"symbol": "SPY", "qty": 100, "asset_class": "us_equity"},
    ]
    assert current_state(positions) == WheelState.LONG_STOCK


def test_short_call_open_when_shares_and_short_call():
    positions = [
        {"symbol": "SPY", "qty": 100, "asset_class": "us_equity"},
        {"symbol": "SPY260619C00450000", "qty": -1, "asset_class": "us_option"},
    ]
    assert current_state(positions) == WheelState.SHORT_CALL_OPEN


def test_advance_state_put_otm_returns_flat():
    s = advance_state(state=WheelState.SHORT_PUT_OPEN, put_expired_otm=True)
    assert s == WheelState.FLAT


def test_advance_state_put_assigned_returns_long_stock():
    s = advance_state(state=WheelState.SHORT_PUT_OPEN, put_assigned=True)
    assert s == WheelState.LONG_STOCK


def test_advance_state_call_otm_returns_long_stock():
    s = advance_state(state=WheelState.SHORT_CALL_OPEN, call_expired_otm=True)
    assert s == WheelState.LONG_STOCK


def test_advance_state_call_assigned_returns_flat():
    s = advance_state(state=WheelState.SHORT_CALL_OPEN, call_assigned=True)
    assert s == WheelState.FLAT


def test_snapshot_collapses_multi_lot_shares():
    positions = [
        {"symbol": "SPY", "qty": 100, "asset_class": "us_equity"},
        {"symbol": "SPY", "qty": 50, "asset_class": "us_equity"},
    ]
    snap = snapshot_positions(positions)
    assert snap.spy_shares == 150
