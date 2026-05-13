"""Orphan-order detection.

Plan v4 §5 idempotency contract: any order in state=submitted older than
60 seconds without ack is queried against the broker. If found, a new
order_state_event row back-fills broker_order_id and transitions
to=acked. If not found, transitions to=cancelled with
reason='orphan_recovered'.

Phase 1 ships ``find_orphans`` (read-only helper) + ``recover_orphan``
(applies the transition given a broker-result callback). The runtime
loop that drives this lands in Phase 3 alongside the Alpaca adapter
hardening.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

from trading_bot.ledger.state_event import append_state_event

DEFAULT_ORPHAN_AGE_SECONDS = 60


@dataclass(frozen=True)
class Orphan:
    order_uid: str
    client_order_id: str
    submitted_at: dt.datetime
    age_seconds: float


def find_orphans(
    conn: sqlite3.Connection,
    *,
    max_age_seconds: int = DEFAULT_ORPHAN_AGE_SECONDS,
    now: Optional[dt.datetime] = None,
) -> list[Orphan]:
    """Return every order whose current state is 'submitted' and whose
    last state event is older than ``max_age_seconds``.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=max_age_seconds)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.order_uid, m.client_order_id, c.state_ts
        FROM order_master m
        JOIN order_current c USING (order_uid)
        WHERE c.state = 'submitted'
          AND c.state_ts <= ?
        """,
        (cutoff.isoformat(),),
    )
    out: list[Orphan] = []
    for order_uid, client_order_id, state_ts_str in cur.fetchall():
        submitted_at = dt.datetime.fromisoformat(state_ts_str)
        age = (now - submitted_at).total_seconds()
        out.append(Orphan(
            order_uid=order_uid,
            client_order_id=client_order_id,
            submitted_at=submitted_at,
            age_seconds=age,
        ))
    return out


BrokerLookupT = Callable[[str], Optional[str]]
"""Caller-supplied function: takes a ``client_order_id``, returns the
broker's ``broker_order_id`` if the broker knows about it, else None."""


def recover_orphan(
    conn: sqlite3.Connection,
    orphan: Orphan,
    *,
    broker_lookup: BrokerLookupT,
    now: Optional[dt.datetime] = None,
) -> str:
    """Apply the recovery transition. Returns the new state ('acked' or
    'cancelled'). Caller holds the writer lock.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    broker_id = broker_lookup(orphan.client_order_id)
    if broker_id:
        append_state_event(
            conn,
            order_uid=orphan.order_uid,
            to_state="acked",
            from_state="submitted",
            broker_order_id=broker_id,
            reason="orphan_recovered",
            now=now,
        )
        return "acked"
    append_state_event(
        conn,
        order_uid=orphan.order_uid,
        to_state="cancelled",
        from_state="submitted",
        reason="orphan_recovered",
        now=now,
    )
    return "cancelled"


__all__ = [
    "BrokerLookupT",
    "DEFAULT_ORPHAN_AGE_SECONDS",
    "Orphan",
    "find_orphans",
    "recover_orphan",
]
