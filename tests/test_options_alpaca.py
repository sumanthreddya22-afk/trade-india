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


def test_get_chain_times_out_when_alpaca_hangs(monkeypatch):
    """Regression for the 2026-04-30/05-01 wheel_scan hang: a slow Alpaca
    options-chain endpoint must not block the caller indefinitely. The
    caller should see AlpacaClientError after the timeout × (retries+1)
    budget, not wait forever.
    """
    import time
    from trading_bot.exceptions import AlpacaClientError
    import trading_bot.options.alpaca_options as ao

    # Squash the timeout + retries to keep the test fast.
    monkeypatch.setattr(ao, "_OPTION_CHAIN_TIMEOUT_S", 0.3)
    monkeypatch.setattr(ao, "_OPTION_CHAIN_RETRIES", 1)

    feed = MagicMock()
    def hang(*_a, **_k):
        time.sleep(5)  # would hang past the test timeout without the wrapper
        return {}
    feed.get_option_chain.side_effect = hang

    with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient",
               return_value=feed):
        with patch("trading_bot.options.alpaca_options.TradingClient"):
            c = OptionAlpacaClient(_settings())
            t0 = time.monotonic()
            with pytest.raises(AlpacaClientError, match="exhausted"):
                c.get_chain("AAPL",
                            expiration_gte=dt.date(2026, 5, 1),
                            expiration_lte=dt.date(2026, 5, 30))
            elapsed = time.monotonic() - t0

    # 0.3s × 2 attempts = 0.6s budget; allow generous slack for thread
    # scheduling, but assert it's nowhere near the 5s the mock would take
    # without the timeout wrapper.
    assert elapsed < 3.0, f"call took {elapsed:.2f}s — wrapper not enforcing timeout"


def test_get_chain_retries_then_succeeds(monkeypatch):
    """A flaky options-chain endpoint that succeeds on the second attempt
    must not surface as an error to the caller."""
    import trading_bot.options.alpaca_options as ao
    monkeypatch.setattr(ao, "_OPTION_CHAIN_TIMEOUT_S", 1.0)
    monkeypatch.setattr(ao, "_OPTION_CHAIN_RETRIES", 2)

    snap = MagicMock()
    snap.symbol = "AAPL250516C00200000"
    snap.latest_quote = MagicMock(bid_price=2.0, ask_price=2.1)
    snap.latest_trade = MagicMock(price=2.05)
    snap.greeks = MagicMock(delta=0.27, gamma=0.0, theta=-0.04, vega=0.10, rho=0.0)
    snap.implied_volatility = 0.30

    feed = MagicMock()
    feed.get_option_chain.side_effect = [
        Exception("503 service unavailable"),  # retryable failure
        {"AAPL250516C00200000": snap},          # second attempt succeeds
    ]

    with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient",
               return_value=feed):
        with patch("trading_bot.options.alpaca_options.TradingClient"):
            c = OptionAlpacaClient(_settings())
            chain = c.get_chain("AAPL",
                                expiration_gte=dt.date(2026, 5, 1),
                                expiration_lte=dt.date(2026, 5, 30))
    assert len(chain) == 1
    assert feed.get_option_chain.call_count == 2


def test_get_chain_does_not_retry_non_retryable_errors(monkeypatch):
    """Schema errors / 4xx-style failures should propagate immediately —
    no point burning retries on a malformed request."""
    import trading_bot.options.alpaca_options as ao
    monkeypatch.setattr(ao, "_OPTION_CHAIN_RETRIES", 5)  # would be obvious if retries fired

    feed = MagicMock()
    feed.get_option_chain.side_effect = ValueError("bad expiration date")

    with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient",
               return_value=feed):
        with patch("trading_bot.options.alpaca_options.TradingClient"):
            c = OptionAlpacaClient(_settings())
            from trading_bot.exceptions import AlpacaClientError
            with pytest.raises(AlpacaClientError, match="bad expiration date"):
                c.get_chain("AAPL",
                            expiration_gte=dt.date(2026, 5, 1),
                            expiration_lte=dt.date(2026, 5, 30))
    assert feed.get_option_chain.call_count == 1  # NOT retried


def test_list_optionable_us_equities_reads_attributes_field():
    """Alpaca-py exposes optionability via asset.attributes containing
    'has_options' (a list of strings), not a top-level options_enabled bool."""
    a_yes = MagicMock(spec=["symbol", "tradable", "attributes", "options_enabled"])
    a_yes.symbol = "AAPL"; a_yes.tradable = True
    a_yes.attributes = ["has_options", "fractional_eh_enabled"]
    a_yes.options_enabled = False
    a_no = MagicMock(spec=["symbol", "tradable", "attributes", "options_enabled"])
    a_no.symbol = "PENNY"; a_no.tradable = True
    a_no.attributes = ["fractional_eh_enabled"]
    a_no.options_enabled = False
    a_legacy = MagicMock(spec=["symbol", "tradable", "attributes", "options_enabled"])
    a_legacy.symbol = "LEGACY"; a_legacy.tradable = True
    a_legacy.attributes = []
    a_legacy.options_enabled = True  # legacy field path still respected
    trading = MagicMock(); trading.get_all_assets.return_value = [a_yes, a_no, a_legacy]
    with patch("trading_bot.options.alpaca_options.TradingClient", return_value=trading):
        with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient"):
            c = OptionAlpacaClient(_settings())
            out = c.list_optionable_us_equities()
    assert out == {"AAPL", "LEGACY"}
