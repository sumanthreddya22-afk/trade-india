"""Wheel state machine v3 — multi-underlying.

Generalises spy_wheel_v1's state machine to any underlying symbol. Each
underlying has its own (FLAT, SHORT_PUT_OPEN, LONG_STOCK,
SHORT_CALL_OPEN) state derived from the broker position vector.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable, Mapping


class WheelState(str, enum.Enum):
    FLAT = "flat"
    SHORT_PUT_OPEN = "short_put_open"
    LONG_STOCK = "long_stock"
    SHORT_CALL_OPEN = "short_call_open"


@dataclass(frozen=True)
class UnderlyingPositionSnapshot:
    underlying: str
    shares: float = 0.0
    short_puts: int = 0
    short_calls: int = 0


def _underlying_share_row(p: dict, symbol: str) -> bool:
    sym = (p.get("symbol", "") or "").upper()
    asset_class = (p.get("asset_class") or "").lower()
    return sym == symbol.upper() and (
        "equity" in asset_class or asset_class in ("us_equity", "stock", "")
    )


def _underlying_option_row(p: dict, symbol: str, *, side: str) -> bool:
    """Detect an OCC-style option ticker for ``symbol`` of ``side``
    (``"P"`` or ``"C"``). The OCC layout is
    ``{ROOT}{YY}{MM}{DD}{P|C}{STRIKE×1000:08d}``.
    """
    sym = (p.get("symbol", "") or "").upper()
    root = symbol.upper()
    if not sym.startswith(root):
        return False
    if len(sym) <= len(root) + 9:
        return False
    suffix = sym[len(root):]
    # The 7th char of the suffix is the put/call indicator in the
    # standard OCC layout (after YYMMDD).
    if len(suffix) <= 6:
        return False
    return suffix[6:7] == side


def _short_contract_count(p: dict) -> int:
    qty = float(p.get("qty", 0) or 0)
    return int(round(-qty)) if qty < 0 else 0


def snapshot_underlying(
    positions: Iterable[dict], underlying: str,
) -> UnderlyingPositionSnapshot:
    shares = 0.0
    puts = 0
    calls = 0
    for p in positions:
        if _underlying_share_row(p, underlying):
            shares += float(p.get("qty", 0) or 0)
        elif _underlying_option_row(p, underlying, side="P"):
            puts += _short_contract_count(p)
        elif _underlying_option_row(p, underlying, side="C"):
            calls += _short_contract_count(p)
    return UnderlyingPositionSnapshot(
        underlying=underlying, shares=shares,
        short_puts=puts, short_calls=calls,
    )


def current_state(
    positions: Iterable[dict], underlying: str,
) -> WheelState:
    snap = snapshot_underlying(positions, underlying)
    if snap.short_calls > 0 and snap.shares >= 100:
        return WheelState.SHORT_CALL_OPEN
    if snap.shares >= 100:
        return WheelState.LONG_STOCK
    if snap.short_puts > 0:
        return WheelState.SHORT_PUT_OPEN
    return WheelState.FLAT


def snapshot_all_underlyings(
    positions: Iterable[dict], underlyings: Iterable[str],
) -> Mapping[str, UnderlyingPositionSnapshot]:
    pos_list = list(positions)
    return {
        u: snapshot_underlying(pos_list, u) for u in underlyings
    }


__all__ = [
    "UnderlyingPositionSnapshot",
    "WheelState",
    "current_state",
    "snapshot_all_underlyings",
    "snapshot_underlying",
]
