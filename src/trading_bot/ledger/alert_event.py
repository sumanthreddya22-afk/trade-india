"""Hash-chained append for ``alert_event`` (WS5d P&L tripwires).

The four tripwires (realized_loss / drift / exec_quality / behavioural)
each write through this. Severity ``halt`` indicates the caller also
fired a kill_switch_event; ``alert`` is observable in the cockpit but
doesn't gate trading.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash

TRIPWIRES = ("realized_loss", "drift", "exec_quality", "behavioural")
SEVERITIES = ("alert", "halt")


def write_event(
    conn: sqlite3.Connection,
    *,
    tripwire: str,
    severity: str,
    observed: float,
    threshold: float,
    window: str,
    reason: str,
    payload: Optional[Mapping[str, Any]] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    if tripwire not in TRIPWIRES:
        raise ValueError(f"tripwire must be one of {TRIPWIRES}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "alert_event")
    row = {
        "event_ts": now.isoformat(),
        "tripwire": tripwire,
        "severity": severity,
        "observed": float(observed),
        "threshold": float(threshold),
        "window": window,
        "reason": reason,
        "payload_json": (
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                       default=str)
            if payload else None
        ),
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO alert_event (
            event_ts, tripwire, severity, observed, threshold, window,
            reason, payload_json, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["tripwire"], row["severity"],
            row["observed"], row["threshold"], row["window"],
            row["reason"], row["payload_json"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


__all__ = ["TRIPWIRES", "SEVERITIES", "write_event"]
