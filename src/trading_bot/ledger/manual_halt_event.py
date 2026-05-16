"""Hash-chained append for ``manual_halt_event`` (WS5f Layer 4).

PAUSE (reversible) and FLATTEN (one-way) operator actions write here.
Each row is attributable to the operator's git identity.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash

ACTIONS = ("pause", "resume", "flatten")
SOURCES = ("cockpit", "cli", "hotkey")


def write_event(
    conn: sqlite3.Connection,
    *,
    action: str,
    operator: str,
    source: str,
    reason: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if not operator:
        raise ValueError("operator is required")
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "manual_halt_event")
    row = {
        "event_ts": now.isoformat(),
        "action": action,
        "operator": operator,
        "reason": reason,
        "source": source,
        "payload_json": (
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                       default=str)
            if payload else None
        ),
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO manual_halt_event (
            event_ts, action, operator, reason, source, payload_json,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["action"], row["operator"], row["reason"],
            row["source"], row["payload_json"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


def current_pause_state(conn: sqlite3.Connection) -> str:
    """Return the most recent action state: 'paused' if last action was
    pause without subsequent resume/flatten; 'flattened' if the last
    action is flatten; else 'normal'."""
    cur = conn.execute(
        "SELECT action FROM manual_halt_event ORDER BY ledger_seq DESC LIMIT 1"
    )
    r = cur.fetchone()
    if not r:
        return "normal"
    last = r[0]
    if last == "pause":
        return "paused"
    if last == "flatten":
        return "flattened"
    return "normal"


__all__ = ["ACTIONS", "SOURCES", "write_event", "current_pause_state"]
