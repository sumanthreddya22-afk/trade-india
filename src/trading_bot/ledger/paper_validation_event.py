"""Hash-chained append for ``paper_validation_event`` (v4 Phase C).

Records the outcome of a mutation candidate's paper-submit validation
test: how many decisions were taken, how many intents made it past the
risk precheck, how many filled, and the average slippage. ``passed=1``
means the candidate may auto-promote to ``tiny_paper`` (skipping shadow)
under the paper fast-track lock.
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
    candidate_id: str,
    strategy_family: str,
    candidate_params: Mapping,
    num_decisions: int,
    submitted_intents: int,
    risk_rejected: int,
    filled_intents: int,
    avg_slippage_bps: float,
    passed: bool,
    reason: str,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "paper_validation_event")
    row = {
        "event_ts": now.isoformat(),
        "candidate_id": candidate_id,
        "strategy_family": strategy_family,
        "candidate_params": json.dumps(
            dict(candidate_params), sort_keys=True, separators=(",", ":"),
            default=str,
        ),
        "num_decisions": int(num_decisions),
        "submitted_intents": int(submitted_intents),
        "risk_rejected": int(risk_rejected),
        "filled_intents": int(filled_intents),
        "avg_slippage_bps": float(avg_slippage_bps),
        "passed": 1 if passed else 0,
        "reason": reason,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO paper_validation_event (
            event_ts, candidate_id, strategy_family, candidate_params,
            num_decisions, submitted_intents, risk_rejected, filled_intents,
            avg_slippage_bps, passed, reason, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["candidate_id"], row["strategy_family"],
            row["candidate_params"], row["num_decisions"],
            row["submitted_intents"], row["risk_rejected"],
            row["filled_intents"], row["avg_slippage_bps"], row["passed"],
            row["reason"], prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


__all__ = ["write_event"]
