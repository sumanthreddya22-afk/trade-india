"""Options wheel state machine (Phase 3 scaffold).

Models the lifecycle of one wheel cycle:

  cash → csp_open → assigned → cc_open → called_away → cash
                  ↘
                   csp_expired → cash       (CSP expired worthless; back to cash)
                  ↘
                   csp_rolled               (rolled CSP for credit, stays in csp_open)

  cc_open ↘
           cc_expired → cash               (CC expired worthless, hold shares for next cycle)
          ↘
           cc_rolled                       (rolled CC for credit, stays in cc_open)

This module is the state-transition engine — pure functions over a
``WheelCycleOptions`` row. Real broker actions (submit / roll / accept
assignment) are wired in Phase 3C+ via the same submit_txn pattern
the crypto pipeline uses.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from trading_bot.pipelines.options.state_db import (
    WheelCycleOptions,
    WheelStateHistoryOptions,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class WheelState(str, Enum):
    CASH = "cash"
    CSP_OPEN = "csp_open"
    CSP_EXPIRED = "csp_expired"
    ASSIGNED = "assigned"
    CC_OPEN = "cc_open"
    CC_EXPIRED = "cc_expired"
    CALLED_AWAY = "called_away"
    CLOSED = "closed"      # cycle terminated cleanly (cash + realized_pnl set)


# Allowed transitions. Used by ``advance`` to enforce machine validity —
# any disallowed transition raises and writes nothing (defensive).
_ALLOWED_TRANSITIONS = {
    WheelState.CASH:        {WheelState.CSP_OPEN},
    WheelState.CSP_OPEN:    {WheelState.CSP_EXPIRED, WheelState.ASSIGNED, WheelState.CSP_OPEN},  # roll = stays open
    WheelState.CSP_EXPIRED: {WheelState.CLOSED, WheelState.CSP_OPEN},  # back to cash → close, or re-enter
    WheelState.ASSIGNED:    {WheelState.CC_OPEN},
    WheelState.CC_OPEN:     {WheelState.CC_EXPIRED, WheelState.CALLED_AWAY, WheelState.CC_OPEN},  # roll
    WheelState.CC_EXPIRED:  {WheelState.CC_OPEN},  # next cycle CC; if cycle ends, transition to CLOSED
    WheelState.CALLED_AWAY: {WheelState.CLOSED},
    WheelState.CLOSED:      set(),  # terminal
}


class InvalidTransition(ValueError):
    """Raised when ``advance`` is called with a disallowed (from, to) pair."""


# ---------------------------------------------------------------------------
# Cycle lifecycle
# ---------------------------------------------------------------------------


@dataclass
class CycleSnapshot:
    """Read-only view of one wheel cycle row, useful for tests + dashboards."""
    id: int
    underlying: str
    state: WheelState
    started_at: dt.datetime
    ended_at: Optional[dt.datetime]
    initial_csp_strike: Optional[float]
    assignment_share_basis: Optional[float]
    final_called_away_at: Optional[float]
    cumulative_premium: float
    realized_pnl: Optional[float]


def open_cycle(
    engine: Any,
    *,
    underlying: str,
    initial_csp_strike: float,
    target_delta_csp: float,
    target_delta_cc: float,
    now: Optional[dt.datetime] = None,
) -> int:
    """Start a new wheel cycle in the CSP_OPEN state. Returns cycle_id."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        row = WheelCycleOptions(
            underlying=underlying,
            state=WheelState.CSP_OPEN.value,
            started_at=now, ended_at=None,
            initial_csp_strike=initial_csp_strike,
            assignment_share_basis=None,
            final_called_away_at=None,
            cumulative_premium=0.0,
            realized_pnl=None,
            target_delta_csp=target_delta_csp,
            target_delta_cc=target_delta_cc,
        )
        session.add(row)
        session.commit()
        cycle_id = row.id
        # Audit: synthetic "cash → csp_open" transition row.
        session.add(WheelStateHistoryOptions(
            cycle_id=cycle_id,
            transitioned_at=now,
            from_state=WheelState.CASH.value,
            to_state=WheelState.CSP_OPEN.value,
            transition="cycle_opened_csp",
            details_json=json.dumps({
                "initial_csp_strike": initial_csp_strike,
                "target_delta_csp": target_delta_csp,
                "target_delta_cc": target_delta_cc,
            }),
        ))
        session.commit()
    return cycle_id


def advance(
    engine: Any,
    *,
    cycle_id: int,
    to_state: WheelState,
    transition: str,
    details: Optional[Dict[str, Any]] = None,
    premium_delta: float = 0.0,
    assignment_share_basis: Optional[float] = None,
    final_called_away_at: Optional[float] = None,
    realized_pnl: Optional[float] = None,
    now: Optional[dt.datetime] = None,
) -> CycleSnapshot:
    """Advance a wheel cycle to a new state. Validates transition; updates
    ``cumulative_premium`` (added not replaced) and other side fields.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        cycle = session.get(WheelCycleOptions, cycle_id)
        if cycle is None:
            raise ValueError(f"cycle {cycle_id} not found")
        from_state = WheelState(cycle.state)

        if to_state not in _ALLOWED_TRANSITIONS.get(from_state, set()):
            raise InvalidTransition(
                f"cycle {cycle_id}: {from_state.value} → {to_state.value} not allowed"
            )

        cycle.state = to_state.value
        cycle.cumulative_premium = (cycle.cumulative_premium or 0.0) + premium_delta
        if assignment_share_basis is not None:
            cycle.assignment_share_basis = assignment_share_basis
        if final_called_away_at is not None:
            cycle.final_called_away_at = final_called_away_at
        if realized_pnl is not None:
            cycle.realized_pnl = realized_pnl
        if to_state == WheelState.CLOSED:
            cycle.ended_at = now

        session.add(WheelStateHistoryOptions(
            cycle_id=cycle_id,
            transitioned_at=now,
            from_state=from_state.value,
            to_state=to_state.value,
            transition=transition,
            details_json=json.dumps(details or {}, default=str),
        ))
        session.commit()
        return _snapshot(cycle)


def close_cycle(
    engine: Any,
    *,
    cycle_id: int,
    realized_pnl: float,
    now: Optional[dt.datetime] = None,
) -> CycleSnapshot:
    """Convenience: close a cycle that's reached called_away or csp_expired."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        cycle = session.get(WheelCycleOptions, cycle_id)
        if cycle is None:
            raise ValueError(f"cycle {cycle_id} not found")
        from_state = WheelState(cycle.state)
    if from_state not in (WheelState.CALLED_AWAY, WheelState.CSP_EXPIRED):
        raise InvalidTransition(
            f"close_cycle requires from_state in (called_away, csp_expired); got {from_state.value}"
        )
    return advance(
        engine, cycle_id=cycle_id, to_state=WheelState.CLOSED,
        transition=f"cycle_closed_from_{from_state.value}",
        realized_pnl=realized_pnl, now=now,
    )


def _snapshot(cycle: WheelCycleOptions) -> CycleSnapshot:
    return CycleSnapshot(
        id=cycle.id, underlying=cycle.underlying,
        state=WheelState(cycle.state),
        started_at=cycle.started_at, ended_at=cycle.ended_at,
        initial_csp_strike=cycle.initial_csp_strike,
        assignment_share_basis=cycle.assignment_share_basis,
        final_called_away_at=cycle.final_called_away_at,
        cumulative_premium=cycle.cumulative_premium or 0.0,
        realized_pnl=cycle.realized_pnl,
    )


def history(engine: Any, *, cycle_id: int) -> list[WheelStateHistoryOptions]:
    """Return all transition rows for one cycle, oldest first."""
    with Session(engine) as session:
        rows = (
            session.query(WheelStateHistoryOptions)
            .filter(WheelStateHistoryOptions.cycle_id == cycle_id)
            .order_by(WheelStateHistoryOptions.transitioned_at.asc())
            .all()
        )
        for r in rows:
            session.expunge(r)
    return rows
