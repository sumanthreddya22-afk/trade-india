"""Hash-chained append for ``order_state_event``.

Plan v4 §5: every state transition is one row. ``prev_hash`` is the
previous row's ``this_hash`` (or genesis ``"0"*64`` for the first row in
the table). ``this_hash = sha256(prev_hash || canonical(row))``.

Caller is responsible for holding the writer lock and an IMMEDIATE
transaction. The single-writer guard plus the IMMEDIATE transaction
prevent two writers from racing the chain.

Permitted transitions (enforced here):

    None         -> intent
    intent       -> submitted | cancelled
    submitted    -> acked | rejected | cancelled
    acked        -> partially_filled | filled | cancelled | expired
    partially_filled -> partially_filled | filled | cancelled | expired

Anything else raises ``IllegalTransition`` — a structural bug, not a
business case.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash

STATES = (
    "intent", "submitted", "acked", "rejected",
    "cancelled", "partially_filled", "filled", "expired",
)

_LEGAL_TRANSITIONS: dict[Optional[str], frozenset[str]] = {
    None: frozenset({"intent"}),
    "intent": frozenset({"submitted", "cancelled"}),
    "submitted": frozenset({"acked", "rejected", "cancelled"}),
    "acked": frozenset({"partially_filled", "filled", "cancelled", "expired"}),
    "partially_filled": frozenset({
        "partially_filled", "filled", "cancelled", "expired",
    }),
}


class IllegalTransition(Exception):
    """Raised when ``append_state_event`` is asked to move an order from
    a state to one not in the legal transition table."""


def append_state_event(
    conn: sqlite3.Connection,
    *,
    order_uid: str,
    to_state: str,
    from_state: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one state transition row. Returns the new ``ledger_seq``.

    If ``from_state`` is omitted, it is read from the most recent event
    for this order_uid (or None for a brand-new order).
    """
    if to_state not in STATES:
        raise IllegalTransition(f"unknown to_state={to_state!r}")
    if from_state is None:
        from_state = _read_current_state(conn, order_uid)
    legal = _LEGAL_TRANSITIONS.get(from_state, frozenset())
    if to_state not in legal:
        raise IllegalTransition(
            f"illegal transition for order_uid={order_uid}: "
            f"{from_state} -> {to_state}"
        )

    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "order_state_event")
    row_content = {
        "event_ts": now.isoformat(),
        "order_uid": order_uid,
        "from_state": from_state,
        "to_state": to_state,
        "broker_order_id": broker_order_id,
        "reason": reason,
    }
    this_hash = compute_this_hash(prev, row_content)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO order_state_event (
            event_ts, order_uid, from_state, to_state,
            broker_order_id, reason, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            row_content["event_ts"],
            row_content["order_uid"],
            row_content["from_state"],
            row_content["to_state"],
            row_content["broker_order_id"],
            row_content["reason"],
            prev,
            this_hash,
        ),
    )
    return cur.lastrowid


def _read_current_state(
    conn: sqlite3.Connection, order_uid: str
) -> Optional[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT to_state FROM order_state_event "
        "WHERE order_uid = ? ORDER BY ledger_seq DESC LIMIT 1",
        (order_uid,),
    )
    row = cur.fetchone()
    return row[0] if row else None


__all__ = [
    "IllegalTransition",
    "STATES",
    "append_state_event",
]
