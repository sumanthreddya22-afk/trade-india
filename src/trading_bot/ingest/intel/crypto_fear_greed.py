"""Crypto Fear & Greed Index intel feed (Phase A).

Daily aggregate sentiment index 0..100 published at
``https://api.alternative.me/fng/`` (no auth). Used by the regime
classifier and by Crypto Momentum v3 as an optional intel feature.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ingest.intel.base import (
    BaseIntelFeed, IntelRecord, IntelUnavailable,
)

log = logging.getLogger(__name__)


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "trading_bot" / "intel" / "crypto_fear_greed.json"


_FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"


class CryptoFearGreedFeed(BaseIntelFeed):
    feed_id = "crypto_fear_greed"

    def __init__(
        self, cache_path: Optional[Path] = None, timeout_seconds: float = 5.0,
    ) -> None:
        self.cache_path = cache_path or _default_cache_path()
        self.timeout_seconds = timeout_seconds

    def refresh(self) -> dict:
        try:
            with urllib.request.urlopen(
                _FNG_URL, timeout=self.timeout_seconds,
            ) as r:
                body = r.read()
        except urllib.error.URLError as e:
            raise IntelUnavailable(
                f"crypto_fear_greed http failed: {e}"
            ) from e
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as e:
            raise IntelUnavailable(
                "crypto_fear_greed: non-JSON response"
            ) from e
        data = envelope.get("data") or []
        if not data:
            raise IntelUnavailable("crypto_fear_greed: empty data array")
        entry = data[0]
        value = int(entry.get("value", 0))
        classification = entry.get("value_classification", "")
        ts = entry.get("timestamp")
        published_iso = (
            dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).isoformat()
            if ts else dt.datetime.now(dt.timezone.utc).isoformat()
        )
        payload = {
            "value": value,
            "classification": classification,
            "published_iso": published_iso,
            "fetched_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "alternative.me",
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=2))
        log.info("crypto_fear_greed refreshed: %d (%s)", value, classification)
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
            raise IntelUnavailable("crypto fear-greed cache empty")
        value = cache.get("value")
        if value is None:
            raise IntelUnavailable("crypto fear-greed cache missing 'value'")
        return {
            "index": IntelRecord(
                feed_id=self.feed_id, series_id="index",
                value=float(value), unit="0-100",
                source_ts=cache.get("published_iso", ""),
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        }

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        v = cache.get("value")
        return {"crypto_fear_greed_index": float(v) if v is not None else None}


__all__ = ["CryptoFearGreedFeed"]
