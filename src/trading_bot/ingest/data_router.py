"""Data router — picks the right source per (asset_class, kind, urgency).

Caller intent → source matrix:

  | Kind          | Stocks       | Crypto         | Options       |
  |---------------|--------------|----------------|---------------|
  | live quote    | Alpaca       | Alpaca         | Alpaca (L3)   |
  | daily bars    | yfinance     | Alpaca crypto  | n/a           |
  | option chain  | n/a          | n/a            | yfinance      |

Rationale (see ingest/yfinance_adapter.py header):
  * Alpaca free is fine for live execution + crypto bars but blocks
    SIP within 15 minutes and has no free options chain.
  * yfinance gives unlimited adjusted daily bars + current option
    chains, no auth, no quota.
  * Coinbase WebSocket (free, no key) can feed real-time crypto ticks
    for low-latency strategies — not used by the v4 daemon yet.

The router exposes one entry point per kind. Callers (loaders,
strategy runners) go through these — never import yfinance / Alpaca
SDK directly.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Sequence

from trading_bot.research.historical_bars import DailyBar

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Daily bars router
# ---------------------------------------------------------------------------

def fetch_daily_bars(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
    asset_class: str = "us_equity",
) -> list[DailyBar]:
    """Route to the right historical-bars source.

    ``asset_class`` ∈ {"us_equity", "crypto"}. Equity goes to yfinance
    (free, adjusted, no SIP gate). Crypto goes to Alpaca (free, includes
    BTC/USD, ETH/USD).
    """
    if not symbols:
        return []
    if asset_class == "crypto":
        return _fetch_crypto_bars_alpaca(symbols=symbols, start=start, end=end)
    return _fetch_stock_bars_yfinance(symbols=symbols, start=start, end=end)


def _fetch_stock_bars_yfinance(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
) -> list[DailyBar]:
    from trading_bot.ingest.yfinance_adapter import fetch_daily_bars as _yf
    return _yf(symbols=symbols, start=start, end=end)


def _fetch_crypto_bars_alpaca(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
) -> list[DailyBar]:
    """Alpaca CryptoHistoricalDataClient. No auth required for crypto."""
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    except ImportError:
        log.error("alpaca-py not installed")
        return []

    cli = CryptoHistoricalDataClient()
    try:
        req = CryptoBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc),
            end=dt.datetime.combine(end, dt.time.max, tzinfo=dt.timezone.utc),
        )
        bars = cli.get_crypto_bars(req)
    except Exception as e:  # noqa: BLE001
        log.warning("alpaca crypto bars failed: %s", e)
        return []

    out: list[DailyBar] = []
    for sym in symbols:
        series = bars.data.get(sym, [])
        for b in series:
            ts = getattr(b, "timestamp", None)
            if ts is None:
                continue
            bar_date = ts.date() if hasattr(ts, "date") else dt.date.today()
            out.append(DailyBar(
                symbol=sym, bar_date=bar_date,
                open=float(b.open or 0), high=float(b.high or 0),
                low=float(b.low or 0), close=float(b.close or 0),
                volume=float(b.volume or 0),
                vwap=float(getattr(b, "vwap", 0) or 0) or None,
                source="alpaca:crypto:1d",
            ))
    return out


# ---------------------------------------------------------------------------
# Option chain router
# ---------------------------------------------------------------------------

def fetch_option_chain(underlying: str, expiry: dt.date):
    """yfinance is currently the only free option-chain source."""
    from trading_bot.ingest.yfinance_adapter import fetch_option_chain as _yf
    return _yf(underlying, expiry)


def list_option_expirations(underlying: str) -> list[dt.date]:
    from trading_bot.ingest.yfinance_adapter import list_expirations
    return list_expirations(underlying)


__all__ = [
    "fetch_daily_bars", "fetch_option_chain", "list_option_expirations",
]
