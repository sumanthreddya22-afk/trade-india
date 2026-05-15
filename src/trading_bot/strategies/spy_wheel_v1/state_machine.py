"""Wheel state machine — derives current state from ledger + positions.

States:
  * **FLAT**          — no SPY shares, no open SPY options. Sell put weekly.
  * **SHORT_PUT_OPEN**— short SPY put open, waiting for expiry.
  * **LONG_STOCK**    — own 100 (or N×100) SPY shares from assignment.
                        Sell covered calls weekly.
  * **SHORT_CALL_OPEN**— short SPY call open against shares, waiting for expiry.

State is inferred from the live broker position vector at decision
time, not stored in a separate table. This is intentional: the ledger
is the source of truth, and any state-machine bug shows up as a
mismatch between "what the wheel thinks" and "what the broker shows."
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable


SPY = "SPY"


class WheelState(str, enum.Enum):
    FLAT = "flat"
    SHORT_PUT_OPEN = "short_put_open"
    LONG_STOCK = "long_stock"
    SHORT_CALL_OPEN = "short_call_open"


@dataclass(frozen=True)
class WheelPositionSnapshot:
    spy_shares: float = 0.0
    short_puts: int = 0           # contracts (positive count, even though short)
    short_calls: int = 0


def _is_spy_share(p: dict) -> bool:
    """Either symbol == 'SPY' as an equity; not an option contract."""
    sym = p.get("symbol", "")
    asset_class = (p.get("asset_class") or "").lower()
    return sym.upper() == SPY and (
        "equity" in asset_class or asset_class in ("us_equity", "stock", "")
    )


def _is_spy_put(p: dict) -> bool:
    sym = p.get("symbol", "").upper()
    # OCC-style ticker: e.g. SPY250516P00450000. Contains 'P' after the date.
    return (
        sym.startswith(SPY) and len(sym) > 15
        and any(c == "P" for c in sym[6:15])
    )


def _is_spy_call(p: dict) -> bool:
    sym = p.get("symbol", "").upper()
    return (
        sym.startswith(SPY) and len(sym) > 15
        and any(c == "C" for c in sym[6:15])
    )


def _short_contract_count(p: dict) -> int:
    """For options, qty is negative when short (broker convention).
    Returns the positive contract count when short, 0 otherwise."""
    qty = float(p.get("qty", 0))
    if qty < 0:
        return int(round(-qty))
    return 0


def snapshot_positions(positions: Iterable[dict]) -> WheelPositionSnapshot:
    """Collapse the broker's position rows into the wheel-relevant view."""
    shares = 0.0
    puts = 0
    calls = 0
    for p in positions:
        if _is_spy_share(p):
            shares += float(p.get("qty", 0))
        elif _is_spy_put(p):
            puts += _short_contract_count(p)
        elif _is_spy_call(p):
            calls += _short_contract_count(p)
    return WheelPositionSnapshot(
        spy_shares=shares, short_puts=puts, short_calls=calls,
    )


def current_state(positions: Iterable[dict]) -> WheelState:
    """Infer the current wheel state.

    Priority order:
      1. If a SPY call is open AND we hold ≥ 100 shares → SHORT_CALL_OPEN.
      2. If we hold ≥ 100 shares (no open call) → LONG_STOCK.
      3. If a SPY put is open → SHORT_PUT_OPEN.
      4. Else → FLAT.
    """
    snap = snapshot_positions(positions)
    if snap.short_calls > 0 and snap.spy_shares >= 100:
        return WheelState.SHORT_CALL_OPEN
    if snap.spy_shares >= 100:
        return WheelState.LONG_STOCK
    if snap.short_puts > 0:
        return WheelState.SHORT_PUT_OPEN
    return WheelState.FLAT


def advance_state(
    *, state: WheelState, put_expired_otm: bool = False,
    put_assigned: bool = False, call_expired_otm: bool = False,
    call_assigned: bool = False,
) -> WheelState:
    """Pure-function state transition. Returns the next state given
    the resolution of any open contract."""
    if state == WheelState.SHORT_PUT_OPEN:
        if put_assigned:
            return WheelState.LONG_STOCK
        if put_expired_otm:
            return WheelState.FLAT
        return state
    if state == WheelState.SHORT_CALL_OPEN:
        if call_assigned:
            return WheelState.FLAT
        if call_expired_otm:
            return WheelState.LONG_STOCK
        return state
    # FLAT and LONG_STOCK are "decision" states — the runner moves us
    # to SHORT_PUT_OPEN / SHORT_CALL_OPEN by submitting the order.
    return state


__all__ = [
    "SPY", "WheelPositionSnapshot", "WheelState",
    "advance_state", "current_state", "snapshot_positions",
]
