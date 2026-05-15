"""Hash-chained append for ``llm_call_event`` (v4 Phase 12).

Every Claude CLI invocation through ``shared.llm_transport`` records one
row here for cost + cache + throttle audit. Append-only; queries roll up
into daily LLM budget reports.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    persona_id: str,
    model: str,
    priority: str,
    input_hash: str,
    cache_hit: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    deferred: bool = False,
    dropped: bool = False,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "llm_call_event")
    row = {
        "event_ts": now.isoformat(),
        "persona_id": persona_id,
        "model": model,
        "priority": priority,
        "input_hash": input_hash,
        "cache_hit": 1 if cache_hit else 0,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "latency_ms": int(latency_ms),
        "deferred": 1 if deferred else 0,
        "dropped": 1 if dropped else 0,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO llm_call_event (
            event_ts, persona_id, model, priority, input_hash, cache_hit,
            input_tokens, output_tokens, latency_ms, deferred, dropped,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["persona_id"], row["model"], row["priority"],
            row["input_hash"], row["cache_hit"], row["input_tokens"],
            row["output_tokens"], row["latency_ms"], row["deferred"],
            row["dropped"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


def calls_today(
    conn: sqlite3.Connection,
    *,
    now: Optional[dt.datetime] = None,
    exclude_cache_hits: bool = True,
) -> int:
    """Count non-cache-hit LLM calls since UTC midnight today."""
    now = now or dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    sql = "SELECT COUNT(*) FROM llm_call_event WHERE event_ts >= ?"
    params: tuple = (midnight,)
    if exclude_cache_hits:
        sql += " AND cache_hit = 0 AND dropped = 0"
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


__all__ = ["calls_today", "write_event"]
