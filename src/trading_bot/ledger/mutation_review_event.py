"""Hash-chained append for ``mutation_review_event`` (v4 Phase C).

Weekly Claude memo summarising last week's mutation outcomes. The memo
is informational — does not auto-act — but is anchored to a persona hash
so the rationale chain is auditable.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    review_window_iso: str,
    persona_id: str,
    persona_hash: str,
    n_candidates: int,
    n_passed: int,
    memo_markdown: str,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "mutation_review_event")
    row = {
        "event_ts": now.isoformat(),
        "review_window_iso": review_window_iso,
        "persona_id": persona_id,
        "persona_hash": persona_hash,
        "n_candidates": int(n_candidates),
        "n_passed": int(n_passed),
        "memo_markdown": memo_markdown,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO mutation_review_event (
            event_ts, review_window_iso, persona_id, persona_hash,
            n_candidates, n_passed, memo_markdown, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["review_window_iso"], row["persona_id"],
            row["persona_hash"], row["n_candidates"], row["n_passed"],
            row["memo_markdown"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


__all__ = ["write_event"]
