"""Whale Alert collector — confirmed >$1M on-chain transfers.

This is the crypto pipeline's "SEC 8-K equivalent" — a confirmed
on-chain transaction is a primary-source, time-stamped, undeniable
material event. Highest weight (5.0) of any crypto source.

Sentiment heuristic (per the plan):
    direction == exchange-inbound  →  -0.5  (sell pressure heading to venue)
    exchange-outbound  to cold     →  +0.4  (accumulation moves to storage)
    everything else (wallet ↔ wallet) →  0.0  (neutral; flow is informational)

Free tier covers BTC + ETH; paid tiers add altcoins. Empty
``whale_alert_api_key`` → silent skip (matches the existing pattern
for ``cryptopanic_api_key`` and ``newsapi_key``).

Dedup: Whale Alert returns a stable ``hash`` per transaction, which we
pass through to ``stable_event_hash`` so re-fetching the same hour's
transactions doesn't write duplicates.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, List, Optional

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    stable_event_hash,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

WHALE_ALERT_API_URL = "https://api.whale-alert.io/v1/transactions"
DEFAULT_LOOKBACK_SECONDS = 3600       # last hour
DEFAULT_MIN_USD = 1_000_000           # match the source's free-tier floor

EXCHANGE_OWNER_TYPES = frozenset({"exchange", "exchange-deposit", "exchange-cold"})

# Owner-type → "this is cold storage" heuristic. Coinbase / Binance /
# Kraken cold wallets are tagged ``exchange-cold`` by Whale Alert.
COLD_WALLET_OWNER_TYPES = frozenset({"unknown", "exchange-cold", "wallet"})


def _classify_sentiment(from_type: str, to_type: str) -> float:
    """Map Whale Alert owner-types into [-1.0, +1.0] sentiment.

    Exchange-inbound: someone is moving size to a venue → likely sale → -0.5
    Exchange-outbound to cold: accumulation → +0.4
    Wallet ↔ wallet: structurally neutral → 0.0
    """
    f = (from_type or "").lower()
    t = (to_type or "").lower()
    if t in EXCHANGE_OWNER_TYPES and f not in EXCHANGE_OWNER_TYPES:
        return -0.5
    if f in EXCHANGE_OWNER_TYPES and t in COLD_WALLET_OWNER_TYPES and t not in EXCHANGE_OWNER_TYPES:
        return 0.4
    return 0.0


def collect_whale_alert(
    engine: Any,
    *,
    settings: Any,
    min_usd: int = DEFAULT_MIN_USD,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Pull recent Whale Alert transactions; write one event per transfer.

    ``fetcher`` is a hook for tests — if None, we use ``_default_fetcher``
    which calls the live API. In production tests + the smoke harness we
    inject a fake to avoid hitting the network or burning quota.
    """
    api_key = (getattr(settings, "whale_alert_api_key", "") or "").strip()
    if not api_key:
        logger.debug("whale_alert: no api key set, skipping silently")
        return SourceResult(source="whale_alert")

    now = now or utcnow()
    cutoff = now - dt.timedelta(seconds=lookback_seconds)

    try:
        payload = (fetcher or _default_fetcher)(
            api_key=api_key,
            start_unix=int(cutoff.timestamp()),
            min_usd=min_usd,
        )
    except Exception as e:  # noqa: BLE001 — fail-soft, never crash the role
        logger.warning("whale_alert: fetch failed: %s", e)
        return SourceResult(source="whale_alert", error=str(e))

    transactions = (payload or {}).get("transactions") or []
    written = 0
    skipped = 0
    for tx in transactions:
        try:
            row_written, row_skipped = _write_one_tx(engine, tx, now=now)
            written += row_written
            skipped += row_skipped
        except Exception as e:  # noqa: BLE001 — per-tx failure shouldn't kill batch
            logger.warning("whale_alert: skipped malformed tx: %s", e)
            skipped += 1

    return SourceResult(
        source="whale_alert",
        written=written,
        skipped=skipped,
        extra={"requested_min_usd": min_usd, "lookback_seconds": lookback_seconds},
    )


def _write_one_tx(engine: Any, tx: Dict[str, Any], *, now: dt.datetime) -> tuple[int, int]:
    """Map one Whale Alert transaction dict → IntelEventCrypto row.

    Returns (written_count, skipped_count) — usually (1, 0) or (0, 1).
    """
    raw_symbol = tx.get("symbol") or ""
    chain = (tx.get("blockchain") or "").lower() or None
    tx_hash = tx.get("hash") or ""
    amount_usd = float(tx.get("amount_usd") or 0.0)
    timestamp = tx.get("timestamp")
    from_type = (tx.get("from") or {}).get("owner_type", "")
    to_type = (tx.get("to") or {}).get("owner_type", "")

    if not raw_symbol or not tx_hash:
        return (0, 1)

    symbol = normalize_crypto_symbol(raw_symbol)
    sentiment = _classify_sentiment(from_type, to_type)
    headline = (
        f"{amount_usd / 1_000_000:.1f}M {raw_symbol.upper()} "
        f"({from_type or 'unknown'} → {to_type or 'unknown'})"
    )
    event_at: Optional[dt.datetime] = None
    if timestamp:
        try:
            event_at = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc)
        except (TypeError, ValueError):
            event_at = None

    ok = write_event(
        engine,
        symbol=symbol,
        source="whale_alert",
        headline=headline[:1000],
        url=f"https://whale-alert.io/transaction/{chain or 'unknown'}/{tx_hash}",
        sentiment=sentiment,
        raw_score=amount_usd,
        event_at=event_at,
        event_hash=stable_event_hash("whale_alert", chain or "?", tx_hash),
        chain=chain,
        tx_hash=tx_hash,
        now=now,
    )
    return (1, 0) if ok else (0, 1)


def _default_fetcher(*, api_key: str, start_unix: int, min_usd: int) -> Dict[str, Any]:
    """Live HTTP call to Whale Alert. Used in production; mocked in tests."""
    import requests  # local import — many tests don't need requests at all

    response = requests.get(
        WHALE_ALERT_API_URL,
        params={
            "api_key": api_key,
            "start": start_unix,
            "min_value": min_usd,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()
