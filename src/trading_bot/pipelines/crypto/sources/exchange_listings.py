"""Exchange listings collector — Coinbase + Binance announcements.

Per the plan, listings are crypto's strongest free deterministic
catalyst (the "Coinbase effect"). Highest weight (5.0).

Two sub-fetchers (one per exchange) are wired through one collector
so the orchestration layer treats this as a single source.

Sentiment heuristic:
    new_listing  → +0.7
    delisting    → -0.7
    suspension   → -0.5
    other        →  0.0
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    stable_event_hash,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

COINBASE_BLOG_RSS_URL = "https://blog.coinbase.com/feed"
BINANCE_ANNOUNCEMENTS_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
)


# Title patterns. Compiled as a tuple so we can extend per-exchange easily.
_NEW_LISTING_PATTERNS = (
    re.compile(r"\b([A-Z]{2,8})\b\s+(?:is\s+)?(?:launching|now\s+(?:available|trading)|listed)", re.I),
    re.compile(r"\bbinance\s+will\s+list\s+([A-Z]{2,8})\b", re.I),
    re.compile(r"\b([A-Z]{2,8})\s+\([^)]+\)\s+(?:is\s+)?(?:launching|now)", re.I),
    re.compile(r"\blisting\s+of\s+([A-Z]{2,8})\b", re.I),
)
_DELIST_PATTERNS = (
    re.compile(r"\b(?:delist(?:ing)?|removal\s+of)\s+([A-Z]{2,8})\b", re.I),
    re.compile(r"\b([A-Z]{2,8})\s+will\s+be\s+delisted\b", re.I),
)
_SUSPEND_PATTERNS = (
    re.compile(r"\b(?:suspending|suspension\s+of|halt\s+of)\s+([A-Z]{2,8})\b", re.I),
    re.compile(r"\btrading\s+(?:of|for)\s+([A-Z]{2,8})\s+(?:is\s+)?(?:suspended|halted)\b", re.I),
)


@dataclass
class _ParsedItem:
    symbol: str
    sentiment: float
    headline: str
    url: str
    item_id: str
    event_at: Optional[dt.datetime]


def _classify_title(title: str) -> tuple[str | None, float]:
    """Return (extracted_symbol, sentiment_or_0.0). None when no match."""
    for pat in _DELIST_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(1).upper(), -0.7
    for pat in _SUSPEND_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(1).upper(), -0.5
    for pat in _NEW_LISTING_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(1).upper(), 0.7
    return None, 0.0


def _parse_announcement_items(
    items: Iterable[Dict[str, Any]],
    *,
    exchange: str,
) -> List[_ParsedItem]:
    """Normalise a list of raw announcement dicts into ParsedItem rows.

    Each input item is expected to expose ``title``, ``url`` (or ``link``),
    ``id`` (or ``code``/``guid``), and an optional timestamp field. We
    only return items whose title matches a known listing/delist/suspend
    pattern — everything else is irrelevant (regular blog posts, market
    commentary, etc.) and gets dropped silently.
    """
    parsed: List[_ParsedItem] = []
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        symbol_raw, sentiment = _classify_title(title)
        if not symbol_raw:
            continue
        symbol = normalize_crypto_symbol(symbol_raw)
        url = item.get("url") or item.get("link") or ""
        item_id = str(item.get("id") or item.get("code") or item.get("guid") or url or title)
        ts_raw = item.get("event_at") or item.get("timestamp") or item.get("published")
        event_at = _parse_timestamp(ts_raw)
        parsed.append(_ParsedItem(
            symbol=symbol,
            sentiment=sentiment,
            headline=f"[{exchange}] {title}"[:1000],
            url=url,
            item_id=f"{exchange}:{item_id}",
            event_at=event_at,
        ))
    return parsed


def _parse_timestamp(raw: Any) -> Optional[dt.datetime]:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.timezone.utc)
    try:
        # Unix seconds (int) or millis (large int)
        v = int(raw)
        if v > 10_000_000_000:
            v //= 1000
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        s = str(raw).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def collect_exchange_listings(
    engine: Any,
    *,
    settings: Any = None,                  # not used; kept for signature parity
    coinbase_fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    binance_fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Pull announcement feeds; write one event per material listing/delist."""
    now = now or utcnow()
    written = 0
    skipped = 0
    errors: List[str] = []

    fetchers = (
        ("coinbase", coinbase_fetcher or _default_coinbase_fetcher),
        ("binance",  binance_fetcher  or _default_binance_fetcher),
    )

    for exchange, fetcher in fetchers:
        try:
            raw_items = list(fetcher() or [])
        except Exception as e:  # noqa: BLE001 — per-exchange fail-soft
            logger.warning("exchange_listings %s fetch failed: %s", exchange, e)
            errors.append(f"{exchange}:{e}")
            continue

        for parsed in _parse_announcement_items(raw_items, exchange=exchange):
            ok = write_event(
                engine,
                symbol=parsed.symbol,
                source="exchange_listing",
                headline=parsed.headline,
                url=parsed.url,
                sentiment=parsed.sentiment,
                event_at=parsed.event_at,
                event_hash=stable_event_hash("exchange_listing", parsed.item_id),
                now=now,
            )
            if ok:
                written += 1
            else:
                skipped += 1

    return SourceResult(
        source="exchange_listing",
        written=written,
        skipped=skipped,
        error="; ".join(errors) if errors else None,
        extra={"exchanges_polled": [name for name, _ in fetchers]},
    )


# ---------------------------------------------------------------------------
# Default live fetchers (mocked in tests so we never hit the network)
# ---------------------------------------------------------------------------


def _default_coinbase_fetcher() -> Iterable[Dict[str, Any]]:
    """Pull the Coinbase blog RSS feed; emit dicts with title/url/timestamp."""
    import feedparser

    parsed = feedparser.parse(COINBASE_BLOG_RSS_URL)
    out: List[Dict[str, Any]] = []
    for entry in (getattr(parsed, "entries", []) or []):
        out.append({
            "title": getattr(entry, "title", ""),
            "url":   getattr(entry, "link", ""),
            "id":    getattr(entry, "id", "") or getattr(entry, "guid", ""),
            "published": getattr(entry, "published", ""),
        })
    return out


def _default_binance_fetcher() -> Iterable[Dict[str, Any]]:
    """Pull Binance "New Listings" announcement category via their public CMS API."""
    import requests

    response = requests.post(
        BINANCE_ANNOUNCEMENTS_URL,
        json={
            "type": 1,            # 1 = "New Cryptocurrency Listings" category
            "pageSize": 20,
            "pageNo": 1,
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json() or {}
    catalogs = (((payload.get("data") or {}).get("catalogs") or [{}])[0].get("articles") or [])
    out: List[Dict[str, Any]] = []
    for article in catalogs:
        out.append({
            "title":     article.get("title", ""),
            "url":       f"https://www.binance.com/en/support/announcement/{article.get('code', '')}",
            "id":        article.get("id") or article.get("code"),
            "timestamp": article.get("releaseDate"),  # millis
        })
    return out
