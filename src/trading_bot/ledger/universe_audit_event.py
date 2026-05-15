"""Hash-chained append for ``universe_audit_event`` (v4 Phase A).

The weekly universe audit job records one row per active strategy: the
top-N members at audit time, the diff vs the previous audit, the turnover
percentage, and a breach flag when turnover exceeds the policy threshold.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Optional, Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    members: Sequence[str],
    additions: Sequence[str],
    removals: Sequence[str],
    turnover_pct: float,
    breach: bool,
    claude_memo_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "universe_audit_event")
    row = {
        "event_ts": now.isoformat(),
        "strategy_id": strategy_id,
        "universe_size": len(members),
        "members_json": json.dumps(list(members), sort_keys=True,
                                   separators=(",", ":")),
        "additions_json": json.dumps(list(additions), sort_keys=True,
                                      separators=(",", ":")),
        "removals_json": json.dumps(list(removals), sort_keys=True,
                                     separators=(",", ":")),
        "turnover_pct": float(turnover_pct),
        "breach": 1 if breach else 0,
        "claude_memo_id": claude_memo_id,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO universe_audit_event (
            event_ts, strategy_id, universe_size, members_json,
            additions_json, removals_json, turnover_pct, breach,
            claude_memo_id, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["strategy_id"], row["universe_size"],
            row["members_json"], row["additions_json"], row["removals_json"],
            row["turnover_pct"], row["breach"], row["claude_memo_id"],
            prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


def latest_for_strategy(
    conn: sqlite3.Connection, strategy_id: str,
) -> Optional[dict]:
    cur = conn.execute(
        "SELECT event_ts, members_json FROM universe_audit_event "
        "WHERE strategy_id=? ORDER BY ledger_seq DESC LIMIT 1",
        (strategy_id,),
    )
    r = cur.fetchone()
    if r is None:
        return None
    return {
        "event_ts": r[0],
        "members": json.loads(r[1]),
    }


__all__ = ["latest_for_strategy", "write_event"]
