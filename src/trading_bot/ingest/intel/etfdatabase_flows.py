"""etf.com / ETFDatabase flow intel feed (Phase A scaffold).

Daily ETF flow data (net AUM in/out) sourced from etf.com or
ETFDatabase. Cache-backed for Phase A; production scraper lands later.
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
    return Path.home() / ".cache" / "trading_bot" / "intel" / "etfdatabase_flows.json"


class EtfDatabaseFlowsFeed(BaseIntelFeed):
    feed_id = "etfdatabase_flows"

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
            raise IntelUnavailable("etfdatabase flows cache empty")
        out: dict[str, IntelRecord] = {}
        for sym, payload in (cache.get("series") or {}).items():
            out[sym] = IntelRecord(
                feed_id=self.feed_id, series_id=sym,
                value=float(payload.get("flow_30d_usd", 0.0)),
                unit="usd",
                source_ts=cache.get("published_iso", ""),
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        s = (cache.get("series") or {}).get(symbol)
        if not s:
            return {"etfdatabase_flow_30d": None}
        return {"etfdatabase_flow_30d": float(s.get("flow_30d_usd", 0.0))}


__all__ = ["EtfDatabaseFlowsFeed"]
