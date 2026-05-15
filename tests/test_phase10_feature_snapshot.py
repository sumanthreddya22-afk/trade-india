"""feature_snapshot append-only ledger table."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_bot.ledger import connect_writer, create_ledger
from trading_bot.ledger.feature_snapshot import insert_or_get, load


@pytest.fixture
def ledger(tmp_path: Path):
    p = tmp_path / "ledger.db"
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_insert_then_load_roundtrip(ledger) -> None:
    conn = connect_writer(ledger)
    try:
        sid = insert_or_get(
            conn,
            snapshot_id="univ:abc123",
            strategy_id="DUAL_MOMENTUM_v1",
            universe={"rule_name": "top_by_volume",
                      "symbols": ["SPY", "TLT"], "rule_hash": "deadbeef"},
            intel={"vix": {"value": 14.2, "source_ts": "2026-05-15"}},
        )
        conn.commit()
        loaded = load(conn, sid)
    finally:
        conn.close()
    assert loaded is not None
    assert loaded["snapshot_id"] == "univ:abc123"
    assert loaded["universe"]["symbols"] == ["SPY", "TLT"]
    assert loaded["intel"]["vix"]["value"] == 14.2


def test_insert_is_idempotent_on_snapshot_id(ledger) -> None:
    """A second insert with the same snapshot_id must not append a new
    row (and must not extend the hash chain)."""
    conn = connect_writer(ledger)
    try:
        insert_or_get(conn, snapshot_id="univ:idem",
                      strategy_id="DUAL_MOMENTUM_v1",
                      universe={"symbols": ["SPY"]}, intel={})
        conn.commit()
        insert_or_get(conn, snapshot_id="univ:idem",
                      strategy_id="DUAL_MOMENTUM_v1",
                      universe={"symbols": ["SPY"]}, intel={})
        conn.commit()
        cur = conn.execute("SELECT COUNT(*) FROM feature_snapshot")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_append_only_trigger_blocks_update(ledger) -> None:
    conn = connect_writer(ledger)
    try:
        insert_or_get(conn, snapshot_id="univ:locked",
                      strategy_id="DUAL_MOMENTUM_v1",
                      universe={"symbols": ["SPY"]}, intel={})
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE feature_snapshot SET intel_json='{}' "
                "WHERE snapshot_id=?", ("univ:locked",),
            )
    finally:
        conn.close()


def test_hash_chain_extends_for_distinct_snapshots(ledger) -> None:
    conn = connect_writer(ledger)
    try:
        insert_or_get(conn, snapshot_id="univ:a",
                      strategy_id="DUAL_MOMENTUM_v1",
                      universe={"symbols": ["SPY"]}, intel={})
        insert_or_get(conn, snapshot_id="univ:b",
                      strategy_id="DUAL_MOMENTUM_v1",
                      universe={"symbols": ["TLT"]}, intel={})
        conn.commit()
        cur = conn.execute(
            "SELECT snapshot_id, prev_hash, this_hash "
            "FROM feature_snapshot ORDER BY ledger_seq"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0][2] == rows[1][1]            # row1.this_hash == row2.prev_hash
    assert rows[0][2] != rows[1][2]            # distinct payloads → distinct hash
