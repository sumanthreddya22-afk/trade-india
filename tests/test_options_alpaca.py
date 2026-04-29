import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.chain import ChainContract


def _settings():
    s = MagicMock()
    s.alpaca_api_key = "k"
    s.alpaca_api_secret = "s"
    s.alpaca_base_url = "https://paper-api.alpaca.markets/v2"
    return s


def test_get_chain_normalizes_snapshot():
    snap_call = MagicMock()
    snap_call.symbol = "AAPL250516C00200000"
    snap_call.latest_quote = MagicMock(bid_price=2.0, ask_price=2.10)
    snap_call.latest_trade = MagicMock(price=2.05)
    snap_call.greeks = MagicMock(delta=0.27, gamma=0.0, theta=-0.04, vega=0.10, rho=0.0)
    snap_call.implied_volatility = 0.30

    feed = MagicMock()
    feed.get_option_chain.return_value = {"AAPL250516C00200000": snap_call}

    with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient",
               return_value=feed):
        with patch("trading_bot.options.alpaca_options.TradingClient"):
            c = OptionAlpacaClient(_settings())
            chain = c.get_chain("AAPL", expiration_gte=dt.date(2026, 5, 1),
                                expiration_lte=dt.date(2026, 5, 30))
    assert len(chain) == 1
    cc = chain[0]
    assert isinstance(cc, ChainContract)
    assert cc.kind == "C" and cc.strike == 200.0
    assert cc.bid == 2.0 and cc.delta == 0.27


def test_submit_csp_sell_to_open_uses_limit_order():
    trading = MagicMock()
    submitted = MagicMock(id="ord-1")
    trading.submit_order.return_value = submitted
    with patch("trading_bot.options.alpaca_options.TradingClient",
               return_value=trading):
        with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient"):
            c = OptionAlpacaClient(_settings())
            order_id = c.sell_to_open(
                contract_symbol="AAPL250516P00190000", qty=1, limit_price=Decimal("2.10"),
            )
    assert order_id == "ord-1"
    trading.submit_order.assert_called_once()


def test_buy_to_close_returns_order_id():
    trading = MagicMock()
    trading.submit_order.return_value = MagicMock(id="ord-2")
    with patch("trading_bot.options.alpaca_options.TradingClient",
               return_value=trading):
        with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient"):
            c = OptionAlpacaClient(_settings())
            assert c.buy_to_close(
                contract_symbol="AAPL250516P00190000", qty=1, limit_price=Decimal("0.95"),
            ) == "ord-2"


def test_constructor_rejects_live_url():
    s = _settings()
    s.alpaca_base_url = "https://api.alpaca.markets/v2"
    from trading_bot.exceptions import LiveModeDisabled
    with pytest.raises(LiveModeDisabled):
        OptionAlpacaClient(s)
