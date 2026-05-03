"""ApeWisdom r/CryptoCurrency mention collector — ported from stocks tree.

Wraps ``trading_bot.intel_gates._fetch_crypto_mentions`` (which already
caches the API call for the spike-skip gate) and writes one event per
coin with mentions >= 5. Empty / unavailable response → silent no-op.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

DEFAULT_MENTIONS_FLOOR = 5


def collect_apewisdom_crypto(
    engine: Any,
    *,
    settings: Any = None,
    mentions_floor: int = DEFAULT_MENTIONS_FLOOR,
    fetcher: Optional[Callable[[], Optional[Dict[str, Dict[str, Any]]]]] = None,
    now=None,
) -> SourceResult:
    """Pull ApeWisdom r/CryptoCurrency snapshot; write events above the floor."""
    now = now or utcnow()
    try:
        snap = (fetcher or _default_fetcher)()
    except Exception as e:  # noqa: BLE001
        logger.warning("apewisdom_crypto fetch failed: %s", e)
        return SourceResult(source="apewisdom_crypto", error=str(e))

    if not snap:
        return SourceResult(source="apewisdom_crypto", extra={"note": "no data"})

    written = 0
    skipped = 0
    for ticker, row in snap.items():
        try:
            mentions = int(row.get("mentions") or 0)
            if mentions < mentions_floor:
                skipped += 1
                continue
            prior = int(row.get("mentions_24h_ago") or 0)
            delta = mentions - prior
            rank = int(row.get("rank") or 999)
            ok = write_event(
                engine,
                symbol=normalize_crypto_symbol(ticker),
                source="apewisdom_crypto",
                headline=(
                    f"{ticker} r/CryptoCurrency cluster: {mentions} mentions "
                    f"({delta:+d} vs 24h ago, rank {rank})"
                )[:1000],
                raw_score=float(mentions),
                now=now,
            )
            if ok:
                written += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("apewisdom_crypto: skipped malformed row: %s", e)
            skipped += 1
    return SourceResult(
        source="apewisdom_crypto",
        written=written, skipped=skipped,
    )


def _default_fetcher() -> Optional[Dict[str, Dict[str, Any]]]:
    """Live call via the existing intel_gates wrapper.

    We deliberately go through ``intel_gates._fetch_crypto_mentions``
    instead of duplicating the HTTP call — that wrapper has the cache +
    rate-limit handling for ApeWisdom that the stocks side already
    relies on. Importing it here is OK because intel_gates does not
    write to any stocks-pipeline tables.
    """
    from trading_bot.intel_gates import _fetch_crypto_mentions
    return _fetch_crypto_mentions()
