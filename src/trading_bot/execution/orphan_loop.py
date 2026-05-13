"""Orphan-recovery loop (Phase 3 wire-up of Phase 1's helper).

The daemon (Phase 5) will call ``run_once`` every 30 seconds. For Phase
3 we ship the function and its tests; the schedule arrives with the
daemon.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Callable, Optional

from trading_bot.ledger import find_orphans, recover_orphan
from trading_bot.ledger.orphan_recovery import BrokerLookupT

DEFAULT_MAX_AGE_SECONDS = 60


def run_once(
    conn: sqlite3.Connection,
    *,
    broker_lookup: BrokerLookupT,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: Optional[dt.datetime] = None,
) -> list[dict]:
    """Find every orphan and recover it. Returns a list of
    ``{order_uid, result}`` rows for telemetry."""
    now = now or dt.datetime.now(dt.timezone.utc)
    orphans = find_orphans(conn, max_age_seconds=max_age_seconds, now=now)
    out = []
    for o in orphans:
        result = recover_orphan(conn, o, broker_lookup=broker_lookup, now=now)
        out.append({"order_uid": o.order_uid, "result": result})
    return out


__all__ = ["DEFAULT_MAX_AGE_SECONDS", "run_once"]
