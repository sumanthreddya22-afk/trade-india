"""Binance perpetual-funding stream adapter (Phase 1G.2).

Polls the Binance ``premiumIndex`` REST endpoint every ~60 seconds
(faster than the per-tick ``binance_funding`` collector) and emits
``StreamEvent`` rows for symbols whose funding rate has flipped into
the soft-trigger band. The express-lane dispatcher then fires an
immediate hold debate for any held perp showing extreme funding.

Sentiment heuristic mirrors ``sources/binance_funding.py``:
    funding rate >= +0.10%/8h → -0.4 (over-leveraged longs)
    funding rate <= -0.10%/8h → +0.3 (squeeze setup)
    |funding| < 0.05%/8h      → no event written

Dedup: bucketed by funding-period (every 8h) so the same funding rate
across multiple polls within one period writes only one event.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Iterable, List, Optional

from trading_bot.pipelines.crypto.event_streamer import StreamEvent, ingest_stream_event
from trading_bot.pipelines.crypto.sources._base import normalize_crypto_symbol

logger = logging.getLogger(__name__)

BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

DEFAULT_TRACKED_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "LINKUSDT", "ADAUSDT", "AVAXUSDT", "ARBUSDT", "OPUSDT", "MATICUSDT",
)
SOFT_TRIGGER = 0.0010   # 0.10%/8h
NORMAL_FLOOR = 0.0005   # below this magnitude → no event


def _classify(rate: float) -> Optional[float]:
    if abs(rate) < NORMAL_FLOOR:
        return None
    if rate >= SOFT_TRIGGER:
        return -0.4
    if rate <= -SOFT_TRIGGER:
        return 0.3
    return None


def binance_funding_payload_to_events(
    payload: Iterable[dict],
    *,
    tracked: Iterable[str],
    now: Optional[dt.datetime] = None,
) -> List[StreamEvent]:
    """Convert a Binance premiumIndex response into StreamEvents.

    Only emits events for the tracked perp list — Binance returns ALL
    symbols when called without a query, so we filter client-side.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    wanted = {s.upper() for s in tracked}
    out: List[StreamEvent] = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        raw_symbol = (row.get("symbol") or "").upper()
        if raw_symbol not in wanted:
            continue
        try:
            rate = float(row.get("lastFundingRate") or 0.0)
        except (TypeError, ValueError):
            continue
        sentiment = _classify(rate)
        if sentiment is None:
            continue

        # Bucket into 8-hour funding periods so the same period only
        # writes one row per (symbol, period).
        try:
            funding_time_ms = int(row.get("nextFundingTime") or 0)
        except (TypeError, ValueError):
            funding_time_ms = 0
        funding_period_bucket = funding_time_ms // (8 * 3600 * 1000) if funding_time_ms else 0

        canonical = normalize_crypto_symbol(raw_symbol)
        out.append(StreamEvent(
            symbol=canonical,
            source="binance_funding",
            event_at=now,
            sentiment=sentiment,
            payload={
                "raw_symbol": raw_symbol,
                "funding_rate": rate,
                "funding_period_bucket": funding_period_bucket,
            },
            natural_id=f"{raw_symbol}|funding_period_{funding_period_bucket}",
        ))
    return out


def poll_binance_funding(
    engine: Any,
    *,
    tracked_symbols: Iterable[str] = DEFAULT_TRACKED_SYMBOLS,
    fetcher: Optional[Callable[[], List[dict]]] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Poll Binance funding rates and ingest material-funding StreamEvents."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        payload = (fetcher or _default_fetcher)() or []
    except Exception as e:  # noqa: BLE001
        logger.warning("poll_binance_funding fetch failed: %s", e)
        return 0

    events = binance_funding_payload_to_events(payload, tracked=tracked_symbols, now=now)
    written = 0
    for ev in events:
        if ingest_stream_event(engine, event=ev, now=now) is not None:
            written += 1
    return written


def _default_fetcher() -> List[dict]:
    """Live HTTP call to Binance premiumIndex."""
    import requests

    resp = requests.get(BINANCE_PREMIUM_INDEX_URL, timeout=10)
    resp.raise_for_status()
    return resp.json() or []
