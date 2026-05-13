"""Phase 1 — UPDATE / DELETE on any ledger table raises a schema-level error.

This is Plan §14 P0: "Append-only ledger — Attempting UPDATE/DELETE on
any ledger table raises a schema-level error."
"""
from __future__ import annotations

import sqlite3

import pytest

from trading_bot.ledger import (
    OrderIntent, append_fill_event, append_state_event,
    insert_order_master, write_decision, write_recon_proof,
    write_snapshot,
)


def _seed_order(conn: sqlite3.Connection) -> str:
    """Insert one order_master row and its first state event for tests
    that need a target row to attempt mutating."""
    intent = OrderIntent(
        client_order_id="20260513_TEST_SPY_1",
        strategy_id="TEST", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    append_state_event(conn, order_uid=uid, to_state="intent")
    return uid


def _seed_all_tables(conn: sqlite3.Connection) -> None:
    """Put exactly one row in every append-only table so DELETE has a
    target row that will fire the per-row BEFORE-DELETE trigger."""
    uid = _seed_order(conn)
    append_state_event(conn, order_uid=uid, to_state="submitted")
    append_state_event(conn, order_uid=uid, to_state="acked",
                       broker_order_id="brk-1")
    append_fill_event(conn, order_uid=uid, broker_fill_id="fill-1",
                      symbol="SPY", qty=1, price=400.0)
    write_snapshot(conn, source="bot", symbol="SPY", asset_class="equity",
                   qty=1.0, classification="bot")
    write_decision(conn, strategy_id="T", strategy_ver=1,
                   code_hash="a" * 64, config_hash="b" * 64,
                   policy_hash="c" * 64,
                   feature_snapshot_id="feat-x",
                   intent={"symbol": "SPY"}, risk_decision="accept")
    write_recon_proof(conn, recon_window="eod",
                      bot_hash="d" * 64, broker_hash="d" * 64,
                      match=True, diff_json=None, action_taken="none")


@pytest.mark.parametrize("table", [
    "order_master", "order_state_event", "fill_event",
    "position_snapshot", "strategy_decision", "reconciliation_proof",
])
def test_delete_forbidden(ledger_conn, table) -> None:
    _seed_all_tables(ledger_conn)
    with pytest.raises(sqlite3.IntegrityError, match=r"append-only"):
        ledger_conn.execute(f"DELETE FROM {table} WHERE 1=1")


def test_update_order_master_forbidden(ledger_conn) -> None:
    _seed_order(ledger_conn)
    with pytest.raises(sqlite3.IntegrityError, match=r"append-only"):
        ledger_conn.execute(
            "UPDATE order_master SET qty = 99 WHERE order_uid IS NOT NULL"
        )


def test_update_state_event_forbidden(ledger_conn) -> None:
    _seed_order(ledger_conn)
    with pytest.raises(sqlite3.IntegrityError, match=r"append-only"):
        ledger_conn.execute(
            "UPDATE order_state_event SET to_state = 'rejected' WHERE 1=1"
        )


def test_insert_still_works(ledger_conn) -> None:
    uid = _seed_order(ledger_conn)
    # adding a legal transition must not raise
    append_state_event(ledger_conn, order_uid=uid, to_state="submitted")
