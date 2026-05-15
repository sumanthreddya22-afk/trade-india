"""Hash-chained append for ``drift_postmortem_event`` (v4 Phase A).

Stores Claude-authored memos triggered by drift / universe_audit / regime
events. The source event type + ledger_seq let the postmortem be linked
back to its triggering row.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


VALID_SOURCES = ("drift_event", "universe_audit_event", "regime_event")


def write_event(
    conn: sqlite3.Connection,
    *,
    source_event_type: str,
    source_ledger_seq: int,
    persona_id: str,
    persona_hash: str,
    memo_markdown: str,
    now: Optional[dt.datetime] = None,
) -> int:
    if source_event_type not in VALID_SOURCES:
        raise ValueError(
            f"source_event_type must be one of {VALID_SOURCES}, "
            f"got {source_event_type!r}"
        )
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "drift_postmortem_event")
    row = {
        "event_ts": now.isoformat(),
        "source_event_type": source_event_type,
        "source_ledger_seq": int(source_ledger_seq),
        "persona_id": persona_id,
        "persona_hash": persona_hash,
        "memo_markdown": memo_markdown,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO drift_postmortem_event (
            event_ts, source_event_type, source_ledger_seq,
            persona_id, persona_hash, memo_markdown,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["source_event_type"],
            row["source_ledger_seq"], row["persona_id"],
            row["persona_hash"], row["memo_markdown"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


__all__ = ["VALID_SOURCES", "write_event"]
