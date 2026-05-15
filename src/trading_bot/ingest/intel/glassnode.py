"""Glassnode free-tier on-chain intel (Phase B).

Daily MVRV-Z, NUPL, exchange flows. Cached per-symbol. Real fetcher
ships later; the scaffold returns the cached payload through the
``IntelFeed`` contract.
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
    return Path.home() / ".cache" / "trading_bot" / "intel" / "glassnode.json"


class GlassnodeFeed(BaseIntelFeed):
    feed_id = "glassnode"

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
            raise IntelUnavailable("glassnode cache empty")
        out: dict[str, IntelRecord] = {}
        published_iso = cache.get("published_iso", "")
        for sym, metrics in (cache.get("symbols") or {}).items():
            for metric, value in metrics.items():
                key = f"{sym}.{metric}"
                out[key] = IntelRecord(
                    feed_id=self.feed_id, series_id=key,
                    value=float(value), unit="raw",
                    source_ts=published_iso,
                    fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        metrics = (cache.get("symbols") or {}).get(symbol) or {}
        return {
            "glassnode_mvrv_z": metrics.get("mvrv_z"),
            "glassnode_nupl": metrics.get("nupl"),
            "glassnode_exchange_inflow_24h": metrics.get("exchange_inflow_24h"),
        }


__all__ = ["GlassnodeFeed"]
