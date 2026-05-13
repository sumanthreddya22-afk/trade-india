"""Phase 3 — order_router: risk + freshness + idempotency + submission."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.execution import submit_order
from trading_bot.execution.order_router import SubmissionResult
from trading_bot.ingest import write_watermark
from trading_bot.ledger import OrderIntent
from trading_bot.risk import load_policy
from trading_bot.risk.types import AccountState, Position


def _intent(**kw) -> OrderIntent:
    defaults = dict(
        client_order_id="20260513_BENCH_SPY_1",
        strategy_id="BENCH", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=100.0, tif="day", origin="strategy",
    )
    defaults.update(kw)
    return OrderIntent(**defaults)


def _acct(equity=15000) -> AccountState:
    return AccountState(
        equity=equity, cash=equity * 0.5,
        equity_at_session_start=equity, day_trade_count=0,
    )


def _seed_fresh_watermark(conn, lane="equity") -> None:
    write_watermark(
        conn, source_id="alpaca", lane=lane,
        event_ts=dt.datetime.now(dt.timezone.utc),
    )


def _accepting_broker(**_kw):
    return {"ok": True, "broker_order_id": "brk-1"}


def _rejecting_broker(**_kw):
    return {"ok": False, "broker_order_id": None, "error": "throttle"}


def test_happy_path_submits_and_logs(ledger_conn) -> None:
    _seed_fresh_watermark(ledger_conn)
    bundle = load_policy()
    res = submit_order(
        conn=ledger_conn, intent=_intent(qty=1, limit_price=10),
        account=_acct(), positions=[], policy=bundle,
        lane="benchmark", quote_lane="equity",
        intent_price=10.0, stop_loss_price=9.95,
        broker_submit=_accepting_broker,
    )
    assert res.submitted is True
    assert res.risk_verdict == "accept"
    assert res.broker_order_id == "brk-1"

    # ledger writes
    cur = ledger_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM order_master")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT to_state FROM order_state_event "
                "ORDER BY ledger_seq")
    states = [r[0] for r in cur.fetchall()]
    assert states == ["intent", "submitted"]
    cur.execute("SELECT risk_decision FROM strategy_decision")
    assert cur.fetchone()[0] == "accept"


def test_risk_halt_writes_decision_no_submit(ledger_conn) -> None:
    bundle = load_policy()
    res = submit_order(
        conn=ledger_conn,
        intent=_intent(client_order_id="20260513_T_SPY_X"),
        account=_acct(), positions=[], policy=bundle,
        lane="mean_reversion",         # status=research_only -> halt
        quote_lane="equity",
        intent_price=10.0,
        broker_submit=_accepting_broker,
    )
    assert res.submitted is False
    assert res.risk_verdict == "halt"
    # no order_master row was written
    cur = ledger_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM order_master")
    assert cur.fetchone()[0] == 0
    # but a decision row IS written
    cur.execute("SELECT risk_decision FROM strategy_decision")
    assert cur.fetchone()[0] == "halt"


def test_stale_data_blocks_submission(ledger_conn) -> None:
    bundle = load_policy()
    # No watermark -> stale -> halt
    res = submit_order(
        conn=ledger_conn, intent=_intent(qty=1, limit_price=10),
        account=_acct(), positions=[], policy=bundle,
        lane="benchmark", quote_lane="equity",
        intent_price=10.0,
        broker_submit=_accepting_broker,
    )
    assert res.submitted is False
    assert "data_freshness" in res.reason or "no_watermark" in res.reason


def test_idempotent_block_on_active_cid(ledger_conn) -> None:
    _seed_fresh_watermark(ledger_conn)
    bundle = load_policy()
    cid = "20260513_BENCH_SPY_DUP"
    submit_order(
        conn=ledger_conn,
        intent=_intent(client_order_id=cid, qty=1, limit_price=10),
        account=_acct(), positions=[], policy=bundle,
        lane="benchmark", quote_lane="equity", intent_price=10.0,
        broker_submit=_accepting_broker,
    )
    # Second submit with same CID -> idempotent block.
    res2 = submit_order(
        conn=ledger_conn,
        intent=_intent(client_order_id=cid, qty=1, limit_price=10),
        account=_acct(), positions=[], policy=bundle,
        lane="benchmark", quote_lane="equity", intent_price=10.0,
        broker_submit=_accepting_broker,
    )
    assert res2.submitted is False
    assert "idempotent" in res2.reason


def test_broker_failure_writes_cancelled_state(ledger_conn) -> None:
    _seed_fresh_watermark(ledger_conn)
    bundle = load_policy()
    res = submit_order(
        conn=ledger_conn,
        intent=_intent(client_order_id="20260513_BENCH_SPY_FAIL",
                       qty=1, limit_price=10),
        account=_acct(), positions=[], policy=bundle,
        lane="benchmark", quote_lane="equity", intent_price=10.0,
        broker_submit=_rejecting_broker,
    )
    assert res.submitted is False
    assert res.reason == "broker_submit_failed"
    cur = ledger_conn.cursor()
    cur.execute("SELECT to_state FROM order_state_event ORDER BY ledger_seq")
    states = [r[0] for r in cur.fetchall()]
    assert states == ["intent", "cancelled"]


def test_reduce_path_uses_adjusted_qty(ledger_conn) -> None:
    _seed_fresh_watermark(ledger_conn)
    bundle = load_policy()
    # equity=15k; symbol cap 5%=$750; positions worth $700 in SPY
    # try to buy 10 @ 100 (notional 1000); reduce-to-fit: headroom 50/100=0.5
    captured = {}

    def capture_broker(**kw):
        captured.update(kw)
        return {"ok": True, "broker_order_id": "brk-X"}

    res = submit_order(
        conn=ledger_conn,
        intent=_intent(client_order_id="20260513_BENCH_SPY_R",
                       qty=10, limit_price=100.0),
        account=_acct(),
        positions=[Position(symbol="SPY", asset_class="equity", qty=7,
                            market_value=700, classification="bot")],
        policy=bundle,
        lane="benchmark", quote_lane="equity", intent_price=100.0,
        stop_loss_price=99.5,
        broker_submit=capture_broker,
    )
    assert res.submitted is True
    assert res.risk_verdict == "reduce"
    assert abs(captured["qty"] - 0.5) < 1e-6
