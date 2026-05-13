"""Shared dataclasses for the risk kernel.

These are the inputs and outputs of every check function. Keeping them
in one module prevents circular imports between checks.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

VerdictT = Literal["accept", "reduce", "halt"]


@dataclass(frozen=True)
class AccountState:
    """Snapshot of the broker account at intent time."""

    equity: float
    cash: float
    equity_at_session_start: float
    day_trade_count: int
    buying_power: float = 0.0


@dataclass(frozen=True)
class Position:
    """Minimal position record — what every risk check needs."""

    symbol: str
    asset_class: str                                # equity | crypto | option
    qty: float
    market_value: float
    classification: str = "unknown"
    strategy_id: Optional[str] = None
    lane: Optional[str] = None
    opened_at: Optional[dt.datetime] = None
    has_stop: bool = False


@dataclass(frozen=True)
class RiskDecision:
    """Output of a single check or of the overall precheck."""

    verdict: VerdictT
    reason: str
    adjusted_qty: Optional[float] = None
    """If verdict='reduce', the qty the kernel may submit instead of
    the requested qty. Otherwise None."""

    @classmethod
    def accept(cls, reason: str = "") -> "RiskDecision":
        return cls(verdict="accept", reason=reason)

    @classmethod
    def halt(cls, reason: str) -> "RiskDecision":
        return cls(verdict="halt", reason=reason)

    @classmethod
    def reduce(cls, reason: str, adjusted_qty: float) -> "RiskDecision":
        return cls(verdict="reduce", reason=reason, adjusted_qty=adjusted_qty)


__all__ = [
    "AccountState",
    "Position",
    "RiskDecision",
    "VerdictT",
]
