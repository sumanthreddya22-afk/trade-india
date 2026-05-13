"""Phase 3 — orphan-recovery loop wraps Phase 1's helper."""
from __future__ import annotations

import datetime as dt

from trading_bot.execution import orphan_loop
from trading_bot.ledger import (
    OrderIntent, append_state_event, insert_order_master,
)


def _submit(conn, *, cid: str, submitted_at: dt.datetime) -> str:
    intent = OrderIntent(
        client_order_id=cid, strategy_id="BENCH", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=100.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    append_state_event(conn, order_uid=uid, to_state="intent",
                       now=submitted_at)
    append_state_event(conn, order_uid=uid, to_state="submitted",
                       now=submitted_at)
    return uid


def test_run_once_recovers_old_orphans(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(seconds=120)
    young = now - dt.timedelta(seconds=10)
    _submit(ledger_conn, cid="20260513_BENCH_SPY_OLD", submitted_at=old)
    _submit(ledger_conn, cid="20260513_BENCH_SPY_YOUNG", submitted_at=young)

    out = orphan_loop.run_once(
        ledger_conn,
        broker_lookup=lambda cid: "brk-late-ack" if "OLD" in cid else None,
        max_age_seconds=60, now=now,
    )
    assert len(out) == 1
    assert out[0]["result"] == "acked"
    # Second call: no more orphans (the young one isn't old enough).
    out2 = orphan_loop.run_once(
        ledger_conn,
        broker_lookup=lambda cid: None,
        max_age_seconds=60, now=now,
    )
    assert out2 == []
