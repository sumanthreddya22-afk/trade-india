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
