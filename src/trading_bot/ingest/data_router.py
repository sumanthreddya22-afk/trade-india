"""Data router — picks the right source per (asset_class, kind, urgency).

Caller intent -> source matrix (India-first):

  | Kind          | NSE/BSE equity | Crypto INR     | Options (NFO)  |
  |---------------|----------------|----------------|----------------|
  | live quote    | Zerodha Kite   | CoinDCX        | Zerodha Kite   |
  | daily bars    | yfinance (.NS) | yfinance (-INR)| n/a            |
  | option chain  | n/a            | n/a            | yfinance (.NS) |

Legacy US sources (Alpaca) are still reachable via ``us_equity`` /
``crypto`` asset classes for backward compatibility.

Rationale:
  * yfinance gives free adjusted daily bars for NSE (.NS suffix) and
    crypto INR pairs (BTC-INR, ETH-INR). No auth, no quota.
  * Zerodha Kite Connect provides live quotes + order submission
    (but costs Rs 2,000/month and needs daily token refresh).
  * For paper trading, yfinance is sufficient -- no broker needed.

The router exposes one entry point per kind. Callers (loaders,
strategy runners) go through these -- never import yfinance / broker
SDK directly.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Sequence

from trading_bot.research.historical_bars import DailyBar

log = logging.getLogger(__name__)

# Asset class aliases -- callers can use either form.
_NSE_CLASSES = {"nse_equity", "us_equity", "stock", "equity"}
_CRYPTO_CLASSES = {"crypto", "crypto_inr", "crypto_usd"}


# ---------------------------------------------------------------------------
# Daily bars router
# ---------------------------------------------------------------------------

def fetch_daily_bars(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
    asset_class: str = "nse_equity",
) -> list[DailyBar]:
    """Route to the right historical-bars source.

    ``asset_class`` guides the symbol mapping:
      * ``nse_equity`` / ``us_equity`` / ``stock`` / ``equity`` -> yfinance
        with ``.NS`` suffix for NSE symbols.
      * ``crypto`` / ``crypto_inr`` -> yfinance with ``-INR`` suffix.
      * ``crypto_usd`` -> yfinance with ``-USD`` suffix (legacy).
    """
    if not symbols:
        return []
    ac = asset_class.lower().strip()
    if ac in _CRYPTO_CLASSES:
        return _fetch_crypto_bars_yfinance(symbols=symbols, start=start, end=end)
    # Default: treat as NSE equity
    return _fetch_nse_bars_yfinance(symbols=symbols, start=start, end=end)


def _fetch_nse_bars_yfinance(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
) -> list[DailyBar]:
    """NSE / BSE equity + ETF bars via yfinance (.NS suffix)."""
    from trading_bot.ingest.yfinance_adapter import fetch_daily_bars as _yf
    return _yf(symbols=symbols, start=start, end=end, exchange="NSE")


def _fetch_crypto_bars_yfinance(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
) -> list[DailyBar]:
    """Crypto INR pairs via yfinance (BTC/INR -> BTC-INR)."""
    from trading_bot.ingest.yfinance_adapter import fetch_daily_bars as _yf
    return _yf(symbols=symbols, start=start, end=end, exchange="NSE")


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
