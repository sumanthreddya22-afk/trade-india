"""India VIX intel feed — replaces CBOE VIX for Indian market regime.

India VIX is NSE's volatility index, computed from Nifty 50 option
bid-ask quotes using the CBOE VIX methodology adapted for NSE weekly
expiry contracts.

A rising India VIX → elevated uncertainty → regime classifier moves
toward cautious/defensive mode.

Sources:
  - NSE India VIX: https://www.nseindia.com/market-data/india-vix
  - yfinance ticker: ^INDIAVIX (most reliable free source)
  - NSE direct: https://www.nseindia.com/api/allIndices

Range reference (historical):
  < 12   — Very low volatility (complacent market)
  12–18  — Normal range
  18–25  — Elevated (cautious mode)
  > 25   — High volatility / crisis (defensive mode trigger)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ingest.intel.base import (
    BaseIntelFeed, IntelRecord, IntelUnavailable,
)

log = logging.getLogger(__name__)


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "trading_bot" / "intel" / "india_vix.json"


class IndiaVixFeed(BaseIntelFeed):
    """India VIX feed using yfinance (^INDIAVIX ticker).

    Refresh runs once daily, before market open. The strategy hot path
    reads from the cache only (no network on the hot path).
    """

    feed_id = "india_vix"

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or _default_cache_path()

    def refresh(self, *, asof: Optional[dt.date] = None) -> dict:
        """Pull India VIX from yfinance."""
        try:
            import yfinance as yf
        except ImportError as e:
            raise IntelUnavailable("yfinance not installed") from e
        asof = asof or dt.date.today()
        try:
            ticker = yf.Ticker("^INDIAVIX")
            hist = ticker.history(period="5d")
            if hist.empty:
                raise IntelUnavailable("India VIX: empty history from yfinance")
            vix_value = float(hist["Close"].iloc[-1])
            published_iso = hist.index[-1].date().isoformat()
        except IntelUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise IntelUnavailable(f"India VIX fetch failed: {e}") from e
        payload = {
            "vix": vix_value,
            "published_iso": published_iso,
            "fetched_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "yfinance:^INDIAVIX",
            "source_url": "https://www.nseindia.com/market-data/india-vix",
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=2))
        log.info("India VIX refreshed: %.2f (as of %s)", vix_value, published_iso)
        return payload

    def _load(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except json.JSONDecodeError:
            return {}

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        cache = self._load()
        if not cache:
            raise IntelUnavailable("India VIX cache empty — run refresh first")
        vix = cache.get("vix")
        if vix is None:
            raise IntelUnavailable("India VIX cache missing 'vix' field")
        published_iso = cache.get("published_iso", "")
        return {
            "INDIAVIX": IntelRecord(
                feed_id=self.feed_id,
                series_id="INDIAVIX",
                value=float(vix),
                unit="index",
                source_ts=published_iso,
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                source_url="https://www.nseindia.com/market-data/india-vix",
            )
        }

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        vix = cache.get("vix")
        return {"india_vix": float(vix) if vix is not None else None}


__all__ = ["IndiaVixFeed"]
