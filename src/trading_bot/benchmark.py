"""SPY benchmark prices. Used by fitness function for alpha calc.

Tries Alpaca daily bars first; falls back to stooq.com (free, no API key).
"""
from __future__ import annotations

import datetime as dt
from io import StringIO
from pathlib import Path

import pandas as pd
import requests


class SpyBenchmark:
    def __init__(self, *, cache_path: str | Path = "data/spy_benchmark.parquet"):
        self.cache_path = Path(cache_path)

    def get(self, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            return self._fetch_alpaca(start=start, end=end)
        except Exception:
            return self._fetch_stooq(start=start, end=end)

    def _fetch_alpaca(self, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        from trading_bot.config import Settings
        from trading_bot.market_data import MarketDataClient

        client = MarketDataClient(Settings())
        bars = client.get_daily_bars("SPY", lookback_days=(end - start).days + 5)
        bars.index = pd.to_datetime(bars.index)
        return bars.loc[start.isoformat() : end.isoformat(), ["close"]]

    def _fetch_stooq(self, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        url = (
            "https://stooq.com/q/d/l/?s=spy.us&i=d"
            f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        df["Date"] = pd.to_datetime(df["Date"])
        return df.set_index("Date").rename(columns={"Close": "close"})[["close"]]

    @staticmethod
    def period_return(df: pd.DataFrame) -> float:
        if len(df) < 2:
            return 0.0
        return float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0)
