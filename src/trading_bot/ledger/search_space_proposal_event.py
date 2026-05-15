"""Hash-chained append for ``search_space_proposal_event`` (v4 Phase C).

Monthly Claude memo proposing additions to ``research/search_space_v1.json``.
This is *proposal only* — humans must sign a new versioned file to apply.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    review_month_iso: str,
    persona_id: str,
    persona_hash: str,
    current_hash: str,
    proposed_additions: Mapping,
    memo_markdown: str,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "search_space_proposal_event")
    row = {
        "event_ts": now.isoformat(),
        "review_month_iso": review_month_iso,
        "persona_id": persona_id,
        "persona_hash": persona_hash,
        "current_hash": current_hash,
        "proposed_additions": json.dumps(
            dict(proposed_additions), sort_keys=True, separators=(",", ":"),
            default=str,
        ),
        "memo_markdown": memo_markdown,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO search_space_proposal_event (
            event_ts, review_month_iso, persona_id, persona_hash,
            current_hash, proposed_additions, memo_markdown,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["review_month_iso"], row["persona_id"],
            row["persona_hash"], row["current_hash"],
            row["proposed_additions"], row["memo_markdown"],
            prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


__all__ = ["write_event"]
