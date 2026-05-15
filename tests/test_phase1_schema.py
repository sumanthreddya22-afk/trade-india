"""Phase 1 — DDL applies cleanly and every table is present."""
from __future__ import annotations

import sqlite3

from trading_bot.ledger import HASH_CHAINED_TABLES, SCHEMA_VERSION, read_schema_version


EXPECTED_TABLES = {
    "order_master", "order_state_event", "fill_event",
    "position_snapshot", "strategy_decision", "reconciliation_proof",
    "schema_meta",
}

EXPECTED_VIEW = "order_current"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def _view_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='view'")
    return {r[0] for r in cur.fetchall()}


def _trigger_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    return {r[0] for r in cur.fetchall()}


def test_all_tables_present(ledger_conn) -> None:
    tables = _table_names(ledger_conn)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


def test_order_current_view_present(ledger_conn) -> None:
    views = _view_names(ledger_conn)
    assert EXPECTED_VIEW in views


def test_immutability_triggers_present(ledger_conn) -> None:
    triggers = _trigger_names(ledger_conn)
    expected = set()
    for t in (
        "order_master", "order_state_event", "fill_event",
        "position_snapshot", "strategy_decision", "reconciliation_proof",
    ):
        expected.add(f"no_update_{t}")
        expected.add(f"no_delete_{t}")
    missing = expected - triggers
    assert not missing, f"missing triggers: {missing}"


def test_schema_version_recorded(ledger_conn) -> None:
    assert read_schema_version(ledger_conn) == SCHEMA_VERSION


def test_hash_chained_tables_constant_matches() -> None:
    assert set(HASH_CHAINED_TABLES) == {
        "order_state_event", "fill_event", "position_snapshot",
        "strategy_decision", "reconciliation_proof", "feature_snapshot",
    }


def test_create_ledger_is_idempotent(ledger_conn) -> None:
    # Running again must not raise (IF NOT EXISTS everywhere).
    from trading_bot.ledger import create_ledger
    create_ledger(ledger_conn)
    create_ledger(ledger_conn)
