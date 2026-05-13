"""Phase 1 — orphan recovery: find + transition."""
from __future__ import annotations

import datetime as dt

from trading_bot.ledger import (
    OrderIntent, append_state_event, find_orphans,
    insert_order_master, recover_orphan,
)


def _submit_order(conn, cid: str, *, submitted_at: dt.datetime) -> str:
    intent = OrderIntent(
        client_order_id=cid, strategy_id="T", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    append_state_event(conn, order_uid=uid, to_state="intent",
                       now=submitted_at)
    append_state_event(conn, order_uid=uid, to_state="submitted",
                       now=submitted_at)
    return uid


def test_find_orphans_returns_old_submitted(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(seconds=120)
    young = now - dt.timedelta(seconds=10)
    _submit_order(ledger_conn, "20260513_T_SPY_1", submitted_at=old)
    _submit_order(ledger_conn, "20260513_T_QQQ_1", submitted_at=young)

    orphans = find_orphans(ledger_conn, max_age_seconds=60, now=now)
    cids = {o.client_order_id for o in orphans}
    assert "20260513_T_SPY_1" in cids
    assert "20260513_T_QQQ_1" not in cids


def test_recover_orphan_found_at_broker(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(seconds=120)
    _submit_order(ledger_conn, "20260513_T_SPY_1", submitted_at=old)

    orphans = find_orphans(ledger_conn, max_age_seconds=60, now=now)
    assert len(orphans) == 1
    o = orphans[0]
    result = recover_orphan(ledger_conn, o,
                            broker_lookup=lambda cid: "brk-late-ack",
                            now=now)
    assert result == "acked"
    # No longer an orphan.
    assert find_orphans(ledger_conn, max_age_seconds=60, now=now) == []


def test_recover_orphan_not_found_at_broker(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(seconds=120)
    _submit_order(ledger_conn, "20260513_T_SPY_1", submitted_at=old)
    orphans = find_orphans(ledger_conn, max_age_seconds=60, now=now)
    result = recover_orphan(ledger_conn, orphans[0],
                            broker_lookup=lambda cid: None,
                            now=now)
    assert result == "cancelled"
