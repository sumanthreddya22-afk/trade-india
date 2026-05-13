"""Phase 1 — fill_event writer + dedup via broker_fill_id."""
from __future__ import annotations

import sqlite3

import pytest

from trading_bot.ledger import (
    OrderIntent, append_fill_event, append_state_event,
    insert_order_master, verify_chain,
)


def _seeded_order(conn) -> str:
    intent = OrderIntent(
        client_order_id="20260513_T_SPY_1",
        strategy_id="T", strategy_ver=1, symbol="SPY", asset_class="equity",
        side="buy", qty=10, limit_price=400.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    append_state_event(conn, order_uid=uid, to_state="intent")
    append_state_event(conn, order_uid=uid, to_state="submitted")
    append_state_event(conn, order_uid=uid, to_state="acked",
                       broker_order_id="brk-1")
    return uid


def test_append_one_fill(ledger_conn) -> None:
    uid = _seeded_order(ledger_conn)
    seq = append_fill_event(
        ledger_conn,
        order_uid=uid, broker_fill_id="fill-1",
        symbol="SPY", qty=10, price=400.05,
        fees_broker=0.0, fees_sec=0.0028, fees_finra_taf=0.001,
        is_partial=False, liquidity_flag="T",
    )
    assert seq == 1
    assert verify_chain(ledger_conn, "fill_event") == 1


def test_dedup_via_broker_fill_id(ledger_conn) -> None:
    uid = _seeded_order(ledger_conn)
    append_fill_event(ledger_conn, order_uid=uid, broker_fill_id="x",
                      symbol="SPY", qty=10, price=400.0)
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE"):
        append_fill_event(ledger_conn, order_uid=uid, broker_fill_id="x",
                          symbol="SPY", qty=10, price=400.0)


def test_multiple_partials_then_full(ledger_conn) -> None:
    uid = _seeded_order(ledger_conn)
    append_fill_event(ledger_conn, order_uid=uid, broker_fill_id="p1",
                      symbol="SPY", qty=3, price=400.0, is_partial=True)
    append_fill_event(ledger_conn, order_uid=uid, broker_fill_id="p2",
                      symbol="SPY", qty=3, price=400.05, is_partial=True)
    append_fill_event(ledger_conn, order_uid=uid, broker_fill_id="full",
                      symbol="SPY", qty=4, price=400.10, is_partial=False)
    assert verify_chain(ledger_conn, "fill_event") == 3
