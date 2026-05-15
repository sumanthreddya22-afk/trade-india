"""FRED (St. Louis Fed) intel feed.

Free, JSON-over-HTTPS, no auth for most series via the public ``fred``
API. We use the same endpoint shape but stay polite — one request per
series per decision, 5-second timeout, no retries on the hot path
(the operator can re-run; the daemon halts the dependent strategy if
the feed is unavailable rather than silently using stale values).

Default series cover the regime indicators the seed thesis cares about:
  * ``VIXCLS`` — CBOE Volatility Index (close)
  * ``DGS10`` — 10-year Treasury constant maturity rate
  * ``DGS2``  — 2-year Treasury constant maturity rate
  * ``DFF``   — Effective Federal Funds Rate

Optional ``api_key`` may be passed to authenticate; without one, FRED
serves with stricter rate limits but still works for low-volume use.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Sequence

from trading_bot.ingest.intel.base import IntelRecord, IntelUnavailable

log = logging.getLogger(__name__)

DEFAULT_SERIES: tuple[str, ...] = ("VIXCLS", "DGS10", "DGS2", "DFF")

UNITS = {
    "VIXCLS": "index",
    "DGS10": "pct",
    "DGS2": "pct",
    "DFF": "pct",
}


@dataclass(frozen=True)
class FredFeed:
    """FRED API feed. Stateless; one HTTP call per series."""

    series: Sequence[str] = DEFAULT_SERIES
    api_key: str | None = None
    base_url: str = "https://api.stlouisfed.org/fred/series/observations"
    timeout_seconds: float = 5.0
    feed_id: str = "fred_v1"

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        out: dict[str, IntelRecord] = {}
        fetched_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        for sid in self.series:
            try:
                value, observation_date = self._fetch_latest(
                    sid, on_or_before=decision_date,
                )
            except IntelUnavailable:
                raise
            except Exception as e:  # noqa: BLE001
                raise IntelUnavailable(
                    f"fred[{sid}] fetch failed: {type(e).__name__}: {e}"
                ) from e
            out[sid] = IntelRecord(
                feed_id=self.feed_id,
                series_id=sid,
                value=value,
                unit=UNITS.get(sid, "unknown"),
                source_ts=observation_date,
                fetched_ts=fetched_ts,
                source_url=f"https://fred.stlouisfed.org/series/{sid}",
            )
        return out

    def _fetch_latest(
        self, series_id: str, *, on_or_before: dt.date,
    ) -> tuple[float, str]:
        """Return (value, observation_date_iso) for the most recent
        observation at or before ``on_or_before``. Raises
        ``IntelUnavailable`` on network / parse / empty errors."""
        if self.api_key is None:
            # FRED's free no-key endpoint exists at /series/observations
            # with file_type=json but is rate-limited. The api_key is
            # the recommended path. Without it, expect 429s.
            params = {
                "series_id": series_id,
                "file_type": "json",
                "observation_end": on_or_before.isoformat(),
                "sort_order": "desc",
                "limit": "1",
            }
        else:
            params = {
                "series_id": series_id,
                "file_type": "json",
                "api_key": self.api_key,
                "observation_end": on_or_before.isoformat(),
                "sort_order": "desc",
                "limit": "1",
            }
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_seconds) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise IntelUnavailable(
                f"fred[{series_id}] http {e.code}: {e.reason}"
            ) from e
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise IntelUnavailable(
                f"fred[{series_id}] non-json response"
            ) from e
        obs = payload.get("observations") or []
        if not obs:
            raise IntelUnavailable(
                f"fred[{series_id}] no observations on or before {on_or_before}"
            )
        latest = obs[0]
        raw_value = latest.get("value")
        if raw_value in (None, ".", ""):
            raise IntelUnavailable(
                f"fred[{series_id}] sentinel value '{raw_value}'"
            )
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as e:
            raise IntelUnavailable(
                f"fred[{series_id}] non-numeric value '{raw_value}'"
            ) from e
        observation_date = latest.get("date", on_or_before.isoformat())
        return value, observation_date


__all__ = ["DEFAULT_SERIES", "FredFeed", "UNITS"]
