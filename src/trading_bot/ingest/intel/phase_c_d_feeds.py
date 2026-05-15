"""Phase C + D intel feed scaffolds.

Bundles the long-tail of feeds together to keep the file count
manageable. Each class follows the same cache-backed contract: real
fetchers ship in a follow-up phase but the IntelFeed surface is
already wired so strategies + the regime classifier + the research
bot can opt in via policy.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ingest.intel.base import (
    BaseIntelFeed, IntelRecord, IntelUnavailable,
)


def _cache_dir() -> Path:
    return Path.home() / ".cache" / "trading_bot" / "intel"


class _CachedFeedBase(BaseIntelFeed):
    """Reusable cache-backed feed. Subclasses set ``feed_id`` and
    ``feature_keys`` (mapping of feature_id -> JSON dotted path)."""

    feature_keys: dict[str, str] = {}

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or (
            _cache_dir() / f"{self.feed_id}.json"
        )

    def _load(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except json.JSONDecodeError:
            return {}

    def _resolve(self, payload: dict, dotted: str) -> Any:
        cur: Any = payload
        for part in dotted.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        cache = self._load()
        if not cache:
            raise IntelUnavailable(f"{self.feed_id} cache empty")
        out: dict[str, IntelRecord] = {}
        published_iso = cache.get("published_iso", "")
        for feature_id, dotted in self.feature_keys.items():
            v = self._resolve(cache, dotted)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            out[feature_id] = IntelRecord(
                feed_id=self.feed_id, series_id=feature_id,
                value=fv, unit="raw",
                source_ts=published_iso,
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        return out

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        symbol_payload = (cache.get("symbols") or {}).get(symbol, {})
        out: dict[str, Any] = {}
        for feature_id, dotted in self.feature_keys.items():
            # Try per-symbol first, then global cache.
            v = self._resolve(symbol_payload, dotted)
            if v is None:
                v = self._resolve(cache, dotted)
            out[feature_id] = v
        return out


# ---- Phase C: stocks + sentiment intel ------------------------------------

class SecForm4Feed(_CachedFeedBase):
    feed_id = "sec_form_4"
    feature_keys = {"form_4_insider_cluster_30d": "insider_cluster_30d"}


class SecForm13FFeed(_CachedFeedBase):
    feed_id = "sec_form_13f"
    feature_keys = {"form_13f_buyer_count_q": "buyer_count_q"}


class FinnhubEarningsFeed(_CachedFeedBase):
    feed_id = "finnhub_earnings_calendar"
    feature_keys = {"days_until_earnings": "days_until_earnings"}


class EstimizeFeed(_CachedFeedBase):
    feed_id = "estimize_free"
    feature_keys = {"estimize_surprise_pct": "surprise_pct"}


class StockTwitsFeed(_CachedFeedBase):
    feed_id = "stocktwits"
    feature_keys = {
        "stocktwits_sentiment_24h": "sentiment_24h",
        "stocktwits_message_count_24h": "message_count_24h",
    }


class GoogleTrendsFeed(_CachedFeedBase):
    feed_id = "google_trends"
    feature_keys = {"google_trends_attention_score": "attention_score"}


class RedditCryptoFeed(_CachedFeedBase):
    feed_id = "reddit_crypto"
    feature_keys = {"reddit_crypto_sentiment_24h": "sentiment_24h"}


# ---- Phase D: long tail + macro --------------------------------------------

class WhaleAlertFeed(_CachedFeedBase):
    feed_id = "whale_alert"
    feature_keys = {"whale_alert_exchange_flow_24h": "exchange_flow_24h"}


class LunarCrushFeed(_CachedFeedBase):
    feed_id = "lunarcrush"
    feature_keys = {"lunarcrush_galaxy_score": "galaxy_score"}


class CftcCotFeed(_CachedFeedBase):
    feed_id = "cftc_cot"
    feature_keys = {"cftc_cot_smart_money_net": "smart_money_net"}


class ForexFactoryFeed(_CachedFeedBase):
    feed_id = "forex_factory"
    feature_keys = {"forex_factory_high_impact_count_24h": "high_impact_count_24h"}


class RoicAITranscriptsFeed(_CachedFeedBase):
    feed_id = "roic_ai_transcripts"
    feature_keys = {"transcript_tone_score": "transcript_tone_score"}


class SeekingAlphaFreeFeed(_CachedFeedBase):
    feed_id = "seeking_alpha_free"
    feature_keys = {"seeking_alpha_bull_bear_score": "bull_bear_score"}


class TipRanksFeed(_CachedFeedBase):
    feed_id = "tipranks_rss"
    feature_keys = {"tipranks_consensus_rating": "consensus_rating"}


__all__ = [
    "CftcCotFeed",
    "EstimizeFeed",
    "FinnhubEarningsFeed",
    "ForexFactoryFeed",
    "GoogleTrendsFeed",
    "LunarCrushFeed",
    "RedditCryptoFeed",
    "RoicAITranscriptsFeed",
    "SecForm4Feed",
    "SecForm13FFeed",
    "SeekingAlphaFreeFeed",
    "StockTwitsFeed",
    "TipRanksFeed",
    "WhaleAlertFeed",
]
