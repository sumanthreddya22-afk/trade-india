"""Writer + idempotency helper for ``order_master``.

``order_master`` is immutable: inserted once at intent time and never
updated. The unique constraint on ``client_order_id`` plus the
BEFORE-UPDATE / BEFORE-DELETE triggers (see ``schema.py``) enforce it.

The idempotency contract (Plan v4 §5 box):

    client_order_id is YYYYMMDD_<strategy>_<symbol>_<seq> and is UNIQUE
    in order_master. The execution router refuses to re-submit a
    client_order_id that already exists with current-state in
    {submitted, acked, partially_filled, filled}.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal, Optional

# States the router treats as "live" — re-submitting an existing CID in
# any of these is forbidden. ``intent`` is also live: the order is being
# constructed but no broker side yet exists, so re-submitting would
# create a duplicate intent.
ACTIVE_STATES: frozenset[str] = frozenset({
    "intent", "submitted", "acked", "partially_filled", "filled",
})

TERMINAL_STATES: frozenset[str] = frozenset({
    "rejected", "cancelled", "expired",
})

IdempotencyT = Literal["absent", "active", "terminal"]


@dataclass(frozen=True)
class OrderIntent:
    """The canonical intent payload. ``intent_hash`` (sha256 of the
    canonical JSON of this payload, excluding the hash itself) lands in
    ``order_master.intent_hash``.
    """

    client_order_id: str
    strategy_id: str
    strategy_ver: int
    symbol: str
    asset_class: str
    side: str
    qty: float
    limit_price: Optional[float]
    tif: str
    origin: str

    def canonical(self) -> bytes:
        from trading_bot.ledger.canonical import canonical_json
        return canonical_json(self.__dict__)

    def intent_hash(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


def insert_order_master(
    conn: sqlite3.Connection,
    intent: OrderIntent,
    *,
    now: Optional[dt.datetime] = None,
    order_uid: Optional[str] = None,
) -> str:
    """Insert a row into ``order_master`` and return the generated
    ``order_uid``. Caller is responsible for the writer lock + IMMEDIATE
    transaction (see ``connection.acquire_writer_lock``).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    order_uid = order_uid or _generate_order_uid()
    conn.execute(
        """
        INSERT INTO order_master (
            order_uid, client_order_id, strategy_id, strategy_ver,
            symbol, asset_class, side, qty, limit_price, tif,
            intent_hash, origin, created_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            order_uid,
            intent.client_order_id,
            intent.strategy_id,
            intent.strategy_ver,
            intent.symbol,
            intent.asset_class,
            intent.side,
            float(intent.qty),
            None if intent.limit_price is None else float(intent.limit_price),
            intent.tif,
            intent.intent_hash(),
            intent.origin,
            now.isoformat(),
        ),
    )
    return order_uid


def lookup_by_client_order_id(
    conn: sqlite3.Connection, client_order_id: str
) -> Optional[dict]:
    """Return the ``order_master`` row matching ``client_order_id`` or None."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM order_master WHERE client_order_id = ?",
        (client_order_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def current_state(
    conn: sqlite3.Connection, order_uid: str
) -> Optional[str]:
    """Return the most-recent ``to_state`` for ``order_uid`` or None."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT to_state
        FROM order_state_event
        WHERE order_uid = ?
        ORDER BY ledger_seq DESC
        LIMIT 1
        """,
        (order_uid,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def check_idempotent(
    conn: sqlite3.Connection, client_order_id: str
) -> tuple[IdempotencyT, Optional[str]]:
    """Idempotency lookup. Returns:

      - ``("absent", None)``     — never seen; caller may submit.
      - ``("active", order_uid)``— exists and live; caller MUST refuse.
      - ``("terminal", order_uid)`` — exists but terminal; caller may
                                       submit a new CID (not this one).
    """
    row = lookup_by_client_order_id(conn, client_order_id)
    if row is None:
        return ("absent", None)
    state = current_state(conn, row["order_uid"])
    if state is None:
        # Master row exists with no state event yet — treat as intent.
        return ("active", row["order_uid"])
    if state in ACTIVE_STATES:
        return ("active", row["order_uid"])
    if state in TERMINAL_STATES:
        return ("terminal", row["order_uid"])
    # Unrecognised state — fail closed.
    return ("active", row["order_uid"])


# ---------------------------------------------------------------------------
# UUIDv7 — time-ordered 128-bit identifier (RFC 9562 §5.7).
#
# Python's stdlib uuid module does not yet ship UUIDv7. We implement it
# inline so order_uids sort by creation time, which makes ledger scans
# vastly cheaper for "give me all orders for 2026-05-13".
# ---------------------------------------------------------------------------

def _generate_order_uid() -> str:
    """Return a UUIDv7 hex string, time-ordered."""
    ts_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    rand_a = int.from_bytes(uuid.uuid4().bytes[:2], "big") & 0x0FFF
    rand_b = int.from_bytes(uuid.uuid4().bytes[2:10], "big") & 0x3FFFFFFFFFFFFFFF
    v7_int = (
        (ts_ms & 0xFFFFFFFFFFFF) << 80
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return str(uuid.UUID(int=v7_int))


__all__ = [
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "IdempotencyT",
    "OrderIntent",
    "check_idempotent",
    "current_state",
    "insert_order_master",
    "lookup_by_client_order_id",
]
