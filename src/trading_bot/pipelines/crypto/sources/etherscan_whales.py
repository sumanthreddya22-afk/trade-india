"""Etherscan ERC-20 whale-wallet poller.

Backup / depth source for ERC-20 movements that Whale Alert's free tier
doesn't cover. Polls a curated list of ``watched_whale_addresses`` (one
or more ETH addresses known to belong to actively-tracked entities) for
recent ERC-20 transfers above a USD threshold.

The watched-address list is intentionally empty by default. Phase 1D
(crypto lesson loop, per-chain attribution) will populate it from
addresses whose flows correlate with winning trades. Until then this
collector is wired but dormant — same fail-soft posture as
``whale_alert`` when the api key is unset.

Sentiment heuristic mirrors whale_alert:
    transfer to a known exchange contract → -0.4
    transfer from exchange to wallet      → +0.3
    wallet ↔ wallet                        →  0.0

Free-tier limits: 5 req/sec, 100k req/day. The collector spaces calls
~0.25s apart per watched address to stay safely under both ceilings.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    stable_event_hash,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

ETHERSCAN_API_URL = "https://api.etherscan.io/api"
DEFAULT_MIN_USD = 1_000_000
DEFAULT_LOOKBACK_SECONDS = 3600
DEFAULT_PER_ADDRESS_DELAY_SEC = 0.25       # ~4 req/sec — under the 5/sec ceiling
DEFAULT_PER_ADDRESS_LIMIT = 50

# Known DeFi exchange contract prefixes / tags (kept as a small set;
# Phase 1D's lesson loop will surface more from realised flows).
KNOWN_EXCHANGE_TAGS = frozenset({
    "binance", "coinbase", "kraken", "okx", "bitfinex", "bybit",
    "kucoin", "huobi", "gate", "ftx",
})


def _classify_sentiment(from_tag: str, to_tag: str) -> float:
    """Map source/destination tags into [-1.0, +1.0] sentiment.

    More conservative magnitudes than whale_alert because Etherscan tags
    are heuristic strings (not blockchain-confirmed exchange ownership).
    """
    f = (from_tag or "").lower()
    t = (to_tag or "").lower()
    if any(x in t for x in KNOWN_EXCHANGE_TAGS) and not any(x in f for x in KNOWN_EXCHANGE_TAGS):
        return -0.4
    if any(x in f for x in KNOWN_EXCHANGE_TAGS) and not any(x in t for x in KNOWN_EXCHANGE_TAGS):
        return 0.3
    return 0.0


def collect_etherscan_whales(
    engine: Any,
    *,
    settings: Any,
    watched_addresses: Optional[Sequence[str]] = None,
    min_usd: int = DEFAULT_MIN_USD,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    per_address_delay_sec: float = DEFAULT_PER_ADDRESS_DELAY_SEC,
    per_address_limit: int = DEFAULT_PER_ADDRESS_LIMIT,
    fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Poll Etherscan ``tokentx`` for each watched address; write large transfers.

    ``watched_addresses`` defaults to an empty list — see module docstring.
    ``fetcher`` and ``sleeper`` are test hooks (avoid live HTTP / real sleeps).
    """
    api_key = (getattr(settings, "etherscan_api_key", "") or "").strip()
    if not api_key:
        logger.debug("etherscan_whales: no api key set, skipping silently")
        return SourceResult(source="etherscan_whales")

    addresses = list(watched_addresses or [])
    if not addresses:
        logger.debug("etherscan_whales: empty watched-address list, skipping")
        return SourceResult(
            source="etherscan_whales",
            extra={"reason": "no watched addresses configured"},
        )

    now = now or utcnow()
    cutoff_unix = int((now - dt.timedelta(seconds=lookback_seconds)).timestamp())

    written_total = 0
    skipped_total = 0
    errors: List[str] = []

    for i, address in enumerate(addresses):
        if i > 0:
            sleeper(per_address_delay_sec)
        try:
            payload = (fetcher or _default_fetcher)(
                api_key=api_key,
                address=address,
                limit=per_address_limit,
            )
        except Exception as e:  # noqa: BLE001 — per-address failure shouldn't kill batch
            logger.warning("etherscan_whales: %s fetch failed: %s", address, e)
            errors.append(f"{address}:{e}")
            continue

        for tx in (payload or {}).get("result") or []:
            try:
                w, s = _write_one_tx(
                    engine, tx, watched=address.lower(),
                    cutoff_unix=cutoff_unix, min_usd=min_usd, now=now,
                )
                written_total += w
                skipped_total += s
            except Exception as e:  # noqa: BLE001 — skip malformed, continue
                logger.warning("etherscan_whales: skipped malformed tx: %s", e)
                skipped_total += 1

    return SourceResult(
        source="etherscan_whales",
        written=written_total,
        skipped=skipped_total,
        error="; ".join(errors) if errors else None,
        extra={
            "addresses_polled": len(addresses),
            "min_usd": min_usd,
            "lookback_seconds": lookback_seconds,
        },
    )


def _write_one_tx(
    engine: Any,
    tx: Dict[str, Any],
    *,
    watched: str,
    cutoff_unix: int,
    min_usd: int,
    now: dt.datetime,
) -> tuple[int, int]:
    """Map one Etherscan token-transfer dict → IntelEventCrypto row."""
    tx_hash = tx.get("hash") or tx.get("transactionHash") or ""
    if not tx_hash:
        return (0, 1)

    try:
        timestamp = int(tx.get("timeStamp") or tx.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp < cutoff_unix:
        return (0, 1)

    raw_value = float(tx.get("value_usd") or tx.get("amount_usd") or 0.0)
    if raw_value < min_usd:
        return (0, 1)

    raw_symbol = (tx.get("tokenSymbol") or tx.get("symbol") or "").upper()
    if not raw_symbol:
        return (0, 1)

    from_addr = (tx.get("from") or "").lower()
    to_addr = (tx.get("to") or "").lower()
    from_tag = (tx.get("from_tag") or tx.get("from_label") or "").lower()
    to_tag = (tx.get("to_tag") or tx.get("to_label") or "").lower()

    symbol = normalize_crypto_symbol(raw_symbol)
    sentiment = _classify_sentiment(from_tag, to_tag)
    direction = "outbound" if from_addr == watched else "inbound"
    headline = (
        f"{raw_value / 1_000_000:.1f}M {raw_symbol} "
        f"({direction} for watched {watched[:10]}…)"
    )
    event_at = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc) if timestamp else None

    ok = write_event(
        engine,
        symbol=symbol,
        source="etherscan_whales",
        headline=headline[:1000],
        url=f"https://etherscan.io/tx/{tx_hash}",
        sentiment=sentiment,
        raw_score=raw_value,
        event_at=event_at,
        event_hash=stable_event_hash("etherscan_whales", "ethereum", tx_hash),
        chain="ethereum",
        tx_hash=tx_hash,
        now=now,
    )
    return (1, 0) if ok else (0, 1)


def _default_fetcher(*, api_key: str, address: str, limit: int) -> Dict[str, Any]:
    """Live HTTP call to Etherscan tokentx endpoint."""
    import requests

    response = requests.get(
        ETHERSCAN_API_URL,
        params={
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
            "apikey": api_key,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()
