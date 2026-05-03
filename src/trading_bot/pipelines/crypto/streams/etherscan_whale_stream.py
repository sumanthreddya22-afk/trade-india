"""Etherscan whale-wallet stream adapter (Phase 1G.2).

Polls a curated list of watched ERC-20 whale wallets every cycle and
converts each large transfer into a ``StreamEvent``. Mirrors the
collector at ``sources/etherscan_whales.py`` but emits StreamEvents
that flow through the express-lane dispatcher (so a fresh whale
movement triggers an immediate hold debate on the held position).

Sentiment heuristic mirrors the per-tick collector:
    transfer to known exchange contract → -0.4 (sell pressure)
    transfer from exchange to wallet    → +0.3 (accumulation)
    wallet ↔ wallet                      →  0.0 (informational)

Free-tier limits: 5 req/sec, 100k req/day. The poller spaces calls
~0.25s apart per watched address to stay under both ceilings.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from trading_bot.pipelines.crypto.event_streamer import StreamEvent, ingest_stream_event
from trading_bot.pipelines.crypto.sources._base import normalize_crypto_symbol

logger = logging.getLogger(__name__)

ETHERSCAN_API_URL = "https://api.etherscan.io/api"
DEFAULT_MIN_USD = 1_000_000
DEFAULT_LOOKBACK_SECONDS = 600       # 10 min — express path is faster than per-tick collector
DEFAULT_PER_ADDRESS_DELAY_SEC = 0.25
DEFAULT_PER_ADDRESS_LIMIT = 50

KNOWN_EXCHANGE_TAGS = frozenset({
    "binance", "coinbase", "kraken", "okx", "bitfinex", "bybit",
    "kucoin", "huobi", "gate", "ftx",
})


def _classify_sentiment(from_tag: str, to_tag: str) -> float:
    f = (from_tag or "").lower()
    t = (to_tag or "").lower()
    if any(x in t for x in KNOWN_EXCHANGE_TAGS) and not any(x in f for x in KNOWN_EXCHANGE_TAGS):
        return -0.4
    if any(x in f for x in KNOWN_EXCHANGE_TAGS) and not any(x in t for x in KNOWN_EXCHANGE_TAGS):
        return 0.3
    return 0.0


def etherscan_payload_to_events(
    payload: Dict[str, Any],
    *,
    watched_address: str,
    cutoff_unix: int,
    min_usd: float,
) -> List[StreamEvent]:
    """Convert one Etherscan tokentx response into StreamEvents."""
    out: List[StreamEvent] = []
    for tx in (payload or {}).get("result") or []:
        tx_hash = tx.get("hash") or tx.get("transactionHash") or ""
        if not tx_hash:
            continue

        try:
            timestamp = int(tx.get("timeStamp") or tx.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if timestamp < cutoff_unix:
            continue

        try:
            value_usd = float(tx.get("value_usd") or tx.get("amount_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        if value_usd < min_usd:
            continue

        raw_symbol = (tx.get("tokenSymbol") or tx.get("symbol") or "").upper()
        if not raw_symbol:
            continue

        from_addr = (tx.get("from") or "").lower()
        from_tag = (tx.get("from_tag") or tx.get("from_label") or "").lower()
        to_tag = (tx.get("to_tag") or tx.get("to_label") or "").lower()
        sentiment = _classify_sentiment(from_tag, to_tag)
        direction = "outbound" if from_addr == watched_address.lower() else "inbound"

        out.append(StreamEvent(
            symbol=normalize_crypto_symbol(raw_symbol),
            source="etherscan_whales",
            event_at=dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc),
            sentiment=sentiment,
            chain="ethereum",
            tx_hash=tx_hash,
            payload={
                "watched_address": watched_address[:20],
                "value_usd": value_usd,
                "direction": direction,
                "from_tag": from_tag, "to_tag": to_tag,
                "raw_symbol": raw_symbol,
            },
            natural_id=tx_hash,
        ))
    return out


def poll_etherscan_whales(
    engine: Any,
    *,
    settings: Any = None,
    watched_addresses: Optional[Sequence[str]] = None,
    min_usd: float = DEFAULT_MIN_USD,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    per_address_delay_sec: float = DEFAULT_PER_ADDRESS_DELAY_SEC,
    per_address_limit: int = DEFAULT_PER_ADDRESS_LIMIT,
    fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Optional[dt.datetime] = None,
) -> int:
    """Poll each watched whale wallet and ingest large transfers as
    StreamEvents. Returns total newly-ingested row count.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    api_key = (getattr(settings, "etherscan_api_key", "") or "").strip()
    if not api_key:
        return 0

    addresses = list(watched_addresses or [])
    if not addresses:
        return 0

    cutoff_unix = int((now - dt.timedelta(seconds=lookback_seconds)).timestamp())
    written = 0
    for i, address in enumerate(addresses):
        if i > 0:
            sleeper(per_address_delay_sec)
        try:
            payload = (fetcher or _default_fetcher)(
                api_key=api_key, address=address, limit=per_address_limit,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("poll_etherscan_whales %s fetch failed: %s", address, e)
            continue

        events = etherscan_payload_to_events(
            payload, watched_address=address, cutoff_unix=cutoff_unix, min_usd=min_usd,
        )
        for ev in events:
            if ingest_stream_event(engine, event=ev, now=now) is not None:
                written += 1
    return written


def _default_fetcher(*, api_key: str, address: str, limit: int) -> Dict[str, Any]:
    import requests

    resp = requests.get(
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
    resp.raise_for_status()
    return resp.json()
