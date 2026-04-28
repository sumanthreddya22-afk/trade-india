"""Tests for src/trading_bot/position_protection.py — open-position auto-protect."""
from __future__ import annotations

from decimal import Decimal

import pytest


def test_decide_protect_when_stop_below_current():
    """Stop level computed via max(EMA20, last_close*(1-stop_pct)) is below
    current price → place a stop, don't flatten."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=100.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "protect"
    # stop = max(95, 100*0.95) = max(95, 95) = 95 — equality goes to PROTECT here
    # because the comparison is stop < current (95 < 100 → True).
    assert stop == pytest.approx(95.0)


def test_decide_flatten_when_ema_above_current():
    """Price below EMA-20 → strategy stop sits above current → flatten."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=90.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "flatten"
    assert stop == pytest.approx(95.0)


def test_decide_pct_stop_wins_when_ema_far_below():
    """When EMA-20 is well below the % floor, the % floor is the stop."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=100.0, ema_20=50.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "protect"
    assert stop == pytest.approx(95.0)  # 100 * 0.95


def test_decide_boundary_equality_goes_to_flatten():
    """Spec: boundary case (stop == current) is FLATTEN. The check is `stop < current`."""
    from trading_bot.position_protection import _decide
    decision, _stop = _decide(
        current_price=95.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    # 95*(1-0.05)=90.25; max(95, 90.25)=95 == current → flatten
    assert decision == "flatten"
