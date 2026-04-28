"""Equity high-water mark tracker. The HWM only advances; it never decreases.
Drawdown is computed as (HWM - current) / HWM, expressed as a positive percentage.
Returns 0.0 when current >= HWM or when no HWM has been recorded yet.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.state_db import EquityHighWaterMark


def current_hwm(session: Session, *, account: str) -> float | None:
    row = (
        session.query(EquityHighWaterMark)
        .filter_by(account=account)
        .order_by(desc(EquityHighWaterMark.equity))
        .first()
    )
    return row.equity if row else None


def update_hwm(session: Session, *, account: str, equity: float) -> None:
    """Record a new HWM only if equity exceeds the current HWM. No-op otherwise."""
    existing = current_hwm(session, account=account)
    if existing is not None and equity <= existing:
        return
    session.add(
        EquityHighWaterMark(
            account=account,
            equity=equity,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.commit()


def drawdown_pct(session: Session, *, account: str, current_equity: float) -> float:
    hwm = current_hwm(session, account=account)
    if hwm is None or current_equity >= hwm:
        return 0.0
    return (hwm - current_equity) / hwm * 100.0
