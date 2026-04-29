"""Tests for src/trading_bot/position_protection.py — open-position auto-protect."""
from __future__ import annotations

from decimal import Decimal

import pytest


def test_decide_protect_when_stop_below_current():
    """Stop level computed via max(EMA20, last_close*(1-stop_pct)) is below
    current price → place a stop, don't flatten."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=100.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "protect"
    # stop = max(95, 100*0.95) = max(95, 95) = 95 — equality goes to PROTECT here
    # because the comparison is stop < current (95 < 100 → True).
    assert stop == pytest.approx(95.0)


def test_decide_flatten_when_ema_above_current():
    """Price below EMA-20 → strategy stop sits above current → flatten."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=90.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "flatten"
    assert stop == pytest.approx(95.0)


def test_decide_pct_stop_wins_when_ema_far_below():
    """When EMA-20 is well below the % floor, the % floor is the stop."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=100.0, ema_20=50.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "protect"
    assert stop == pytest.approx(95.0)  # 100 * 0.95


def test_decide_boundary_equality_goes_to_flatten():
    """Spec: boundary case (stop == current) is FLATTEN. The check is `stop < current`."""
    from trading_bot.position_protection import _decide
    decision, _stop = _decide(
        current_price=95.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    # 95*(1-0.05)=90.25; max(95, 90.25)=95 == current → flatten
    assert decision == "flatten"


# ----------------------------------------------------------------------
# evaluate_and_act
# ----------------------------------------------------------------------

import pandas as pd
from unittest.mock import MagicMock


def _bars_with_close_and_ema(*, last_close: float, ema_20: float) -> pd.DataFrame:
    """Return a 30-bar DataFrame so compute_indicators won't be called by tests
    (we patch it). The DataFrame just has to be non-empty."""
    return pd.DataFrame({"close": [last_close] * 30})


def _stub_indicators(*, last_close: float, ema_20: float):
    from trading_bot.market_data import Indicators
    return Indicators(
        last_close=last_close, rsi_14=50.0, macd=0.0, macd_signal=0.0,
        ema_20=ema_20, return_5d=0.0,
    )


def _make_position(symbol: str, qty: str, asset_class: str = "us_equity",
                   current_price: str = "100"):
    from trading_bot.alpaca_client import Position
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        market_value=Decimal("1000"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal(current_price),
        unrealized_pl=Decimal("0"),
        asset_class=asset_class,
    )


def test_evaluate_and_act_long_stock_healthy_places_stop(monkeypatch):
    """Healthy long stock during RTH → place stop, no flatten."""
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )

    client = MagicMock()
    client.place_protective_stop.return_value = "stop-1"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert len(actions) == 1
    a = actions[0]
    assert a.symbol == "AAPL"
    assert a.outcome == "stop_placed"
    assert a.stop_price == pytest.approx(95.0)
    assert a.current_price == pytest.approx(100.0)

    client.place_protective_stop.assert_called_once()
    kw = client.place_protective_stop.call_args.kwargs
    assert kw["symbol"] == "AAPL"
    assert kw["qty"] == Decimal("10")
    assert kw["position_side"] == OrderSide.BUY
    assert kw["asset_class"] == AssetClass.STOCK
    client.place_market_order.assert_not_called()


def test_evaluate_and_act_long_stock_broken_during_rth_flattens(monkeypatch):
    """Broken long stock during RTH → market-flatten."""
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=90.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=90.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_market_order.return_value = "flat-1"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10", current_price="90")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert actions[0].outcome == "flattened"
    client.place_market_order.assert_called_once_with(
        symbol="AAPL", qty=10.0, side=OrderSide.SELL,
        asset_class=AssetClass.STOCK,
    )
    client.place_protective_stop.assert_not_called()


def test_evaluate_and_act_long_stock_broken_off_hours_defers(monkeypatch):
    """Broken stock outside RTH → defer (Alpaca rejects market sell off-hours)."""
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=90.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=90.0, ema_20=95.0,
    )
    client = MagicMock()

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10", current_price="90")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=False,
    )

    assert actions[0].outcome == "deferred_off_hours"
    client.place_market_order.assert_not_called()
    client.place_protective_stop.assert_not_called()


def test_evaluate_and_act_long_stock_healthy_off_hours_places_stop(monkeypatch):
    """Healthy stock off-hours → still place stop (GTC rests into next session)."""
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_protective_stop.return_value = "stop-x"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=False,
    )

    assert actions[0].outcome == "stop_placed"
    client.place_protective_stop.assert_called_once()


def test_evaluate_and_act_crypto_off_hours_still_flattens(monkeypatch):
    """Crypto trades 24/7 — broken crypto outside RTH still gets flattened."""
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=4.0, ema_20=5.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=4.0, ema_20=5.0,
    )
    client = MagicMock()
    client.place_market_order.return_value = "flat-c"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("DOTUSD", "100", asset_class="crypto",
                                   current_price="4")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=False,
    )

    assert actions[0].outcome == "flattened"
    client.place_market_order.assert_called_once_with(
        symbol="DOTUSD", qty=100.0, side=OrderSide.SELL,
        asset_class=AssetClass.CRYPTO,
    )


def test_evaluate_and_act_alpaca_failure_records_failed(monkeypatch):
    """Alpaca exception during order submit → outcome=failed, loop continues."""
    from trading_bot import position_protection as pp
    from trading_bot.exceptions import AlpacaClientError

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_protective_stop.side_effect = AlpacaClientError("rate limit")

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[
            _make_position("AAPL", "10"),
            _make_position("MSFT", "5"),
        ],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert len(actions) == 2
    assert all(a.outcome == "failed" for a in actions)
    assert "rate limit" in actions[0].error
    # Both positions attempted — failure on first did not abort the loop.
    assert client.place_protective_stop.call_count == 2


def test_evaluate_and_act_market_data_failure_records_failed(monkeypatch):
    """get_daily_bars raises → outcome=failed, no order submitted."""
    from trading_bot import position_protection as pp
    from trading_bot.exceptions import AlpacaClientError

    md = MagicMock()
    md.get_daily_bars.side_effect = AlpacaClientError("bars unavailable")
    client = MagicMock()

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert actions[0].outcome == "failed"
    assert "bars unavailable" in actions[0].error
    client.place_protective_stop.assert_not_called()
    client.place_market_order.assert_not_called()


def test_evaluate_and_act_short_position_uses_buy_actions(monkeypatch):
    """Short position (qty < 0): protective action takes the BUY side."""
    from trading_bot.alpaca_client import OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_protective_stop.return_value = "stop-s"

    short = _make_position("AAPL", "-10")
    pp.evaluate_and_act(
        client=client, market_data=md, unprotected=[short],
        stop_pct=Decimal("0.05"), now_in_market_hours=True,
    )

    kw = client.place_protective_stop.call_args.kwargs
    assert kw["position_side"] == OrderSide.SELL  # short side
    # qty passed positive to Alpaca
    assert kw["qty"] == Decimal("10")
