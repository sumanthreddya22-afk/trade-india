"""Rekt.news exploit collector.

Highest-weight negative-side primary source. RSS feed at
https://rekt.news/rss/ — each post documents one DeFi exploit / hack /
rug-pull with the impacted protocol(s) named in the title or body.

Sentiment heuristic:
    confirmed exploit                    →  -0.8
    exploit affecting bridge / cross-chain → -0.9
    rug-pull confirmation                 →  -1.0

Symbol resolution: the post title typically names the protocol
(e.g. "Curve Hack", "Mango Markets", "Wormhole Bridge"). We map a
small set of well-known protocols to their tokens via a curated
table, and Phase 1F's adversarial layer adds the chain context.
Unmapped protocols write the event under a synthetic ``CHAIN`` symbol
that the aggregator can still consume for "this chain has fresh
exploit risk" downstream gates.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    stable_event_hash,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

REKT_RSS_URL = "https://rekt.news/rss/"


# Protocol name → (token symbol, chain). Curated; extend with the lesson loop.
PROTOCOL_TO_SYMBOL_CHAIN: Dict[str, tuple[str, str]] = {
    "curve":      ("CRV",  "ethereum"),
    "uniswap":    ("UNI",  "ethereum"),
    "aave":       ("AAVE", "ethereum"),
    "compound":   ("COMP", "ethereum"),
    "maker":      ("MKR",  "ethereum"),
    "wormhole":   ("ETH",  "ethereum"),  # bridge — affects ETH side most directly
    "ronin":      ("ETH",  "ethereum"),
    "nomad":      ("ETH",  "ethereum"),
    "anchor":     ("ATOM", "cosmos"),
    "mango":      ("SOL",  "solana"),
    "solend":     ("SOL",  "solana"),
    "wintermute": ("ETH",  "ethereum"),
    "euler":      ("EUL",  "ethereum"),
    "harmony":    ("ONE",  "harmony"),
    "binance":    ("BNB",  "bsc"),
    "arbitrum":   ("ARB",  "ethereum"),
    "optimism":   ("OP",   "ethereum"),
    "base":       ("ETH",  "base"),
}


_RUG_KEYWORDS    = ("rug",   "rug pull", "rugpull")
_BRIDGE_KEYWORDS = ("bridge", "cross-chain", "cross chain")
_EXPLOIT_KEYWORDS = ("hack", "exploit", "drained", "stolen", "compromise")


def _classify_severity(title: str, body: str = "") -> float:
    """Sentiment from title + body keywords. Always negative."""
    blob = f"{title}\n{body}".lower()
    if any(k in blob for k in _RUG_KEYWORDS):
        return -1.0
    if any(k in blob for k in _BRIDGE_KEYWORDS):
        return -0.9
    if any(k in blob for k in _EXPLOIT_KEYWORDS):
        return -0.8
    # Default — Rekt only publishes incidents, so even unflagged posts carry weight
    return -0.7


def _resolve_protocol(title: str) -> tuple[str, Optional[str]]:
    """Return (symbol_for_event, chain). Falls back to ('CHAIN_*', chain) when the
    protocol isn't in the mapping table — the aggregator can still surface
    "this chain has fresh exploit risk" via this synthetic symbol.
    """
    t = title.lower()
    for name, (sym, chain) in PROTOCOL_TO_SYMBOL_CHAIN.items():
        if re.search(rf"\b{re.escape(name)}\b", t):
            return sym, chain
    return "EXPLOIT", None  # synthetic catch-all symbol; chain unknown


def collect_rekt_news(
    engine: Any,
    *,
    settings: Any = None,
    fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Pull recent Rekt.news posts; write one event per exploit."""
    now = now or utcnow()
    try:
        items = list((fetcher or _default_fetcher)() or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("rekt_news fetch failed: %s", e)
        return SourceResult(source="rekt_exploit", error=str(e))

    written = 0
    skipped = 0
    for item in items:
        title = (item.get("title") or "").strip()
        body = item.get("summary") or item.get("body") or ""
        url = item.get("url") or item.get("link") or ""
        item_id = str(item.get("id") or item.get("guid") or url or title)
        ts_raw = item.get("event_at") or item.get("published")
        if not title:
            skipped += 1
            continue

        symbol, chain = _resolve_protocol(title)
        sentiment = _classify_severity(title, body)
        event_at = _parse_timestamp(ts_raw)

        ok = write_event(
            engine,
            symbol=normalize_crypto_symbol(symbol),
            source="rekt_exploit",
            headline=f"[rekt] {title}"[:1000],
            url=url,
            sentiment=sentiment,
            event_at=event_at,
            event_hash=stable_event_hash("rekt_exploit", item_id),
            chain=chain,
            now=now,
        )
        if ok:
            written += 1
        else:
            skipped += 1

    return SourceResult(source="rekt_exploit", written=written, skipped=skipped)


def _parse_timestamp(raw: Any) -> Optional[dt.datetime]:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.timezone.utc)
    try:
        v = int(raw)
        if v > 10_000_000_000:
            v //= 1000
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_fetcher() -> Iterable[Dict[str, Any]]:
    """Pull the Rekt.news RSS feed."""
    import feedparser

    parsed = feedparser.parse(REKT_RSS_URL)
    out: List[Dict[str, Any]] = []
    for entry in (getattr(parsed, "entries", []) or []):
        out.append({
            "title":     getattr(entry, "title", ""),
            "url":       getattr(entry, "link", ""),
            "id":        getattr(entry, "id", "") or getattr(entry, "guid", ""),
            "summary":   getattr(entry, "summary", ""),
            "published": getattr(entry, "published", ""),
        })
    return out
