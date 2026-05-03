"""CryptoPanic collector — 100+ crypto news aggregator + community-vote sentiment.

Free-tier API (200 req/day). One call per ingest tick. Empty key →
silent skip (matches the existing pattern). Sentiment is computed from
``votes.positive`` / ``votes.negative`` (normalised to [-1, +1]); the
``votes.important`` count folds into ``raw_score`` for transparency.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from trading_bot.pipelines.crypto.sources._base import (
    CRYPTO_RSS_TIMEOUT,
    CRYPTO_USER_AGENT,
    SourceResult,
    normalize_crypto_symbol,
    parse_rfc822_or_iso,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

CRYPTOPANIC_API_URL = "https://cryptopanic.com/api/v1/posts/"


def collect_cryptopanic(
    engine: Any,
    *,
    settings: Any,
    fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    now=None,
) -> SourceResult:
    """Pull recent CryptoPanic posts; write one event per (post × tagged currency)."""
    now = now or utcnow()
    api_key = (getattr(settings, "cryptopanic_api_key", "") or "").strip()
    if not api_key:
        return SourceResult(source="cryptopanic", extra={"note": "no api key"})

    try:
        body = (fetcher or _default_fetcher)(api_key) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("cryptopanic fetch failed: %s", e)
        return SourceResult(source="cryptopanic", error=str(e))

    written = 0
    skipped = 0
    for post in body.get("results") or []:
        try:
            title = (post.get("title") or "").strip()
            url = (post.get("url") or "").strip()
            published = parse_rfc822_or_iso(post.get("published_at") or "")

            votes = post.get("votes") or {}
            pos = int(votes.get("positive") or 0)
            neg = int(votes.get("negative") or 0)
            important = int(votes.get("important") or 0)
            total_dir = pos + neg
            sentiment = (pos - neg) / total_dir if total_dir > 0 else 0.0
            sentiment = max(-1.0, min(1.0, sentiment))

            currencies = post.get("currencies") or []
            if not currencies:
                skipped += 1
                continue

            for cur in currencies:
                sym = (cur.get("code") or "").upper().strip()
                if not sym:
                    continue
                ok = write_event(
                    engine,
                    symbol=normalize_crypto_symbol(sym),
                    source="cryptopanic",
                    headline=title[:240],
                    url=url,
                    sentiment=sentiment,
                    raw_score=float(important) if important else None,
                    event_at=published,
                    now=now,
                )
                if ok:
                    written += 1
                else:
                    skipped += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("cryptopanic: skipped malformed post: %s", e)
            skipped += 1

    return SourceResult(source="cryptopanic", written=written, skipped=skipped)


def _default_fetcher(api_key: str) -> Dict[str, Any]:
    import requests

    resp = requests.get(
        CRYPTOPANIC_API_URL,
        params={"auth_token": api_key, "kind": "news", "public": "true"},
        timeout=CRYPTO_RSS_TIMEOUT,
        headers={"User-Agent": CRYPTO_USER_AGENT},
    )
    resp.raise_for_status()
    return resp.json() or {}
