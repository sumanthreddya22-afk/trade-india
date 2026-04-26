"""Massive Market Data (Polygon) bar loader for the backtest harness.

Two ways to populate `data/backtest_bars.db` from Massive aggregates:

1. **Direct (cron-safe):** `MassiveBarLoader(api_key)` calls Polygon's REST
   API. Use when `POLYGON_API_KEY` is set in the environment. Adapts the
   Polygon CSV response into the same DataFrame shape that
   `MarketDataClient.get_daily_bars` returns, so `BarStore.warm()` can
   consume it transparently.

2. **CSV-fed (interactive):** `import_csv_into_bar_store(csv_text, ...)`
   takes a Polygon-shaped CSV string (`v,vw,o,c,h,l,t,n`) and writes rows
   into a `BarStore`. Used when the environment doesn't have a key but
   we want to populate the cache one-time from MCP responses or saved
   CSVs.

Polygon symbol convention:
- Stocks: `SPY`, `AAPL` — same as ours.
- Crypto: `X:BTCUSD` (no slash). We accept either form on input and
  translate; output rows always store the original `BTC/USD` symbol so
  the backtest sees the same identity it uses live.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import requests
from sqlalchemy.orm import Session

from trading_bot.backtest.bar_store import BarStore, _BarRow

POLYGON_BASE = "https://api.polygon.io"
HTTP_TIMEOUT = 30


def to_polygon_ticker(symbol: str) -> str:
    """`BTC/USD` → `X:BTCUSD`; otherwise unchanged."""
    if "/" in symbol:
        return "X:" + symbol.replace("/", "")
    return symbol


def _parse_csv_to_rows(csv_text: str, symbol: str) -> list[dict]:
    """Parse Polygon's `v,vw,o,c,h,l,t,n` CSV into normalized rows."""
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for r in reader:
        try:
            ts_ms = int(r["t"])
            d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
            rows.append({
                "symbol": symbol,
                "date": d,
                "open": float(r["o"]),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
                "volume": float(r["v"]),
            })
        except (KeyError, ValueError):
            continue
    return rows


# ---- direct loader (uses POLYGON_API_KEY) ------------------------------


class MassiveBarLoader:
    """Polygon REST client adapter that mirrors MarketDataClient's interface
    so it can be passed to `BarStore.warm` interchangeably.

    The `get_daily_bars(symbol, lookback_days)` signature matches the live
    market_data client used by the backtest's existing warm path.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("POLYGON_API_KEY", "")
        if not self._api_key:
            raise RuntimeError(
                "POLYGON_API_KEY not set. Either export it, add it to .env, "
                "or use import_csv_into_bar_store() with hand-fed CSVs."
            )

    def get_daily_bars(self, symbol: str, lookback_days: int = 60) -> pd.DataFrame:
        """Same shape as MarketDataClient.get_daily_bars — returns a DF
        indexed by timestamp with open/high/low/close/volume columns."""
        to_d = date.today()
        from_d = to_d - timedelta(days=lookback_days * 2)
        return self.get_range(symbol, from_d=from_d, to_d=to_d)

    def get_range(self, symbol: str, *, from_d: date, to_d: date) -> pd.DataFrame:
        ticker = to_polygon_ticker(symbol)
        path = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{from_d}/{to_d}"
        r = requests.get(
            path,
            params={
                "adjusted": "true",
                "sort": "asc",
                "limit": "50000",
                "apiKey": self._api_key,
            },
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "text/csv"},
        )
        r.raise_for_status()
        # Polygon returns JSON by default; ask for CSV via the same header
        # path. If JSON came back, normalize.
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            data = r.json()
            results = data.get("results", []) or []
            rows = [
                {
                    "open": float(b["o"]), "high": float(b["h"]),
                    "low": float(b["l"]), "close": float(b["c"]),
                    "volume": float(b["v"]),
                    "_ts": datetime.fromtimestamp(int(b["t"]) / 1000, tz=timezone.utc),
                }
                for b in results
            ]
        else:
            parsed = _parse_csv_to_rows(r.text, symbol)
            rows = [
                {
                    "open": p["open"], "high": p["high"], "low": p["low"],
                    "close": p["close"], "volume": p["volume"],
                    "_ts": datetime.combine(p["date"], datetime.min.time(), tzinfo=timezone.utc),
                }
                for p in parsed
            ]

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(
            [{k: v for k, v in r.items() if k != "_ts"} for r in rows],
            index=pd.DatetimeIndex([r["_ts"] for r in rows], name="timestamp"),
        )
        return df


# ---- CSV-fed importer (no API key required) ----------------------------


def import_csv_into_bar_store(
    csv_text: str,
    *,
    symbol: str,
    bar_store: BarStore,
) -> int:
    """Insert Polygon-shaped CSV bars into `bar_store`. Returns row count."""
    rows = _parse_csv_to_rows(csv_text, symbol)
    if not rows:
        return 0
    cached_at = datetime.utcnow()
    inserted = 0
    with Session(bar_store._engine) as s:
        for r in rows:
            existing = s.get(_BarRow, {"symbol": r["symbol"], "date": r["date"]})
            if existing is None:
                s.add(_BarRow(
                    symbol=r["symbol"], date=r["date"],
                    open=r["open"], high=r["high"], low=r["low"],
                    close=r["close"], volume=r["volume"],
                    cached_at=cached_at,
                ))
                inserted += 1
            else:
                existing.open = r["open"]
                existing.high = r["high"]
                existing.low = r["low"]
                existing.close = r["close"]
                existing.volume = r["volume"]
                existing.cached_at = cached_at
        s.commit()
    return inserted
