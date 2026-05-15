"""FINRA short-interest intel feed (Phase A scaffold).

FINRA publishes bi-monthly short-interest reports per symbol. The free
data is downloadable at https://www.finra.org/finra-data/short-sale-volume
but the scrape + parse layer is outside the daemon hot path.

This module ships the IntelFeed contract + a cache-backed lookup so
strategies can opt in via ``policy/strategy_signal_features_v1.json``.
When the cache is empty the feature value returns ``None`` and the
strategy is expected to handle missing-intel gracefully.
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
    return Path.home() / ".cache" / "trading_bot" / "intel" / "finra_short_interest.json"


class FinraShortInterestFeed(BaseIntelFeed):
    feed_id = "finra_short_interest"

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or _default_cache_path()

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except json.JSONDecodeError:
            log.warning("finra cache corrupt at %s; ignoring", self.cache_path)
            return {}

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        cache = self._load_cache()
        published_iso = cache.get("published_iso")
        if not published_iso:
            raise IntelUnavailable("finra short-interest cache empty")
        out: dict[str, IntelRecord] = {}
        for sym, payload in (cache.get("series") or {}).items():
            out[sym] = IntelRecord(
                feed_id=self.feed_id, series_id=sym,
                value=float(payload.get("short_interest_pct", 0.0)),
                unit="percent",
                source_ts=published_iso,
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                source_url=cache.get("source_url"),
            )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load_cache()
        series = (cache.get("series") or {}).get(symbol)
        if not series:
            return {"finra_short_interest_pct": None}
        return {
            "finra_short_interest_pct": float(
                series.get("short_interest_pct", 0.0)
            ),
        }


__all__ = ["FinraShortInterestFeed"]
