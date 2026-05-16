"""Hash-chained append for ``broker_switch_event`` (WS6a).

One row recorded on every broker cutover. The reconciliation kill switch
suppresses for 24h after the most recent row here so the first night
after a switch — where ledger positions (e.g. ``source='bot'`` on
Alpaca paper) don't match the new broker's positions — doesn't fire
``recon_mismatch``.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    from_broker: str,
    to_broker: str,
    operator: str,
    reason: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append a broker_switch_event row. Returns the new ``ledger_seq``."""
    if not from_broker or not to_broker:
        raise ValueError("from_broker and to_broker are required")
    if not operator:
        raise ValueError("operator is required (git identity)")
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "broker_switch_event")
    row = {
        "event_ts": now.isoformat(),
        "from_broker": from_broker,
        "to_broker": to_broker,
        "operator": operator,
        "reason": reason,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO broker_switch_event (
            event_ts, from_broker, to_broker, operator, reason,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["from_broker"], row["to_broker"],
            row["operator"], row["reason"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


def most_recent_switch_within(
    conn: sqlite3.Connection,
    *,
    window: dt.timedelta,
    now: Optional[dt.datetime] = None,
) -> Optional[dict]:
    """Return the most recent broker_switch_event row if its ``event_ts``
    is within ``window`` of ``now``, else ``None``.

    The reconciliation kill switch calls this with ``window=24h`` to
    decide whether to suppress ``recon_mismatch`` for the first night
    after a cutover.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - window).isoformat()
    cur = conn.execute(
        """
        SELECT event_ts, from_broker, to_broker, operator, reason
        FROM broker_switch_event
        WHERE event_ts >= ?
        ORDER BY ledger_seq DESC
        LIMIT 1
        """,
        (cutoff,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "event_ts": row[0], "from_broker": row[1], "to_broker": row[2],
        "operator": row[3], "reason": row[4],
    }


__all__ = ["write_event", "most_recent_switch_within"]
