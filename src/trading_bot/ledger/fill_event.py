"""Hash-chained append for ``fill_event``.

Joins to ``order_master`` by ``order_uid`` (NOT by ``broker_order_id`` —
brokers occasionally re-use those across cancel/replace flows).
``broker_fill_id`` is the de-dup key: UNIQUE in the table, so a re-played
fill webhook cannot double-count.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def append_fill_event(
    conn: sqlite3.Connection,
    *,
    order_uid: str,
    broker_fill_id: str,
    symbol: str,
    qty: float,
    price: float,
    fees_broker: float = 0.0,
    fees_sec: float = 0.0,
    fees_finra_taf: float = 0.0,
    is_partial: bool = False,
    liquidity_flag: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one fill event. Returns the new ``ledger_seq``.

    The UNIQUE constraint on ``broker_fill_id`` raises ``IntegrityError``
    if the same fill is replayed — caller can catch this as the
    idempotent-dedup signal.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "fill_event")
    row = {
        "event_ts": now.isoformat(),
        "order_uid": order_uid,
        "broker_fill_id": broker_fill_id,
        "symbol": symbol,
        "qty": float(qty),
        "price": float(price),
        "fees_broker": float(fees_broker),
        "fees_sec": float(fees_sec),
        "fees_finra_taf": float(fees_finra_taf),
        "is_partial": int(bool(is_partial)),
        "liquidity_flag": liquidity_flag,
    }
    this_hash = compute_this_hash(prev, row)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO fill_event (
            event_ts, order_uid, broker_fill_id, symbol, qty, price,
            fees_broker, fees_sec, fees_finra_taf,
            is_partial, liquidity_flag, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["event_ts"], row["order_uid"], row["broker_fill_id"],
            row["symbol"], row["qty"], row["price"],
            row["fees_broker"], row["fees_sec"], row["fees_finra_taf"],
            row["is_partial"], row["liquidity_flag"],
            prev, this_hash,
        ),
    )
    return cur.lastrowid


__all__ = ["append_fill_event"]
