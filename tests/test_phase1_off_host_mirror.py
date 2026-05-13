"""Phase 1 — off-host append-only mirror."""
from __future__ import annotations

from trading_bot.ledger import (
    OrderIntent, append_state_event, insert_order_master,
    mirror_event, mirror_order_master, verify_chain,
)


def _seed(conn) -> str:
    intent = OrderIntent(
        client_order_id="20260513_T_SPY_1", strategy_id="T", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    seq1 = append_state_event(conn, order_uid=uid, to_state="intent")
    seq2 = append_state_event(conn, order_uid=uid, to_state="submitted")
    return uid


def test_mirror_event_preserves_hash(ledger_pair) -> None:
    ledger, mirror = ledger_pair
    uid = _seed(ledger)
    mirror_order_master(mirror, ledger, uid)
    for seq in (1, 2):
        mirror_event(mirror, "order_state_event", ledger, seq)

    cur_l = ledger.cursor()
    cur_m = mirror.cursor()
    cur_l.execute("SELECT ledger_seq, this_hash FROM order_state_event ORDER BY ledger_seq")
    cur_m.execute("SELECT ledger_seq, this_hash FROM order_state_event ORDER BY ledger_seq")
    assert cur_l.fetchall() == cur_m.fetchall()


def test_mirror_chain_independently_verifies(ledger_pair) -> None:
    ledger, mirror = ledger_pair
    uid = _seed(ledger)
    mirror_order_master(mirror, ledger, uid)
    for seq in (1, 2):
        mirror_event(mirror, "order_state_event", ledger, seq)
    assert verify_chain(mirror, "order_state_event") == 2


def test_mirror_event_is_idempotent(ledger_pair) -> None:
    ledger, mirror = ledger_pair
    uid = _seed(ledger)
    mirror_order_master(mirror, ledger, uid)
    mirror_event(mirror, "order_state_event", ledger, 1)
    mirror_event(mirror, "order_state_event", ledger, 1)  # second time, no error
    cur = mirror.cursor()
    cur.execute("SELECT COUNT(*) FROM order_state_event")
    assert cur.fetchone()[0] == 1
