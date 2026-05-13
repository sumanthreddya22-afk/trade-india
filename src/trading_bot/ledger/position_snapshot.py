"""Hash-chained append for ``position_snapshot``.

Plan v4 §5: snapshot every 5 min during session and at session close.
Phase 1 supplies the writer; Phase 5 wires the kernel daemon's scheduler.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Iterable, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_snapshot(
    conn: sqlite3.Connection,
    *,
    source: str,                                  # "bot" | "broker"
    symbol: str,
    asset_class: str,
    qty: float,
    classification: str,                          # "bot|external|manual|unknown"
    avg_cost: Optional[float] = None,
    market_price: Optional[float] = None,
    market_value: Optional[float] = None,
    strategy_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one snapshot row. Returns ``ledger_seq``.

    The caller passes the snapshot timestamp; multiple symbols snapshotted
    at the same wall-clock pass the same ``now`` so they all carry the
    identical ``snapshot_ts`` (which makes "all positions as of T" queries
    cheap).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "position_snapshot")
    row = {
        "snapshot_ts": now.isoformat(),
        "source": source,
        "symbol": symbol,
        "asset_class": asset_class,
        "qty": float(qty),
        "avg_cost": None if avg_cost is None else float(avg_cost),
        "market_price": None if market_price is None else float(market_price),
        "market_value": None if market_value is None else float(market_value),
        "strategy_id": strategy_id,
        "classification": classification,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO position_snapshot (
            snapshot_ts, source, symbol, asset_class, qty, avg_cost,
            market_price, market_value, strategy_id, classification,
            prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["snapshot_ts"], row["source"], row["symbol"], row["asset_class"],
            row["qty"], row["avg_cost"], row["market_price"], row["market_value"],
            row["strategy_id"], row["classification"], prev, this_hash,
        ),
    )
    return cur.lastrowid


def write_snapshot_batch(
    conn: sqlite3.Connection,
    positions: Iterable[dict],
    *,
    source: str,
    now: Optional[dt.datetime] = None,
) -> list[int]:
    """Convenience: append a batch of positions with one wall-clock ts."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[int] = []
    for p in positions:
        seq = write_snapshot(
            conn,
            source=source,
            symbol=p["symbol"],
            asset_class=p.get("asset_class", ""),
            qty=p["qty"],
            classification=p.get("classification", "unknown"),
            avg_cost=p.get("avg_cost"),
            market_price=p.get("market_price"),
            market_value=p.get("market_value"),
            strategy_id=p.get("strategy_id"),
            now=now,
        )
        out.append(seq)
    return out


__all__ = ["write_snapshot", "write_snapshot_batch"]
