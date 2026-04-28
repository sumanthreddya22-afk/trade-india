"""Fallback flag read/write helpers. The 'current' flag is the most-recent
row in `fallback_flags`. Append-only — full audit trail."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.state_db import FallbackFlag


def current_flag(session: Session) -> FallbackFlag | None:
    return (
        session.query(FallbackFlag)
        .order_by(desc(FallbackFlag.set_at))
        .first()
    )


def is_fallback_active(engine) -> bool:
    """Cheap helper for scanners to short-circuit. Returns False if no flag exists yet."""
    with Session(engine) as s:
        row = current_flag(s)
    return bool(row) and bool(row.fallback_active)


def set_flag(
    session: Session,
    *,
    fallback_active: bool,
    set_by: str,
    reason: str | None = None,
) -> FallbackFlag:
    row = FallbackFlag(
        fallback_active=1 if fallback_active else 0,
        set_at=dt.datetime.now(dt.timezone.utc),
        set_by=set_by,
        reason=reason,
    )
    session.add(row)
    session.commit()
    return row


def bootstrap_if_empty(session: Session) -> None:
    """Insert a fallback_active=0 row if the table is empty. Called once on boot."""
    existing = session.query(FallbackFlag).count()
    if existing == 0:
        set_flag(
            session,
            fallback_active=False,
            set_by="bootstrap",
            reason="initial state",
        )
