"""Phase 3 — drift monitor: 20-trade live-vs-model slippage."""
from __future__ import annotations

import datetime as dt

from trading_bot.execution import compute_drift
from trading_bot.ledger import (
    OrderIntent, append_fill_event, append_state_event, insert_order_master,
)


def _seed_filled_order(conn, *, cid: str, fill_price: float,
                       symbol: str = "SPY") -> str:
    intent = OrderIntent(
        client_order_id=cid, strategy_id="BENCH", strategy_ver=1,
        symbol=symbol, asset_class="equity", side="buy",
        qty=10, limit_price=100.0, tif="day", origin="strategy",
    )
    uid = insert_order_master(conn, intent)
    append_state_event(conn, order_uid=uid, to_state="intent")
    append_state_event(conn, order_uid=uid, to_state="submitted")
    append_state_event(conn, order_uid=uid, to_state="acked",
                       broker_order_id=f"brk-{cid}")
    append_fill_event(
        conn, order_uid=uid, broker_fill_id=f"fill-{cid}",
        symbol=symbol, qty=10, price=fill_price,
    )
    return uid


def test_drift_well_within_model_no_breach(ledger_conn) -> None:
    # 3 fills at 100.05 buy fill, modelled mid=100. Realised slip = 5 bps.
    _seed_filled_order(ledger_conn, cid="20260513_BENCH_SPY_1",
                       fill_price=100.05)
    _seed_filled_order(ledger_conn, cid="20260513_BENCH_SPY_2",
                       fill_price=100.05)
    _seed_filled_order(ledger_conn, cid="20260513_BENCH_SPY_3",
                       fill_price=100.05)
    rep = compute_drift(
        ledger_conn, lane="benchmark",
        decision_mid_lookup=lambda row: 100.0,
        modelled_mean_bps=10.0,             # model expects 10 bps slip
        rolling_window=20, tolerance_multiplier=2.0,
    )
    assert rep.n_trades == 3
    assert abs(rep.realised_mean_bps - 5.0) < 0.01
    assert rep.breach is False


def test_drift_breach_recommends_demote(ledger_conn) -> None:
    # 3 fills at 101 buy, mid=100 -> realised slip 100 bps.
    for i in range(3):
        _seed_filled_order(ledger_conn, cid=f"20260513_BENCH_SPY_{i}",
                           fill_price=101.0)
    rep = compute_drift(
        ledger_conn, lane="benchmark",
        decision_mid_lookup=lambda row: 100.0,
        modelled_mean_bps=10.0, rolling_window=20, tolerance_multiplier=2.0,
    )
    assert rep.breach is True
    assert rep.recommendation == "demote:benchmark"


def test_drift_empty_history_no_breach(ledger_conn) -> None:
    rep = compute_drift(
        ledger_conn, lane="benchmark",
        decision_mid_lookup=lambda row: 100.0,
        modelled_mean_bps=10.0,
    )
    assert rep.n_trades == 0
    assert rep.breach is False
