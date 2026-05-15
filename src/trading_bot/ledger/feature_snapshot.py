"""Hash-chained append for ``feature_snapshot``.

Every kernel decision references a snapshot id; the snapshot row
captures the universe (from ``ingest.universe.resolve_universe``) and
the intel records read at decision time. A backtest replays the
snapshot to reproduce the same decision deterministically.

Snapshots are content-addressed: the same ``snapshot_id`` (sha256 of
the canonical payload) is reused across decisions when the inputs
are identical. ``insert_or_get`` is idempotent on ``snapshot_id``.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                      default=str)


def insert_or_get(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    strategy_id: str,
    universe: Mapping[str, Any],
    intel: Mapping[str, Any],
    now: Optional[dt.datetime] = None,
) -> str:
    """Append a snapshot row (if not already present) and return the
    snapshot id. Idempotent on ``snapshot_id``: a second call with the
    same id returns the existing row without writing — the hash chain
    is not extended.
    """
    cur = conn.execute(
        "SELECT snapshot_id FROM feature_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    )
    if cur.fetchone() is not None:
        return snapshot_id
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "feature_snapshot")
    universe_json = _canonical(universe)
    intel_json = _canonical(intel)
    row = {
        "snapshot_id": snapshot_id,
        "captured_ts": now.isoformat(),
        "strategy_id": strategy_id,
        "universe_json": universe_json,
        "intel_json": intel_json,
    }
    this_hash = compute_this_hash(prev, row)
    conn.execute(
        """
        INSERT INTO feature_snapshot (
            snapshot_id, captured_ts, strategy_id,
            universe_json, intel_json, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (snapshot_id, row["captured_ts"], strategy_id,
         universe_json, intel_json, prev, this_hash),
    )
    return snapshot_id


def load(
    conn: sqlite3.Connection, snapshot_id: str,
) -> Optional[dict[str, Any]]:
    cur = conn.execute(
        "SELECT snapshot_id, captured_ts, strategy_id, "
        "universe_json, intel_json, prev_hash, this_hash "
        "FROM feature_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    )
    r = cur.fetchone()
    if r is None:
        return None
    return {
        "snapshot_id": r[0],
        "captured_ts": r[1],
        "strategy_id": r[2],
        "universe": json.loads(r[3]),
        "intel": json.loads(r[4]),
        "prev_hash": r[5],
        "this_hash": r[6],
    }


__all__ = ["insert_or_get", "load"]
