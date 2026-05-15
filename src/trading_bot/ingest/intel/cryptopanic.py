"""CryptoPanic intel feed — recent news-post counts per crypto asset.

CryptoPanic aggregates news + social posts across the crypto ecosystem
and offers a free public API:

    https://cryptopanic.com/api/v1/posts/?auth_token=<key>&currencies=BTC,ETH

The free tier permits ~50 requests/day per IP without auth, more with
an ``auth_token``. We pull ONCE per intel snapshot so the cadence stays
well below the limit.

Series shape: one series_id per configured currency, e.g.
``"BTC_news_24H"``. Value is the post count returned by the API for
the last fetched page. Snapshot-only — Plan v4 §6 reserves news
gating for later phases; today the value flows into
``feature_snapshot.intel_json`` so backtest replay sees the same
news pressure the live decision saw.

Failure modes (network, parse, rate-limit) raise ``IntelUnavailable``
per the intel-layer fail-closed contract.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Sequence

from trading_bot.ingest.intel.base import IntelRecord, IntelUnavailable

log = logging.getLogger(__name__)

DEFAULT_CURRENCIES: tuple[str, ...] = ("BTC", "ETH")
DEFAULT_FILTER: str = "hot"   # CryptoPanic filter: hot|rising|bullish|bearish|important
WINDOW_HOURS_DEFAULT: int = 24


@dataclass(frozen=True)
class CryptoPanicFeed:
    """CryptoPanic posts feed. One HTTP call per fetch (all currencies
    in a single query parameter)."""

    currencies: Sequence[str] = DEFAULT_CURRENCIES
    filter: str = DEFAULT_FILTER
    window_hours: int = WINDOW_HOURS_DEFAULT
    auth_token: str | None = None
    base_url: str = "https://cryptopanic.com/api/v1/posts/"
    timeout_seconds: float = 10.0
    feed_id: str = "cryptopanic_v1"

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        token = self.auth_token or os.environ.get(
            "CRYPTOPANIC_AUTH_TOKEN", "",
        ).strip() or None
        params = {
            "currencies": ",".join(c.upper() for c in self.currencies),
            "filter": self.filter,
            "public": "true",
        }
        if token:
            params["auth_token"] = token
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(
                url, timeout=self.timeout_seconds,
            ) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise IntelUnavailable(
                f"cryptopanic http {e.code}: {e.reason}"
            ) from e
        except Exception as e:  # noqa: BLE001
            raise IntelUnavailable(
                f"cryptopanic fetch failed: {type(e).__name__}: {e}"
            ) from e
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise IntelUnavailable(
                "cryptopanic non-json response"
            ) from e
        results = payload.get("results") or []
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            hours=int(self.window_hours),
        )
        counts: dict[str, int] = {c.upper(): 0 for c in self.currencies}
        latest: dict[str, str] = {}
        for post in results:
            published = post.get("published_at") or post.get("created_at")
            if not published:
                continue
            try:
                pub_ts = dt.datetime.fromisoformat(
                    published.replace("Z", "+00:00"),
                )
            except ValueError:
                continue
            if pub_ts < cutoff:
                continue
            for c in (post.get("currencies") or []):
                code = (c.get("code") or "").upper()
                if code in counts:
                    counts[code] += 1
                    prev = latest.get(code, "")
                    if not prev or published > prev:
                        latest[code] = published

        fetched_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        out: dict[str, IntelRecord] = {}
        for code, n in counts.items():
            sid = f"{code}_news_{int(self.window_hours)}H"
            out[sid] = IntelRecord(
                feed_id=self.feed_id,
                series_id=sid,
                value=float(n),
                unit="count",
                source_ts=latest.get(code) or fetched_ts,
                fetched_ts=fetched_ts,
                source_url="https://cryptopanic.com/",
            )
        return out


__all__ = [
    "CryptoPanicFeed", "DEFAULT_CURRENCIES", "DEFAULT_FILTER",
    "WINDOW_HOURS_DEFAULT",
]
