"""Phase 1 — hash chain compute + verify + tamper detection."""
from __future__ import annotations

import sqlite3

import pytest

from trading_bot.ledger import (
    HashChainBroken, OrderIntent, append_state_event,
    insert_order_master, verify_all_chained, verify_chain,
)
from trading_bot.ledger.canonical import canonical_json
from trading_bot.ledger.hash_chain import GENESIS_PREV_HASH, compute_this_hash


def _seed_n_events(conn: sqlite3.Connection, n: int) -> str:
    intent = OrderIntent(
        client_order_id="20260513_TEST_SPY_1",
        strategy_id="TEST", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    append_state_event(conn, order_uid=uid, to_state="intent")
    if n >= 2:
        append_state_event(conn, order_uid=uid, to_state="submitted")
    if n >= 3:
        append_state_event(conn, order_uid=uid, to_state="acked",
                           broker_order_id="brk-1")
    if n >= 4:
        append_state_event(conn, order_uid=uid, to_state="filled")
    return uid


def test_canonical_json_is_deterministic() -> None:
    row = {"b": 2, "a": 1, "ledger_seq": 99, "prev_hash": "deadbeef",
           "this_hash": "cafe", "c": [1, 2, 3]}
    out = canonical_json(row)
    # ledger_seq / prev_hash / this_hash excluded, others sorted.
    assert out == b'{"a":1,"b":2,"c":[1,2,3]}'


def test_compute_this_hash_is_stable() -> None:
    row = {"event_ts": "2026-05-13T12:00:00+00:00",
           "to_state": "intent", "order_uid": "abc"}
    h1 = compute_this_hash("0" * 64, row)
    h2 = compute_this_hash("0" * 64, row)
    assert h1 == h2
    assert len(h1) == 64


def test_verify_empty_table(ledger_conn) -> None:
    assert verify_chain(ledger_conn, "order_state_event") == 0


def test_verify_chain_after_inserts(ledger_conn) -> None:
    _seed_n_events(ledger_conn, 4)
    assert verify_chain(ledger_conn, "order_state_event") == 4


def test_verify_all_chained_passes_on_empty(ledger_conn) -> None:
    result = verify_all_chained(ledger_conn)
    assert all(v == 0 for v in result.values())
    assert set(result.keys()) == {
        "order_state_event", "fill_event", "position_snapshot",
        "strategy_decision", "reconciliation_proof",
    }


def test_tamper_with_this_hash_is_detected(ledger_conn) -> None:
    _seed_n_events(ledger_conn, 2)
    # Tamper via DROP TRIGGER bypass — same shortcut the writer uses for
    # legitimate hash setting; here we use it to test detection.
    ledger_conn.execute("DROP TRIGGER IF EXISTS no_update_order_state_event")
    ledger_conn.execute(
        "UPDATE order_state_event SET this_hash='deadbeef' "
        "WHERE ledger_seq = (SELECT MIN(ledger_seq) FROM order_state_event)"
    )
    with pytest.raises(HashChainBroken):
        verify_chain(ledger_conn, "order_state_event")


def test_tamper_with_prev_hash_is_detected(ledger_conn) -> None:
    _seed_n_events(ledger_conn, 3)
    ledger_conn.execute("DROP TRIGGER IF EXISTS no_update_order_state_event")
    ledger_conn.execute(
        "UPDATE order_state_event SET prev_hash='deadbeef' WHERE ledger_seq = 2"
    )
    with pytest.raises(HashChainBroken):
        verify_chain(ledger_conn, "order_state_event")


def test_tamper_with_content_is_detected(ledger_conn) -> None:
    _seed_n_events(ledger_conn, 2)
    ledger_conn.execute("DROP TRIGGER IF EXISTS no_update_order_state_event")
    ledger_conn.execute(
        "UPDATE order_state_event SET reason = 'tampered' WHERE ledger_seq = 1"
    )
    with pytest.raises(HashChainBroken):
        verify_chain(ledger_conn, "order_state_event")
