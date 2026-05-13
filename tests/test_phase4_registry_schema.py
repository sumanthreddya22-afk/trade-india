"""Phase 4 — registry DDL: 3 tables + triggers."""
from __future__ import annotations

import sqlite3

import pytest

from trading_bot.registry import (
    ensure_registry_tables, register_version,
)

EXPECTED_TABLES = {
    "strategy_version", "validation_artifact", "promotion_packet",
}


def _tables(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def _triggers(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    return {r[0] for r in cur.fetchall()}


def test_tables_created(ledger_conn) -> None:
    ensure_registry_tables(ledger_conn)
    assert EXPECTED_TABLES.issubset(_tables(ledger_conn))


def test_triggers_created(ledger_conn) -> None:
    ensure_registry_tables(ledger_conn)
    triggers = _triggers(ledger_conn)
    for t in ("strategy_version", "validation_artifact", "promotion_packet"):
        assert f"no_update_{t}" in triggers
        assert f"no_delete_{t}" in triggers


def test_update_strategy_version_forbidden(ledger_conn) -> None:
    register_version(
        ledger_conn, strategy_id="X", strategy_ver=1,
        code_hash="c", config_hash="cf", thesis_id="t", hypothesis_id="h",
        lane="benchmark", owner="op",
    )
    with pytest.raises(sqlite3.IntegrityError, match=r"append-only"):
        ledger_conn.execute(
            "UPDATE strategy_version SET status='live' WHERE strategy_id='X'"
        )
