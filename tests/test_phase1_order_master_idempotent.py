"""Phase 1 — order_master writer + idempotent client_order_id check.

P0 acceptance: "Submitting the same client_order_id twice produces one
acked order in the ledger." Phase 1 supplies the check; Phase 3 wires
the router to honour it.
"""
from __future__ import annotations

import sqlite3

import pytest

from trading_bot.ledger import (
    ACTIVE_STATES, TERMINAL_STATES, OrderIntent,
    append_state_event, check_idempotent, current_state,
    insert_order_master, lookup_by_client_order_id,
)


def _intent(cid: str = "20260513_TEST_SPY_1") -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        strategy_id="TEST", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )


def test_intent_hash_is_deterministic() -> None:
    h1 = _intent().intent_hash()
    h2 = _intent().intent_hash()
    assert h1 == h2
    assert len(h1) == 64


def test_insert_returns_uuidv7_order_uid(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    # UUIDv7 surface check: 8-4-4-4-12 hex with version=7 in the 13th hex char.
    parts = uid.split("-")
    assert len(parts) == 5
    assert parts[2][0] == "7", f"expected UUIDv7, got {uid}"


def test_unique_client_order_id_enforced(ledger_conn) -> None:
    insert_order_master(ledger_conn, _intent())
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE"):
        insert_order_master(ledger_conn, _intent())


def test_check_idempotent_absent(ledger_conn) -> None:
    status, uid = check_idempotent(ledger_conn, "nonexistent")
    assert status == "absent"
    assert uid is None


def test_check_idempotent_active_after_submit(ledger_conn) -> None:
    intent = _intent()
    uid = insert_order_master(ledger_conn, intent)
    append_state_event(ledger_conn, order_uid=uid, to_state="intent")
    append_state_event(ledger_conn, order_uid=uid, to_state="submitted")

    status, found_uid = check_idempotent(ledger_conn, intent.client_order_id)
    assert status == "active"
    assert found_uid == uid


def test_check_idempotent_terminal_after_cancel(ledger_conn) -> None:
    intent = _intent()
    uid = insert_order_master(ledger_conn, intent)
    append_state_event(ledger_conn, order_uid=uid, to_state="intent")
    append_state_event(ledger_conn, order_uid=uid, to_state="cancelled")

    status, found_uid = check_idempotent(ledger_conn, intent.client_order_id)
    assert status == "terminal"
    assert found_uid == uid


def test_lookup_by_client_order_id_returns_row(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    row = lookup_by_client_order_id(ledger_conn, "20260513_TEST_SPY_1")
    assert row is not None
    assert row["order_uid"] == uid
    assert row["symbol"] == "SPY"


def test_current_state_walks_state_events(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    assert current_state(ledger_conn, uid) is None
    append_state_event(ledger_conn, order_uid=uid, to_state="intent")
    assert current_state(ledger_conn, uid) == "intent"
    append_state_event(ledger_conn, order_uid=uid, to_state="submitted")
    assert current_state(ledger_conn, uid) == "submitted"
    append_state_event(ledger_conn, order_uid=uid, to_state="acked",
                       broker_order_id="brk-1")
    assert current_state(ledger_conn, uid) == "acked"


def test_active_and_terminal_state_sets_are_disjoint() -> None:
    assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)
