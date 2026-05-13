"""Reconciliation: prove ``bot_hash == broker_hash`` for the current
position vector.

Plan v4 §5 + §6: nightly + at-close. ``match=0`` halts new entries
(Phase 2 wires the halt). Phase 1 supplies the math + the writer.

The position vector is canonicalised so that two vectors with the same
{symbol → qty} multiset hash to the same value, regardless of input
order or insertion order.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from typing import Iterable, Literal, Mapping, Optional, Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash

WindowT = Literal["intraday", "eod", "monthly"]


def _canonical_position_vector(positions: Iterable[Mapping]) -> str:
    """Stable canonical form of a position list.

    Each position is normalised to ``{symbol, qty, asset_class}`` with
    ``qty`` rounded to 8 decimal places to absorb Alpaca's reporting
    precision drift on crypto.
    """
    normalised = sorted(
        (
            {
                "symbol": p["symbol"],
                "qty": round(float(p["qty"]), 8),
                "asset_class": p.get("asset_class", ""),
            }
            for p in positions
        ),
        key=lambda d: (d["symbol"], d["asset_class"]),
    )
    return json.dumps(normalised, sort_keys=True, separators=(",", ":"))


def hash_position_vector(positions: Iterable[Mapping]) -> str:
    """sha256 hex of the canonical position vector."""
    canonical = _canonical_position_vector(positions)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_recon(
    *,
    bot_positions: Sequence[Mapping],
    broker_positions: Sequence[Mapping],
) -> tuple[str, str, bool, Optional[dict]]:
    """Compute bot_hash, broker_hash, match, and diff_json.

    ``diff_json`` is None when match. On mismatch it contains:
      {"only_in_bot": [...], "only_in_broker": [...], "qty_mismatches": [...]}
    """
    bot_hash = hash_position_vector(bot_positions)
    broker_hash = hash_position_vector(broker_positions)
    match = bot_hash == broker_hash
    if match:
        return bot_hash, broker_hash, True, None

    bot_map = {(p["symbol"], p.get("asset_class", "")): round(float(p["qty"]), 8)
               for p in bot_positions}
    bro_map = {(p["symbol"], p.get("asset_class", "")): round(float(p["qty"]), 8)
               for p in broker_positions}
    only_bot = sorted(k for k in bot_map.keys() - bro_map.keys())
    only_broker = sorted(k for k in bro_map.keys() - bot_map.keys())
    qty_mis = [
        {"symbol": k[0], "asset_class": k[1],
         "bot_qty": bot_map[k], "broker_qty": bro_map[k]}
        for k in (bot_map.keys() & bro_map.keys())
        if bot_map[k] != bro_map[k]
    ]
    diff = {
        "only_in_bot": [{"symbol": s, "asset_class": ac} for (s, ac) in only_bot],
        "only_in_broker": [{"symbol": s, "asset_class": ac} for (s, ac) in only_broker],
        "qty_mismatches": qty_mis,
    }
    return bot_hash, broker_hash, False, diff


def write_recon_proof(
    conn: sqlite3.Connection,
    *,
    recon_window: WindowT,
    bot_hash: str,
    broker_hash: str,
    match: bool,
    diff_json: Optional[dict] = None,
    action_taken: str = "none",       # none | halt_new | incident_opened
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one reconciliation_proof row. Returns ``ledger_seq``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "reconciliation_proof")
    diff_str = (
        json.dumps(diff_json, sort_keys=True, separators=(",", ":"))
        if diff_json is not None else None
    )
    row = {
        "recon_ts": now.isoformat(),
        "recon_window": recon_window,
        "bot_hash": bot_hash,
        "broker_hash": broker_hash,
        "match": 1 if match else 0,
        "diff_json": diff_str,
        "action_taken": action_taken,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reconciliation_proof (
            recon_ts, recon_window, bot_hash, broker_hash,
            match, diff_json, action_taken, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            row["recon_ts"], row["recon_window"], row["bot_hash"],
            row["broker_hash"], row["match"], row["diff_json"],
            row["action_taken"], prev, this_hash,
        ),
    )
    return cur.lastrowid


__all__ = [
    "compute_recon",
    "hash_position_vector",
    "write_recon_proof",
]
