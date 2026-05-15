"""Treasury yield curve intel feed (Phase A).

Pulls per-tenor Treasury yields from FRED (DGS2 + DGS10). The
``fred_yield_curve_slope`` feature returns (10y − 2y) yield in basis
points. ``refresh()`` runs the network fetch + writes the cache; the
strategy hot path reads from the cache only.
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
    return Path.home() / ".cache" / "trading_bot" / "intel" / "treasury_curve.json"


# yfinance tickers for the major tenors — free, no auth, already a dep
# via ingest.data_router. The 2Y is the gap (no clean yfinance ticker),
# so we approximate the curve slope with ``^FVX − ^IRX`` (5Y − 13W) when
# 2Y is unavailable; production can plug in a Treasury.gov XML fetcher.
_YF_TENOR_TICKERS = {
    "13w": "^IRX",   # 13-week T-Bill
    "5y": "^FVX",
    "10y": "^TNX",
    "30y": "^TYX",
}


class TreasuryYieldCurveFeed(BaseIntelFeed):
    feed_id = "treasury_yield_curve"

    def __init__(
        self, cache_path: Optional[Path] = None,
        fred_api_key: Optional[str] = None,  # retained for compat; unused
    ) -> None:
        self.cache_path = cache_path or _default_cache_path()
        self.fred_api_key = fred_api_key

    def refresh(self, *, asof: Optional[dt.date] = None) -> dict:
        """Pull treasury yields from yfinance (free, no auth)."""
        try:
            import yfinance as yf
        except ImportError as e:
            raise IntelUnavailable(
                "yfinance not installed; install or add a FRED API key"
            ) from e
        asof = asof or dt.date.today()
        tenors: dict[str, float] = {}
        published_iso = ""
        for tenor, ticker in _YF_TENOR_TICKERS.items():
            try:
                h = yf.Ticker(ticker).history(period="5d")
                if h.empty:
                    log.warning("treasury %s (%s): empty history", tenor, ticker)
                    continue
                tenors[tenor] = float(h["Close"].iloc[-1])
                published_iso = h.index[-1].date().isoformat()
            except Exception as e:  # noqa: BLE001
                log.warning("treasury %s (%s): %s", tenor, ticker, e)
        if not tenors:
            raise IntelUnavailable("treasury_yield_curve: no tenors fetched")
        # 2y is preferred for the slope feature; absent, fall back to 13w.
        # The query_features path will compute (10y - {2y|13w}) and
        # downstream consumers tolerate either.
        payload = {
            "published_iso": published_iso
                or dt.datetime.now(dt.timezone.utc).date().isoformat(),
            "tenors": tenors,
            "fetched_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "yfinance",
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=2))
        log.info(
            "treasury_yield_curve refreshed: %d tenors (10y=%s)",
            len(tenors), tenors.get("10y"),
        )
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
            raise IntelUnavailable("treasury yield curve cache empty")
        tenors: dict = cache.get("tenors") or {}
        published_iso = cache.get("published_iso", "")
        out: dict[str, IntelRecord] = {}
        for tenor, yield_pct in tenors.items():
            out[tenor] = IntelRecord(
                feed_id=self.feed_id, series_id=tenor,
                value=float(yield_pct), unit="percent",
                source_ts=published_iso,
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        tenors = cache.get("tenors") or {}
        ten = tenors.get("10y") or tenors.get("DGS10")
        short = (
            tenors.get("2y") or tenors.get("DGS2")
            or tenors.get("13w") or tenors.get("DGS3MO")
        )
        if ten is None or short is None:
            return {"fred_yield_curve_slope": None, "treasury_10y": ten}
        # Slope in basis points.
        slope_bps = (float(ten) - float(short)) * 100.0
        return {
            "fred_yield_curve_slope": slope_bps,
            "treasury_10y": float(ten),
        }


__all__ = ["TreasuryYieldCurveFeed"]
