"""Hash-chained append for ``strategy_decision``.

Plan v4 §5: every kernel run logs the inputs and outputs of one decision.
The row references hashes (code, config, policy, feature snapshot) so the
*entire decision* can be reproduced from on-disk artifacts.

Phase 1 supplies the writer; Phase 5 wires the kernel runner.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_decision(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    code_hash: str,
    config_hash: str,
    policy_hash: str,
    feature_snapshot_id: str,
    intent: Mapping[str, Any],
    risk_decision: str,                       # accept | reduce | halt | skip
    risk_reason: Optional[str] = None,
    emitted_client_order_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one decision row. Returns ``ledger_seq``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "strategy_decision")
    intent_canonical = json.dumps(
        dict(intent), sort_keys=True, separators=(",", ":"), default=str,
    )
    row = {
        "decision_ts": now.isoformat(),
        "strategy_id": strategy_id,
        "strategy_ver": strategy_ver,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "policy_hash": policy_hash,
        "feature_snapshot_id": feature_snapshot_id,
        "intent_json": intent_canonical,
        "risk_decision": risk_decision,
        "risk_reason": risk_reason,
        "emitted_client_order_id": emitted_client_order_id,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO strategy_decision (
            decision_ts, strategy_id, strategy_ver,
            code_hash, config_hash, policy_hash, feature_snapshot_id,
            intent_json, risk_decision, risk_reason,
            emitted_client_order_id, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["decision_ts"], row["strategy_id"], row["strategy_ver"],
            row["code_hash"], row["config_hash"], row["policy_hash"],
            row["feature_snapshot_id"], row["intent_json"],
            row["risk_decision"], row["risk_reason"],
            row["emitted_client_order_id"], prev, this_hash,
        ),
    )
    return cur.lastrowid


__all__ = ["write_decision"]
