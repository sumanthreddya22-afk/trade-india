# tests/test_alpaca_client.py
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.alpaca_client import AlpacaClient, AccountSnapshot, Position
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled


@pytest.fixture
def fake_settings():
    s = MagicMock()
    s.alpaca_api_key = "k"
    s.alpaca_api_secret = "s"
    s.alpaca_base_url = "https://paper-api.alpaca.markets/v2"
    s.bot_mode = "paper"
    return s


def test_client_refuses_non_paper_url(fake_settings):
    fake_settings.alpaca_base_url = "https://api.alpaca.markets/v2"  # live URL
    with pytest.raises(LiveModeDisabled):
        AlpacaClient(fake_settings)


def test_get_account_returns_snapshot(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        mock_account = MagicMock()
        mock_account.equity = "100000.50"
        mock_account.cash = "25000.10"
        mock_account.buying_power = "50000.20"
        mock_account.portfolio_value = "100000.50"
        MockTC.return_value.get_account.return_value = mock_account

        client = AlpacaClient(fake_settings)
        snap = client.get_account()
        assert isinstance(snap, AccountSnapshot)
        assert snap.equity == Decimal("100000.50")
        assert snap.cash == Decimal("25000.10")


def test_get_positions_returns_list(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_pos.qty = "10"
        mock_pos.market_value = "2000.00"
        mock_pos.avg_entry_price = "195.50"
        mock_pos.unrealized_pl = "50.00"
        mock_pos.asset_class = "us_equity"
        MockTC.return_value.get_all_positions.return_value = [mock_pos]

        client = AlpacaClient(fake_settings)
        positions = client.get_positions()
        assert len(positions) == 1
        p = positions[0]
        assert isinstance(p, Position)
        assert p.symbol == "AAPL"
        assert p.qty == Decimal("10")
        assert p.market_value == Decimal("2000.00")


def test_get_account_wraps_api_error(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.get_account.side_effect = RuntimeError("boom")
        client = AlpacaClient(fake_settings)
        with pytest.raises(AlpacaClientError):
            client.get_account()


from trading_bot.alpaca_client import OrderRequest, OrderResult, OrderSide, AssetClass


def test_place_stock_order_uses_bracket(fake_settings):
    """Stock orders should use a single bracket submission with stop_loss leg."""
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        leg = MagicMock(id="stop-1", type="stop")
        entry = MagicMock(id="entry-1", legs=[leg])
        MockTC.return_value.submit_order.return_value = entry

        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="AAPL",
            qty=Decimal("10"),
            side=OrderSide.BUY,
            asset_class=AssetClass.STOCK,
            limit_price=Decimal("195.00"),
            stop_loss_price=Decimal("190.00"),
        )
        result = client.place_order_with_stop_loss(req)
        assert isinstance(result, OrderResult)
        assert result.entry_order_id == "entry-1"
        assert result.stop_loss_order_id == "stop-1"
        # Bracket = single submission, not two
        assert MockTC.return_value.submit_order.call_count == 1


def test_place_order_requires_stop_loss(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient"):
        client = AlpacaClient(fake_settings)
        with pytest.raises(ValueError, match="stop_loss_price"):
            OrderRequest(
                symbol="AAPL",
                qty=Decimal("10"),
                side=OrderSide.BUY,
                asset_class=AssetClass.STOCK,
                limit_price=Decimal("195.00"),
                stop_loss_price=None,
            )


def test_place_stock_bracket_failure_raises(fake_settings):
    """If the bracket submission itself fails, raise AlpacaClientError."""
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.side_effect = RuntimeError("rejected")
        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="AAPL",
            qty=Decimal("10"),
            side=OrderSide.BUY,
            asset_class=AssetClass.STOCK,
            limit_price=Decimal("195.00"),
            stop_loss_price=Decimal("190.00"),
        )
        with pytest.raises(AlpacaClientError, match="bracket"):
            client.place_order_with_stop_loss(req)


def test_place_crypto_uses_market_then_stop(fake_settings):
    """Crypto can't use bracket — uses market entry then separate stop."""
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-c", legs=[])
        stop = MagicMock(id="stop-c")
        MockTC.return_value.submit_order.side_effect = [entry, stop]
        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="BTC/USD",
            qty=Decimal("0.001"),
            side=OrderSide.BUY,
            asset_class=AssetClass.CRYPTO,
            limit_price=Decimal("70000"),
            stop_loss_price=Decimal("68000"),
        )
        result = client.place_order_with_stop_loss(req)
        assert result.entry_order_id == "entry-c"
        assert result.stop_loss_order_id == "stop-c"
        assert MockTC.return_value.submit_order.call_count == 2


def test_place_crypto_uses_stop_limit_not_plain_stop(fake_settings):
    """Alpaca rejects plain stop orders for crypto with 'invalid order type
    for crypto order'. Regression: previously the bot used StopOrderRequest,
    so every crypto stop silently failed and positions filled naked.
    """
    from alpaca.trading.requests import (
        MarketOrderRequest as AlpacaMarketOrderRequest,
        StopLimitOrderRequest as AlpacaStopLimitOrderRequest,
    )

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-c", legs=[])
        stop = MagicMock(id="stop-c")
        MockTC.return_value.submit_order.side_effect = [entry, stop]
        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="BTC/USD",
            qty=Decimal("0.001"),
            side=OrderSide.BUY,
            asset_class=AssetClass.CRYPTO,
            limit_price=Decimal("70000"),
            stop_loss_price=Decimal("68000"),
        )
        client.place_order_with_stop_loss(req)
        calls = MockTC.return_value.submit_order.call_args_list
        assert len(calls) == 2
        entry_arg = calls[0].args[0]
        stop_arg = calls[1].args[0]
        assert isinstance(entry_arg, AlpacaMarketOrderRequest)
        assert isinstance(stop_arg, AlpacaStopLimitOrderRequest)
        # Trigger preserved; limit set below trigger to give fill room.
        assert float(stop_arg.stop_price) == 68000.0
        assert float(stop_arg.limit_price) < 68000.0


def test_place_crypto_cancels_pending_entry_when_stop_fails(fake_settings):
    """If the stop submission fails AND the entry hasn't filled yet, the
    pending entry must be cancelled — otherwise it can fill later naked.
    Regression: this was the second half of the naked-position bug.
    """
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-c", legs=[])
        MockTC.return_value.submit_order.side_effect = [
            entry,
            RuntimeError("invalid order type for crypto order"),
        ]
        # Verifier sees no live position (entry hasn't filled).
        MockTC.return_value.get_all_positions.return_value = []

        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="BTC/USD",
            qty=Decimal("0.001"),
            side=OrderSide.BUY,
            asset_class=AssetClass.CRYPTO,
            limit_price=Decimal("70000"),
            stop_loss_price=Decimal("68000"),
        )
        with pytest.raises(AlpacaClientError, match="pending entry has been cancelled"):
            client.place_order_with_stop_loss(req)
        MockTC.return_value.cancel_order_by_id.assert_called_once_with("entry-c")


def test_verifier_recognises_stop_limit_as_protection(fake_settings):
    """A live stop_limit order on the symbol must count as protection so
    the verifier does NOT flatten an already-protected position."""
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        # One filled position and one live stop-limit on the same symbol.
        live_pos = MagicMock(symbol="BTC/USD", qty="0.001")
        live_stop = MagicMock(symbol="BTC/USD", type="OrderType.STOP_LIMIT")
        MockTC.return_value.get_all_positions.return_value = [live_pos]
        MockTC.return_value.get_orders.return_value = [live_stop]

        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="BTC/USD",
            qty=Decimal("0.001"),
            side=OrderSide.BUY,
            asset_class=AssetClass.CRYPTO,
            limit_price=Decimal("70000"),
            stop_loss_price=Decimal("68000"),
        )
        action = client._verify_crypto_stop_or_flatten(req, entry_id="entry-c")
        assert action == "has_stop"
        # Critically, no flatten order should have been submitted.
        MockTC.return_value.submit_order.assert_not_called()


def test_place_protective_stop_stock_long(fake_settings):
    """Long stock: places plain StopOrderRequest with side=SELL, GTC."""
    from decimal import Decimal
    from alpaca.trading.requests import StopOrderRequest
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-123")
        client = AlpacaClient(fake_settings)
        order_id = client.place_protective_stop(
            symbol="AAPL",
            qty=Decimal("10"),
            position_side=OrderSide.BUY,  # long
            asset_class=AssetClass.STOCK,
            stop_price=Decimal("180.00"),
        )

    assert order_id == "stop-123"
    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert isinstance(call_arg, StopOrderRequest)
    assert call_arg.symbol == "AAPL"
    assert float(call_arg.qty) == 10.0
    assert str(call_arg.side).lower().endswith("sell")
    assert float(call_arg.stop_price) == 180.00


def test_place_protective_stop_crypto_long_uses_stop_limit(fake_settings):
    """Crypto long: places StopLimitOrderRequest because Alpaca rejects plain stops on crypto.
    Symbol is rewritten 'DOTUSD' → 'DOT/USD' for orders."""
    from decimal import Decimal
    from alpaca.trading.requests import StopLimitOrderRequest
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-c1")
        client = AlpacaClient(fake_settings)
        order_id = client.place_protective_stop(
            symbol="DOTUSD",  # position-form symbol
            qty=Decimal("100"),
            position_side=OrderSide.BUY,  # long
            asset_class=AssetClass.CRYPTO,
            stop_price=Decimal("5.00"),
        )

    assert order_id == "stop-c1"
    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert isinstance(call_arg, StopLimitOrderRequest)
    assert call_arg.symbol == "DOT/USD"
    assert float(call_arg.stop_price) == 5.00
    # Sell-stop limit must be ≤ trigger; existing CRYPTO_STOP_LIMIT_BUFFER_PCT = 5%.
    assert float(call_arg.limit_price) <= 5.00


def test_place_protective_stop_short_uses_buy_side(fake_settings):
    """Short position (rare but supported): protective stop is a BUY stop above current."""
    from decimal import Decimal
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-s")
        client = AlpacaClient(fake_settings)
        client.place_protective_stop(
            symbol="AAPL",
            qty=Decimal("5"),
            position_side=OrderSide.SELL,  # short
            asset_class=AssetClass.STOCK,
            stop_price=Decimal("200.00"),
        )

    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert str(call_arg.side).lower().endswith("buy")


def test_place_protective_stop_crypto_short_limit_above_trigger(fake_settings):
    """Crypto short → buy-stop_limit; Alpaca requires limit_price >= stop_price."""
    from decimal import Decimal
    from alpaca.trading.requests import StopLimitOrderRequest
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-cs")
        client = AlpacaClient(fake_settings)
        client.place_protective_stop(
            symbol="ETHUSD",
            qty=Decimal("0.5"),
            position_side=OrderSide.SELL,  # short
            asset_class=AssetClass.CRYPTO,
            stop_price=Decimal("3000.00"),
        )

    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert isinstance(call_arg, StopLimitOrderRequest)
    assert str(call_arg.side).lower().endswith("buy")
    assert call_arg.symbol == "ETH/USD"
    assert float(call_arg.limit_price) >= float(call_arg.stop_price)


def test_place_protective_stop_propagates_alpaca_errors(fake_settings):
    from decimal import Decimal
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide
    from trading_bot.exceptions import AlpacaClientError

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.side_effect = RuntimeError("rejected")
        client = AlpacaClient(fake_settings)
        with pytest.raises(AlpacaClientError, match="protective stop"):
            client.place_protective_stop(
                symbol="AAPL", qty=Decimal("1"), position_side=OrderSide.BUY,
                asset_class=AssetClass.STOCK, stop_price=Decimal("100"),
            )


def test_get_active_assets_returns_tradable(monkeypatch):
    from trading_bot.alpaca_client import TradableAsset

    mock_asset_a = MagicMock(
        symbol="NVDA",
        name="NVIDIA",
        exchange="NASDAQ",
        status="active",
        tradable=True,
        fractionable=True,
        asset_class="us_equity",
    )
    mock_asset_b = MagicMock(
        symbol="HALT",
        name="Halted Inc",
        exchange="NYSE",
        status="inactive",
        tradable=False,
        fractionable=False,
        asset_class="us_equity",
    )
    client = MagicMock()
    client.get_all_assets.return_value = [mock_asset_a, mock_asset_b]

    wrapper = AlpacaClient.__new__(AlpacaClient)
    wrapper._client = client

    result = wrapper.get_active_assets("us_equity")
    assert len(result) == 1
    assert result[0].symbol == "NVDA"
    assert isinstance(result[0], TradableAsset)
