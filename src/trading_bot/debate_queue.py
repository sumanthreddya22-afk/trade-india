"""Phase G — DebateQueue: priority-cap replacement for hard daily caps.

Pre-Phase-G the entry/scout/hold debate caps silently dropped the 51st
candidate of the day. Replacement: queue all candidates with their
priority_score throughout the day; the dispatcher consumes the top-N
up to the daily cap. Demoted (deferred) rows roll over to the next tick.

Sequential processing: the dispatcher pops one row at a time, runs its
debate, marks the outcome. No parallel debates.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

from sqlalchemy import desc as _desc, func
from sqlalchemy.orm import Session

from trading_bot.state_db import DebateQueue


log = logging.getLogger(__name__)


# Demote rows older than this — the catalyst is stale and dispatching
# late wouldn't add value.
DEFAULT_QUEUE_TTL_HOURS = 24


def enqueue(
    engine,
    *,
    debate_class: str,
    symbol: str,
    asset_class: str,
    priority_score: float,
    payload: dict | None = None,
    now: dt.datetime | None = None,
) -> int:
    """Add a candidate to the queue. Returns the row id.

    Idempotency: not enforced at the SQL layer (an item legitimately
    queues twice if its priority changes); caller can dedupe by checking
    the queue first.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
    row = DebateQueue(
        debate_class=debate_class, symbol=symbol, asset_class=asset_class,
        priority_score=float(priority_score), payload_json=payload_json,
        queued_at=now,
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return int(row.id)


def top_n_unprocessed(
    engine,
    *,
    debate_class: str,
    n: int,
    ttl_hours: float = DEFAULT_QUEUE_TTL_HOURS,
    now: dt.datetime | None = None,
) -> list[DebateQueue]:
    """Return the top-N unprocessed rows for a debate class, ordered by
    priority_score desc. Skips rows older than ``ttl_hours``.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=ttl_hours)
    with Session(engine) as session:
        rows = (
            session.query(DebateQueue)
            .filter(DebateQueue.debate_class == debate_class)
            .filter(DebateQueue.processed_at.is_(None))
            .filter(DebateQueue.queued_at >= cutoff)
            .order_by(_desc(DebateQueue.priority_score))
            .limit(n)
            .all()
        )
        for r in rows:
            session.expunge(r)
    return rows


def mark_outcome(
    engine,
    *,
    row_id: int,
    outcome: str,
    now: dt.datetime | None = None,
) -> None:
    """Set processed_at + outcome on a queue row.
    Outcomes: ``processed`` | ``demoted`` | ``expired``.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        row = session.get(DebateQueue, row_id)
        if row is None:
            return
        row.processed_at = now
        row.outcome = outcome
        session.commit()


def expire_stale(
    engine,
    *,
    ttl_hours: float = DEFAULT_QUEUE_TTL_HOURS,
    now: dt.datetime | None = None,
) -> int:
    """Mark rows older than ``ttl_hours`` as outcome='expired' so they
    don't clog future top-N queries.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=ttl_hours)
    with Session(engine) as session:
        n = (
            session.query(DebateQueue)
            .filter(DebateQueue.processed_at.is_(None))
            .filter(DebateQueue.queued_at < cutoff)
            .update(
                {"processed_at": now, "outcome": "expired"},
                synchronize_session=False,
            )
        )
        session.commit()
    return int(n or 0)


def queue_depth(engine, *, debate_class: str) -> int:
    """Count of unprocessed (not-yet-debated) rows in the queue."""
    with Session(engine) as session:
        n = (
            session.query(func.count(DebateQueue.id))
            .filter(DebateQueue.debate_class == debate_class)
            .filter(DebateQueue.processed_at.is_(None))
            .scalar()
        )
    return int(n or 0)
