"""Perpetual-futures funding rates intel (Phase B).

Cached per-symbol per-exchange. Strategy reads ``funding_rate_8h_zscore``.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ingest.intel.base import (
    BaseIntelFeed, IntelRecord, IntelUnavailable,
)


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "trading_bot" / "intel" / "funding_rates.json"


class FundingRatesFeed(BaseIntelFeed):
    feed_id = "funding_rates"

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or _default_cache_path()

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
            raise IntelUnavailable("funding rates cache empty")
        out: dict[str, IntelRecord] = {}
        for sym, payload in (cache.get("symbols") or {}).items():
            out[sym] = IntelRecord(
                feed_id=self.feed_id, series_id=sym,
                value=float(payload.get("funding_rate_8h", 0.0)),
                unit="fraction",
                source_ts=cache.get("published_iso", ""),
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        s = (cache.get("symbols") or {}).get(symbol) or {}
        return {
            "funding_rate_8h_zscore": s.get("funding_rate_8h_zscore"),
            "coinglass_oi_delta_24h": s.get("oi_delta_24h"),
        }


__all__ = ["FundingRatesFeed"]
