from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

from trading_bot.config import Settings
from trading_bot.exceptions import AlpacaClientError


MIN_BARS_FOR_INDICATORS = 26  # MACD needs 26 periods of history


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Indicators:
    last_close: float
    rsi_14: float
    macd: float
    macd_signal: float
    ema_20: float
    return_5d: float


def compute_indicators(bars: pd.DataFrame) -> Indicators:
    if len(bars) < MIN_BARS_FOR_INDICATORS:
        raise ValueError(
            f"compute_indicators requires at least {MIN_BARS_FOR_INDICATORS} bars; got {len(bars)}"
        )
    close = bars["close"]
    rsi = RSIIndicator(close=close, window=14).rsi().iloc[-1]
    macd_obj = MACD(close=close)
    macd_val = macd_obj.macd().iloc[-1]
    macd_sig = macd_obj.macd_signal().iloc[-1]
    ema20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
    ret_5d = (close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 else 0.0
    return Indicators(
        last_close=float(close.iloc[-1]),
        rsi_14=float(rsi),
        macd=float(macd_val),
        macd_signal=float(macd_sig),
        ema_20=float(ema20),
        return_5d=float(ret_5d),
    )


def _is_crypto(symbol: str) -> bool:
    return "/" in symbol


class MarketDataClient:
    def __init__(self, settings: Settings) -> None:
        self._stock_client = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
        )
        self._crypto_client = CryptoHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
        )

    def get_daily_bars(self, symbol: str, lookback_days: int = 60) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days * 2)
        try:
            if _is_crypto(symbol):
                req = CryptoBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=start,
                    limit=lookback_days,
                )
                resp = self._crypto_client.get_crypto_bars(req)
            else:
                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=start,
                    limit=lookback_days,
                )
                resp = self._stock_client.get_stock_bars(req)
        except Exception as e:
            raise AlpacaClientError(f"get_daily_bars({symbol}) failed: {e}") from e
        bars = resp.data.get(symbol, [])
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(
            [
                {
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
                for b in bars
            ],
            index=pd.DatetimeIndex([b.timestamp for b in bars], name="timestamp"),
        )
        return df.tail(lookback_days)
