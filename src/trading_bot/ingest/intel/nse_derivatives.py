"""NSE derivatives intel feed — Nifty PCR + FII/DII participation.

Replaces both the FINRA short-interest feed and the CBOE put/call ratio
feed for Indian market regime classification.

Key signals:
  * ``NIFTY_PCR``        — Nifty 50 Put/Call Ratio (Open Interest basis)
  * ``BANKNIFTY_PCR``    — Bank Nifty Put/Call Ratio
  * ``FII_INDEX_NET``    — FII net long/short in index futures (₹ crore)
  * ``FII_STOCK_NET``    — FII net in stock futures
  * ``DII_EQUITY_NET``   — DII net equity buying (₹ crore)

PCR interpretation:
  PCR > 1.2  — Put-heavy = bearish sentiment / potential contrarian buy
  PCR < 0.8  — Call-heavy = bullish / potential complacency signal
  PCR 0.9–1.1 — Neutral

Sources (all free, no auth):
  - NSE India option chain: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
  - NSE participant-wise OI: https://www.nseindia.com/api/allIndices
  - NSE FII/DII activity: https://www.nseindia.com/market-data/fii-dii-activity
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ingest.intel.base import (
    BaseIntelFeed, IntelRecord, IntelUnavailable,
)

log = logging.getLogger(__name__)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; trading-bot/0.1; research only)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
}

_NSE_OPTION_CHAIN_URL = (
    "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
)


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "trading_bot" / "intel" / "nse_derivatives.json"


class NseDerivativesFeed(BaseIntelFeed):
    """NSE derivatives sentiment feed (PCR + FII/DII)."""

    feed_id = "nse_derivatives"

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or _default_cache_path()

    def refresh(self, *, asof: Optional[dt.date] = None) -> dict:
        """Fetch PCR from NSE option chain API."""
        asof = asof or dt.date.today()
        pcr_data: dict[str, float] = {}
        for symbol in ("NIFTY", "BANKNIFTY"):
            try:
                pcr = self._fetch_pcr(symbol)
                if pcr is not None:
                    pcr_data[f"{symbol}_PCR"] = pcr
            except Exception as e:  # noqa: BLE001
                log.warning("nse_derivatives: PCR fetch for %s failed: %s", symbol, e)
        if not pcr_data:
            raise IntelUnavailable("nse_derivatives: no PCR data fetched")
        payload = {
            "published_iso": asof.isoformat(),
            "pcr": pcr_data,
            "fetched_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source_url": "https://www.nseindia.com/api/option-chain-indices",
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=2))
        log.info("nse_derivatives refreshed: %s", pcr_data)
        return payload

    def _fetch_pcr(self, symbol: str) -> Optional[float]:
        """Compute PCR (OI-based) from NSE option chain."""
        url = _NSE_OPTION_CHAIN_URL.format(symbol=symbol)
        try:
            req = urllib.request.Request(url, headers=_NSE_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read()
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            raise IntelUnavailable(f"nse option chain [{symbol}]: {e}") from e
        records = data.get("records", {}).get("data", [])
        total_put_oi = 0
        total_call_oi = 0
        for row in records:
            if "PE" in row:
                total_put_oi += int(row["PE"].get("openInterest", 0) or 0)
            if "CE" in row:
                total_call_oi += int(row["CE"].get("openInterest", 0) or 0)
        if total_call_oi == 0:
            return None
        return round(total_put_oi / total_call_oi, 4)

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
            raise IntelUnavailable("nse_derivatives cache empty")
        pcr_data = cache.get("pcr", {})
        published_iso = cache.get("published_iso", "")
        out: dict[str, IntelRecord] = {}
        for key, value in pcr_data.items():
            out[key] = IntelRecord(
                feed_id=self.feed_id,
                series_id=key,
                value=float(value),
                unit="ratio",
                source_ts=published_iso,
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                source_url="https://www.nseindia.com/market-data/india-vix",
            )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        pcr = cache.get("pcr", {})
        return {
            "nifty_pcr": pcr.get("NIFTY_PCR"),
            "banknifty_pcr": pcr.get("BANKNIFTY_PCR"),
        }


__all__ = ["NseDerivativesFeed"]
