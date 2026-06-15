"""Live price fetcher — real-time quotes from yfinance (free, no API key).

Returns current/last prices for NSE stocks, ETFs, and crypto INR pairs.
Uses yfinance's ``fast_info.last_price`` which is near-real-time during
market hours (09:15–15:30 IST) and last close outside hours.

Thread-safe, no state. Each call hits Yahoo's servers — caller should
cache if polling frequently (dashboard does 30s refresh).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveQuote:
    symbol: str               # internal symbol (NIFTYBEES, BTC/INR)
    price: float              # last traded price in INR
    prev_close: float         # previous day close
    change: float             # price - prev_close
    change_pct: float         # (price / prev_close - 1) * 100
    market_state: str         # "REGULAR" | "PRE" | "POST" | "CLOSED"
    fetched_at: str           # ISO timestamp


def _to_yf(sym: str) -> str:
    """Internal symbol -> yfinance ticker."""
    from trading_bot.ingest.yfinance_adapter import _to_yf_symbol
    return _to_yf_symbol(sym)


def _from_yf(yf_sym: str) -> str:
    """yfinance ticker -> internal symbol."""
    from trading_bot.ingest.yfinance_adapter import _from_yf_symbol
    return _from_yf_symbol(yf_sym)


def fetch_live_prices(symbols: Sequence[str]) -> dict[str, LiveQuote]:
    """Fetch live prices for a list of internal symbols.

    Returns ``{symbol: LiveQuote}`` for symbols that succeeded.
    Silently skips symbols that fail (yfinance is best-effort).
    """
    if not symbols:
        return {}

    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed")
        return {}

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    out: dict[str, LiveQuote] = {}

    for sym in symbols:
        try:
            yf_sym = _to_yf(sym)
            tk = yf.Ticker(yf_sym)

            # fast_info for price (fast, no full API call)
            fi = tk.fast_info
            price = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)

            if price is None or price <= 0:
                continue

            prev = prev or price  # fallback
            change = price - prev
            change_pct = (price / prev - 1.0) * 100.0 if prev > 0 else 0.0

            # Market state — fast_info doesn't have it; use tk.info
            # which does a full API call. Cache-friendly since yfinance
            # caches internally per session.
            try:
                full_info = tk.info
                market_state = (
                    full_info.get("marketState", "CLOSED") or "CLOSED"
                ).upper()
            except Exception:
                market_state = "CLOSED"

            out[sym] = LiveQuote(
                symbol=sym,
                price=round(price, 2),
                prev_close=round(prev, 2),
                change=round(change, 2),
                change_pct=round(change_pct, 2),
                market_state=market_state,
                fetched_at=now,
            )
        except Exception:
            log.debug("live price fetch failed for %s", sym, exc_info=True)
            continue

    return out


__all__ = ["LiveQuote", "fetch_live_prices"]
