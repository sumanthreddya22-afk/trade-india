"""CBOE intel feed (Phase B).

Publishes daily put/call ratio + SKEW index. Both are broad-market
signals consumed by the options-class regime classifier. Free daily
CSVs at https://cdn.cboe.com/api/global/us_indices/daily_prices/.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
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
    return Path.home() / ".cache" / "trading_bot" / "intel" / "cboe.json"


_CBOE_SKEW_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv"
)
_CBOE_PUTCALL_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/"
    "TOTAL_PC_RATIO_History.csv"
)


def _fetch_csv(url: str, timeout: float) -> list[list[str]]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "trading-bot/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise IntelUnavailable(f"cboe http {url}: {e}") from e
    rows = list(csv.reader(io.StringIO(body)))
    if not rows:
        raise IntelUnavailable(f"cboe csv empty: {url}")
    return rows


def _last_numeric_row(rows: list[list[str]]) -> tuple[str, float]:
    """Walk from the bottom, find the first row whose 2nd column parses
    as float. CBOE CSVs sometimes have header text + a few blank lines."""
    for row in reversed(rows):
        if len(row) < 2:
            continue
        try:
            return row[0], float(row[1])
        except ValueError:
            continue
    raise IntelUnavailable("cboe csv: no numeric row found")


class CboeFeed(BaseIntelFeed):
    feed_id = "cboe"

    def __init__(
        self, cache_path: Optional[Path] = None, timeout_seconds: float = 8.0,
    ) -> None:
        self.cache_path = cache_path or _default_cache_path()
        self.timeout_seconds = timeout_seconds

    def refresh(self) -> dict:
        payload: dict = {
            "fetched_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "cboe.com",
        }
        # SKEW
        try:
            skew_rows = _fetch_csv(_CBOE_SKEW_URL, self.timeout_seconds)
            skew_date, skew_value = _last_numeric_row(skew_rows)
            payload["skew"] = skew_value
            payload["skew_published_iso"] = skew_date
        except IntelUnavailable as e:
            log.warning("cboe skew: %s", e)

        # Put/Call total
        try:
            pc_rows = _fetch_csv(_CBOE_PUTCALL_URL, self.timeout_seconds)
            pc_date, pc_value = _last_numeric_row(pc_rows)
            payload["put_call_ratio"] = pc_value
            payload["put_call_published_iso"] = pc_date
        except IntelUnavailable as e:
            log.warning("cboe put_call: %s", e)

        if "skew" not in payload and "put_call_ratio" not in payload:
            raise IntelUnavailable("cboe: both series failed")

        payload["published_iso"] = (
            payload.get("skew_published_iso")
            or payload.get("put_call_published_iso")
            or payload["fetched_ts"]
        )
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=2))
        log.info(
            "cboe refreshed: skew=%s put_call=%s",
            payload.get("skew"), payload.get("put_call_ratio"),
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
            raise IntelUnavailable("cboe cache empty")
        out: dict[str, IntelRecord] = {}
        published_iso = cache.get("published_iso", "")
        for key in ("put_call_ratio", "skew", "vix_term_slope"):
            v = cache.get(key)
            if v is None:
                continue
            out[key] = IntelRecord(
                feed_id=self.feed_id, series_id=key,
                value=float(v), unit="raw",
                source_ts=published_iso,
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        if not out:
            raise IntelUnavailable("cboe cache has no series")
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        return {
            "cboe_putcall_ratio": cache.get("put_call_ratio"),
            "cboe_skew": cache.get("skew"),
        }


__all__ = ["CboeFeed"]
