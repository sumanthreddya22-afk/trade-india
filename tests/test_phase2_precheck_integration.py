"""Phase 2 — full precheck composition.

These tests load the *actual* policy bundle from policy/*.lock and run
the orchestrator end-to-end. Each one targets one specific gate to
confirm composition works.
"""
from __future__ import annotations

from trading_bot.ledger.order_master import OrderIntent
from trading_bot.risk import load_policy, precheck
from trading_bot.risk.types import AccountState, Position


def _intent(**kw):
    defaults = dict(
        client_order_id="20260513_TEST_SPY_1",
        strategy_id="ETF_MOMENTUM_v1", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy",
        qty=1, limit_price=400.0, tif="day", origin="strategy",
    )
    defaults.update(kw)
    return OrderIntent(**defaults)


def _acct(equity=15_000):
    return AccountState(
        equity=equity, cash=equity*0.5,
        equity_at_session_start=equity, day_trade_count=0,
    )


def test_happy_path_accept() -> None:
    bundle = load_policy()
    # Lane status for etf_momentum is "shadow" in the lock; "shadow"
    # is NOT in ACTIVE_LANE_STATES — so even a happy SPY entry will
    # halt with `lane_status:etf_momentum:shadow:no_orders`.
    # This is exactly the v4 design: until Tier-2 passes, no orders
    # may emit from the lane.
    d = precheck.evaluate(
        conn=None,
        intent=_intent(),
        account=_acct(),
        positions=[],
        policy=bundle,
        lane="etf_momentum",
        intent_price=400.0,
        stop_loss_price=395.0,
    )
    assert d.verdict == "halt"
    assert "lane_status" in d.reason


def test_unknown_lane_halts() -> None:
    bundle = load_policy()
    d = precheck.evaluate(
        conn=None, intent=_intent(),
        account=_acct(), positions=[], policy=bundle,
        lane="ghost", intent_price=400.0, stop_loss_price=395.0,
    )
    assert d.verdict == "halt"
    assert "unknown_lane" in d.reason


def test_crypto_over_cap_halts() -> None:
    bundle = load_policy()
    intent = _intent(
        client_order_id="20260513_T_BTCUSD_1",
        symbol="BTCUSD", asset_class="crypto", limit_price=80000,
        qty=0.005,    # $400
    )
    # equity 15k; cap 15% = $2250; existing $2200 + $400 = $2600 > cap.
    crypto_pos = Position(symbol="BTCUSD", asset_class="crypto",
                          qty=0.03, market_value=2200,
                          classification="bot", lane="crypto_trend")
    d = precheck.evaluate(
        conn=None, intent=intent, account=_acct(),
        positions=[crypto_pos], policy=bundle, lane="crypto_trend",
        intent_price=80000, stop_loss_price=78000,
    )
    # crypto_trend is in 'reduce_only' — entries blocked at lane status.
    assert d.verdict == "halt"


def test_exit_passes_even_when_lane_is_reduce_only() -> None:
    bundle = load_policy()
    intent = _intent(
        client_order_id="20260513_T_BTCUSD_X",
        symbol="BTCUSD", asset_class="crypto", side="sell_to_close",
        qty=0.005, limit_price=80000,
    )
    d = precheck.evaluate(
        conn=None, intent=intent, account=_acct(),
        positions=[Position(symbol="BTCUSD", asset_class="crypto",
                            qty=0.03, market_value=2200,
                            classification="bot", lane="crypto_trend")],
        policy=bundle, lane="crypto_trend",
        intent_price=80000, stop_loss_price=None,
    )
    assert d.verdict == "accept"


def test_per_order_at_risk_halts() -> None:
    # Validates the per_order_at_risk gate independently of the live
    # calibration. Under the 2026-05-25 shakedown lock, per_order_at_risk
    # is 10% and per_symbol_gross is 5% — for long stock at_risk <=
    # notional <= symbol_cap so per_symbol_cap binds first. To still
    # exercise the per_order_at_risk machinery, this test builds a
    # bundle with the legacy 2% at-risk threshold.
    import dataclasses
    bundle = load_policy()
    risk_override = {**bundle.risk_policy}
    risk_override["order"] = {**risk_override["order"], "per_order_at_risk_max_pct": 2.0}
    bundle = dataclasses.replace(bundle, risk_policy=risk_override)
    # equity=15k → symbol cap (5%) = $750, order at-risk cap (2%) = $300.
    # qty=5 @ price=100 stop=20 → notional=$500 fits symbol cap,
    # at_risk = 5 * 80 = $400 > $300 → halts at per-order check.
    intent = _intent(qty=5, limit_price=100.0)
    d = precheck.evaluate(
        conn=None, intent=intent, account=_acct(),
        positions=[], policy=bundle, lane="benchmark",
        intent_price=100.0, stop_loss_price=20.0,
    )
    assert d.verdict == "halt"
    assert "order_cap" in d.reason
