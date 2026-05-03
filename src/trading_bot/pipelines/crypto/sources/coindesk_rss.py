"""CoinDesk RSS collector — broad editorial crypto news.

Ported from ``trading_bot.intel.sources.collect_coindesk_rss``. Uses
the new ``parse_rss_entries`` / ``parse_rfc822_or_iso`` helpers in
``_base.py`` and writes via the crypto pipeline's ``write_event``.

Symbol resolution: pulls each entry's title + description through
``trading_bot.intel._crypto_symbols.extract_symbols_from_text``
(slug→ticker map) — that helper is read-only and stocks-agnostic, so
importing it here doesn't violate pipeline isolation.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from trading_bot.pipelines.crypto.sources._base import (
    CRYPTO_RSS_TIMEOUT,
    CRYPTO_USER_AGENT,
    SourceResult,
    parse_rfc822_or_iso,
    parse_rss_entries,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

COINDESK_RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"


def collect_coindesk_rss(
    engine: Any,
    *,
    settings: Any = None,
    fetcher: Optional[Callable[[], list[dict]]] = None,
    now=None,
) -> SourceResult:
    """Pull CoinDesk RSS, extract crypto symbols per entry, write one event per
    (entry × symbol) pair. Empty body → silent no-op.
    """
    now = now or utcnow()
    try:
        entries = (fetcher or _default_fetcher)()
    except Exception as e:  # noqa: BLE001
        logger.warning("coindesk_rss fetch failed: %s", e)
        return SourceResult(source="coindesk_rss", error=str(e))

    return _write_entries(engine, entries or [], source_name="coindesk_rss", now=now)


def _write_entries(engine, entries, *, source_name: str, now) -> SourceResult:
    from trading_bot.intel._crypto_symbols import extract_symbols_from_text

    written = 0
    skipped = 0
    for ent in entries:
        title = (ent.get("title") or "").strip()
        desc = ent.get("description") or ""
        text = f"{title} {desc}"
        symbols = extract_symbols_from_text(text)
        if not symbols:
            continue
        published = parse_rfc822_or_iso(ent.get("published") or "")
        link = ent.get("link") or ""
        for sym in symbols:
            ok = write_event(
                engine,
                symbol=f"{sym}/USD",
                source=source_name,
                headline=title[:240],
                url=link,
                event_at=published,
                now=now,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    return SourceResult(source=source_name, written=written, skipped=skipped)


def _default_fetcher() -> list[dict]:
    import requests

    resp = requests.get(
        COINDESK_RSS_URL,
        timeout=CRYPTO_RSS_TIMEOUT,
        headers={"User-Agent": CRYPTO_USER_AGENT},
    )
    resp.raise_for_status()
    return parse_rss_entries(resp.content)
