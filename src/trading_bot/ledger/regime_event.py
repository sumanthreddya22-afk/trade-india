"""Hash-chained append for ``regime_event`` (v4 Phase A).

Every regime transition (or classifier-confirmed re-eval) appends a row
here. The classifier, manual override, and Claude-supported recovery
flow all write through this function — the ``source`` column tells you
which.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Mapping, Optional, Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


REGIMES = ("normal", "caution", "stress", "crisis", "recovery")
ASSET_CLASSES = ("stocks", "crypto", "options")
SOURCES = ("classifier", "manual", "claude_recovery", "fast_trigger")


def write_event(
    conn: sqlite3.Connection,
    *,
    asset_class: str,
    prior_regime: str,
    new_regime: str,
    source: str,
    trigger_signals: Mapping[str, Any],
    mandated_actions: Sequence[Mapping[str, Any]],
    claude_memo_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class must be one of {ASSET_CLASSES}")
    if prior_regime not in REGIMES or new_regime not in REGIMES:
        raise ValueError(f"regime must be one of {REGIMES}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "regime_event")
    row = {
        "event_ts": now.isoformat(),
        "asset_class": asset_class,
        "prior_regime": prior_regime,
        "new_regime": new_regime,
        "source": source,
        "trigger_signals_json": json.dumps(
            dict(trigger_signals), sort_keys=True, separators=(",", ":"),
            default=str,
        ),
        "mandated_actions_json": json.dumps(
            list(mandated_actions), sort_keys=True, separators=(",", ":"),
            default=str,
        ),
        "claude_memo_id": claude_memo_id,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO regime_event (
            event_ts, asset_class, prior_regime, new_regime, source,
            trigger_signals_json, mandated_actions_json, claude_memo_id,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_ts"], row["asset_class"], row["prior_regime"],
            row["new_regime"], row["source"], row["trigger_signals_json"],
            row["mandated_actions_json"], row["claude_memo_id"],
            prev, this_hash,
        ),
    )
    return int(cur.lastrowid)


def current_regime(
    conn: sqlite3.Connection, asset_class: str,
) -> str:
    """Return the most recent ``new_regime`` for ``asset_class``, defaulting
    to ``"normal"`` if there's no history yet."""
    cur = conn.execute(
        "SELECT new_regime FROM regime_event WHERE asset_class=? "
        "ORDER BY ledger_seq DESC LIMIT 1",
        (asset_class,),
    )
    r = cur.fetchone()
    return r[0] if r else "normal"


__all__ = [
    "ASSET_CLASSES",
    "REGIMES",
    "SOURCES",
    "current_regime",
    "write_event",
]
