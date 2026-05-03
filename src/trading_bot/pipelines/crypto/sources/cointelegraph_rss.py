"""CoinTelegraph RSS collector — second editorial feed for cross-source bonus.

Mirror of ``coindesk_rss`` but pointed at CoinTelegraph. Cross-source
bonus in the aggregator kicks in when both feeds carry the same story,
which is the right behaviour for editorial confirmations.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from trading_bot.pipelines.crypto.sources._base import (
    CRYPTO_RSS_TIMEOUT,
    CRYPTO_USER_AGENT,
    SourceResult,
    parse_rss_entries,
    utcnow,
)
from trading_bot.pipelines.crypto.sources.coindesk_rss import _write_entries

logger = logging.getLogger(__name__)

COINTELEGRAPH_RSS_URL = "https://cointelegraph.com/rss"


def collect_cointelegraph_rss(
    engine: Any,
    *,
    settings: Any = None,
    fetcher: Optional[Callable[[], list[dict]]] = None,
    now=None,
) -> SourceResult:
    """Pull CoinTelegraph RSS; same per-entry symbol-extraction as coindesk."""
    now = now or utcnow()
    try:
        entries = (fetcher or _default_fetcher)()
    except Exception as e:  # noqa: BLE001
        logger.warning("cointelegraph_rss fetch failed: %s", e)
        return SourceResult(source="cointelegraph_rss", error=str(e))

    return _write_entries(engine, entries or [], source_name="cointelegraph_rss", now=now)


def _default_fetcher() -> list[dict]:
    import requests

    resp = requests.get(
        COINTELEGRAPH_RSS_URL,
        timeout=CRYPTO_RSS_TIMEOUT,
        headers={"User-Agent": CRYPTO_USER_AGENT},
    )
    resp.raise_for_status()
    return parse_rss_entries(resp.content)
