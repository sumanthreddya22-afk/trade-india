# src/trading_bot/options/wheel_state.py
"""Wheel cycle state machine. One cycle = one CSP→assigned→CC→closed lifecycle.
Persisted in `wheel_cycles` table."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.state_db import WheelCycle


class Phase(str, Enum):
    CSP_OPEN = "csp_open"
    ASSIGNED = "assigned"
    CC_OPEN = "cc_open"
    CLOSED = "closed"


_ACTIVE_PHASES = {Phase.CSP_OPEN.value, Phase.ASSIGNED.value, Phase.CC_OPEN.value}


class WheelStateRepo:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_active(self, *, symbol: str) -> WheelCycle | None:
        with Session(self.engine) as s:
            return (s.query(WheelCycle)
                    .filter(WheelCycle.symbol == symbol,
                            WheelCycle.phase.in_(_ACTIVE_PHASES))
                    .one_or_none())

    def get_by_cycle_id(self, cycle_id: str) -> WheelCycle | None:
        with Session(self.engine) as s:
            return (s.query(WheelCycle)
                    .filter(WheelCycle.cycle_id == cycle_id)
                    .one_or_none())

    def list_active(self) -> list[WheelCycle]:
        with Session(self.engine) as s:
            return (s.query(WheelCycle)
                    .filter(WheelCycle.phase.in_(_ACTIVE_PHASES))
                    .all())

    def _update(self, cycle_id: str, **fields) -> None:
        with Session(self.engine) as s:
            row = (s.query(WheelCycle).filter(WheelCycle.cycle_id == cycle_id)
                   .one_or_none())
            if row is None:
                raise ValueError(f"unknown cycle_id {cycle_id}")
            for k, v in fields.items():
                setattr(row, k, v)
            s.commit()


def _new_cycle_id() -> str:
    return f"wc_{uuid.uuid4().hex[:12]}"


def open_csp(
    repo: WheelStateRepo, *, symbol: str, contract: str,
    strike: Decimal, expiration: dt.date, credit: Decimal,
) -> str:
    """Bucket E: TOCTOU race fix. Pre-Bucket-E this opened TWO sessions —
    one to check active cycles, another to insert — leaving a window where
    a concurrent wheel_scan could pass the active-check, lose the race,
    and create duplicate active cycles for the same symbol. The check and
    insert now share a single Session so SQLite's serializable isolation
    serializes them.
    """
    cid = _new_cycle_id()
    with Session(repo.engine) as s:
        existing = (s.query(WheelCycle)
                    .filter(WheelCycle.symbol == symbol,
                            WheelCycle.phase.in_(_ACTIVE_PHASES))
                    .one_or_none())
        if existing is not None:
            raise ValueError(f"active cycle exists for {symbol}")
        s.add(WheelCycle(
            cycle_id=cid, symbol=symbol, phase=Phase.CSP_OPEN.value,
            opened_at=dt.datetime.now(dt.timezone.utc),
            csp_contract=contract, csp_strike=strike,
            csp_expiration=expiration, csp_credit=credit,
        ))
        s.commit()
    return cid


def mark_assigned(repo: WheelStateRepo, *, cycle_id: str, when: dt.datetime) -> None:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None or cyc.phase != Phase.CSP_OPEN.value:
        raise ValueError(f"cannot assign cycle {cycle_id} (phase={cyc.phase if cyc else None})")
    cost_basis = (cyc.csp_strike or Decimal(0)) - (cyc.csp_credit or Decimal(0))
    repo._update(cycle_id, phase=Phase.ASSIGNED.value, cost_basis=cost_basis)


def open_cc(
    repo: WheelStateRepo, *, cycle_id: str, contract: str,
    strike: Decimal, expiration: dt.date, credit: Decimal,
) -> None:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None or cyc.phase != Phase.ASSIGNED.value:
        raise ValueError(f"cannot open CC for cycle {cycle_id}")
    repo._update(cycle_id, phase=Phase.CC_OPEN.value,
                 cc_contract=contract, cc_strike=strike,
                 cc_expiration=expiration, cc_credit=credit)


def close_cycle(repo: WheelStateRepo, *, cycle_id: str, realized_pnl: Decimal) -> None:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None:
        raise ValueError(f"cannot close unknown cycle {cycle_id}")
    repo._update(cycle_id, phase=Phase.CLOSED.value,
                 closed_at=dt.datetime.now(dt.timezone.utc),
                 realized_pnl=realized_pnl)


def increment_rolls(repo: WheelStateRepo, *, cycle_id: str) -> int:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None:
        raise ValueError(f"unknown cycle {cycle_id}")
    new_count = (cyc.rolls_used or 0) + 1
    repo._update(cycle_id, rolls_used=new_count)
    return new_count
