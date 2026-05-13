"""90-day failure memory.

Plan v4 §8 mutation discipline. A rejected hypothesis_hash blocks
re-intake for 90 days unless the thesis materially changes (which
yields a different hash).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash
from trading_bot.research.failure_memory_schema import ensure_failure_memory

DEFAULT_TTL_DAYS = 90


def record_rejection(
    conn: sqlite3.Connection,
    *,
    hypothesis_hash: str,
    reason: str,
    strategy_id: Optional[str] = None,
    tier: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    ensure_failure_memory(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "failure_memory")
    row = {
        "hypothesis_hash": hypothesis_hash,
        "rejected_ts": now.isoformat(),
        "reason": reason,
        "strategy_id": strategy_id,
        "tier": tier,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO failure_memory (
            hypothesis_hash, rejected_ts, reason, strategy_id, tier,
            prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            row["hypothesis_hash"], row["rejected_ts"], row["reason"],
            row["strategy_id"], row["tier"], prev, this_hash,
        ),
    )
    return cur.lastrowid


def is_blocked(
    conn: sqlite3.Connection,
    *,
    hypothesis_hash: str,
    now: Optional[dt.datetime] = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> tuple[bool, Optional[str]]:
    """Return (blocked, reason_if_blocked).

    Blocked if any rejection within the last ``ttl_days`` matches the
    hash. The reason returned is from the most recent rejection.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=ttl_days)).isoformat()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT reason FROM failure_memory
            WHERE hypothesis_hash = ? AND rejected_ts >= ?
            ORDER BY rejected_ts DESC
            LIMIT 1
            """,
            (hypothesis_hash, cutoff),
        )
    except sqlite3.OperationalError:
        return False, None
    row = cur.fetchone()
    if row is None:
        return False, None
    return True, row[0]


__all__ = ["DEFAULT_TTL_DAYS", "is_blocked", "record_rejection"]
