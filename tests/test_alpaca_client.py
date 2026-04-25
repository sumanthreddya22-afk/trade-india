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


def test_place_order_with_stop_loss(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-1", status="accepted", filled_qty="0", filled_avg_price=None)
        stop = MagicMock(id="stop-1", status="accepted", filled_qty="0", filled_avg_price=None)
        MockTC.return_value.submit_order.side_effect = [entry, stop]

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
        assert MockTC.return_value.submit_order.call_count == 2


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


def test_place_order_rolls_back_on_stop_failure(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-1", status="accepted", filled_qty="0", filled_avg_price=None)
        MockTC.return_value.submit_order.side_effect = [entry, RuntimeError("stop failed")]

        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="AAPL",
            qty=Decimal("10"),
            side=OrderSide.BUY,
            asset_class=AssetClass.STOCK,
            limit_price=Decimal("195.00"),
            stop_loss_price=Decimal("190.00"),
        )
        with pytest.raises(AlpacaClientError, match="stop-loss"):
            client.place_order_with_stop_loss(req)
        # entry was canceled
        MockTC.return_value.cancel_order_by_id.assert_called_once_with("entry-1")
