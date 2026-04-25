from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.market_data import (
    Indicators,
    MarketDataClient,
    compute_indicators,
)


def _make_bars_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=pd.date_range("2026-04-01", periods=n, freq="D", tz="UTC"),
    )


def test_compute_indicators_returns_expected_keys():
    df = _make_bars_df([100 + i * 0.5 for i in range(40)])
    ind = compute_indicators(df)
    assert isinstance(ind, Indicators)
    assert isinstance(ind.rsi_14, float)
    assert isinstance(ind.macd, float)
    assert isinstance(ind.macd_signal, float)
    assert isinstance(ind.ema_20, float)
    assert isinstance(ind.return_5d, float)
    assert isinstance(ind.last_close, float)


def test_compute_indicators_rsi_high_for_uptrend():
    df = _make_bars_df([100 + i for i in range(40)])
    ind = compute_indicators(df)
    assert ind.rsi_14 > 70


def test_compute_indicators_rsi_low_for_downtrend():
    df = _make_bars_df([100 - i for i in range(40)])
    ind = compute_indicators(df)
    assert ind.rsi_14 < 30


def test_compute_indicators_handles_short_series():
    df = _make_bars_df([100.0, 101.0, 102.0])
    with pytest.raises(ValueError, match="at least"):
        compute_indicators(df)


def test_market_data_client_get_bars():
    fake_settings = MagicMock(
        alpaca_api_key="k",
        alpaca_api_secret="s",
        alpaca_base_url="https://paper-api.alpaca.markets/v2",
    )

    fake_bar = MagicMock()
    fake_bar.timestamp = datetime(2026, 4, 25, tzinfo=timezone.utc)
    fake_bar.open = 195.0
    fake_bar.high = 196.0
    fake_bar.low = 194.0
    fake_bar.close = 195.5
    fake_bar.volume = 1_000_000

    fake_response = MagicMock()
    fake_response.data = {"AAPL": [fake_bar] * 30}

    with patch("trading_bot.market_data.StockHistoricalDataClient") as MockData:
        MockData.return_value.get_stock_bars.return_value = fake_response
        client = MarketDataClient(fake_settings)
        df = client.get_daily_bars("AAPL", lookback_days=30)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 30
        assert "close" in df.columns
