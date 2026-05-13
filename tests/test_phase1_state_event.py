"""Phase 1 — order_state_event legal transitions + hash chaining."""
from __future__ import annotations

import pytest

from trading_bot.ledger import (
    IllegalTransition, OrderIntent,
    append_state_event, insert_order_master, verify_chain,
)


def _intent(cid: str = "20260513_TEST_SPY_1") -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        strategy_id="TEST", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )


def test_first_transition_must_be_intent(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    with pytest.raises(IllegalTransition):
        append_state_event(ledger_conn, order_uid=uid, to_state="filled")


def test_legal_happy_path(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    for st in ("intent", "submitted", "acked", "filled"):
        append_state_event(ledger_conn, order_uid=uid, to_state=st)
    assert verify_chain(ledger_conn, "order_state_event") == 4


def test_legal_acked_to_partial_to_filled(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    for st in ("intent", "submitted", "acked", "partially_filled",
               "partially_filled", "filled"):
        append_state_event(ledger_conn, order_uid=uid, to_state=st)
    assert verify_chain(ledger_conn, "order_state_event") == 6


def test_illegal_submitted_to_filled_blocked(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    append_state_event(ledger_conn, order_uid=uid, to_state="intent")
    append_state_event(ledger_conn, order_uid=uid, to_state="submitted")
    with pytest.raises(IllegalTransition):
        append_state_event(ledger_conn, order_uid=uid, to_state="filled")


def test_unknown_state_blocked(ledger_conn) -> None:
    uid = insert_order_master(ledger_conn, _intent())
    with pytest.raises(IllegalTransition):
        append_state_event(ledger_conn, order_uid=uid, to_state="ghost")


def test_two_orders_share_chain(ledger_conn) -> None:
    """The hash chain is per-table, not per-order. Verify chain still
    passes when events from two distinct orders interleave."""
    uid1 = insert_order_master(ledger_conn, _intent("20260513_T_SPY_1"))
    uid2 = insert_order_master(ledger_conn, _intent("20260513_T_QQQ_1"))
    append_state_event(ledger_conn, order_uid=uid1, to_state="intent")
    append_state_event(ledger_conn, order_uid=uid2, to_state="intent")
    append_state_event(ledger_conn, order_uid=uid1, to_state="submitted")
    append_state_event(ledger_conn, order_uid=uid2, to_state="cancelled")
    assert verify_chain(ledger_conn, "order_state_event") == 4
