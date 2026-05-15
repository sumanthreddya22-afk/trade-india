"""Hash-chained append for ``intel_feature_snapshot`` (v4 Phase A).

Per (decision_id, strategy, symbol, feature) row showing which intel
feature value the bot used in a decision. Lets postmortems answer
"was insider_cluster_buying a factor in this trade?"
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    strategy_id: str,
    symbol: str,
    feature_id: str,
    feature_value: Any,
    feed_id: str,
    asof: dt.datetime,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "intel_feature_snapshot")
    canonical_value = json.dumps(
        feature_value, sort_keys=True, separators=(",", ":"), default=str,
    )
    row = {
        "event_ts": now.isoformat(),
        "decision_id": decision_id,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "feature_id": feature_id,
        "feature_value": canonical_value,
        "feed_id": feed_id,
        "asof": asof.isoformat(),
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO intel_feature_snapshot (
            event_ts, decision_id, strategy_id, symbol, feature_id,
            feature_value, feed_id, asof, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["decision_id"], row["strategy_id"],
            row["symbol"], row["feature_id"], row["feature_value"],
            row["feed_id"], row["asof"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


__all__ = ["write_event"]
