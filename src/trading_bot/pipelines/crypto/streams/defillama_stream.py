"""DefiLlama TVL stream adapter (Phase 1G.2).

Polls the DefiLlama protocols endpoint every ~60 seconds and converts
material TVL deltas (>=10% in 24h, |delta| not just noise) into
``StreamEvent`` rows.

A protocol's TVL spike or run-on-the-bank pattern is a fast-moving
signal that benefits from express-lane handling — capital flows can
move faster than ingestor cadence.

Sentiment heuristic (mirrors ``sources/token_unlocks_defillama.py``):
    24h TVL delta >= +10% → +0.4 (capital inflows = protocol confidence)
    24h TVL delta <= -10% → -0.5 (run-on-the-bank signal)
    |delta| < 10%         → no event written
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from trading_bot.pipelines.crypto.event_streamer import StreamEvent, ingest_stream_event
from trading_bot.pipelines.crypto.sources._base import normalize_crypto_symbol

logger = logging.getLogger(__name__)

DEFILLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"
DEFAULT_TVL_PCT_TRIGGER = 10.0


def _classify_tvl_delta(pct_24h: float, trigger_pct: float) -> Optional[float]:
    if pct_24h >= trigger_pct:
        return 0.4
    if pct_24h <= -trigger_pct:
        return -0.5
    return None


def defillama_payload_to_events(
    payload: Iterable[Dict[str, Any]],
    *,
    trigger_pct: float = DEFAULT_TVL_PCT_TRIGGER,
    now: Optional[dt.datetime] = None,
) -> List[StreamEvent]:
    """Convert the DefiLlama protocols list into StreamEvents.

    The protocols list is large (~3000 entries); only those with a
    material delta become events. We use a daily bucket as the natural_id
    so re-polling the same day produces dups (no new events written).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    out: List[StreamEvent] = []
    for proto in payload or []:
        raw_symbol = (proto.get("symbol") or proto.get("token") or "").upper()
        if not raw_symbol or raw_symbol == "-":
            continue
        try:
            pct_24h = float(proto.get("change_1d") or proto.get("pct_change_24h") or 0.0)
        except (TypeError, ValueError):
            continue
        sentiment = _classify_tvl_delta(pct_24h, trigger_pct)
        if sentiment is None:
            continue
        name = proto.get("name") or raw_symbol
        out.append(StreamEvent(
            symbol=normalize_crypto_symbol(raw_symbol),
            source="defillama_tvl",
            event_at=now,
            sentiment=sentiment,
            payload={"name": name, "pct_change_24h": pct_24h, "raw_symbol": raw_symbol},
            natural_id=f"{raw_symbol}|{today}",
        ))
    return out


def poll_defillama_tvl(
    engine: Any,
    *,
    fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    trigger_pct: float = DEFAULT_TVL_PCT_TRIGGER,
    now: Optional[dt.datetime] = None,
) -> int:
    """Poll DefiLlama and ingest material TVL-delta events. Returns count
    of newly-ingested rows (dups skipped via natural_id).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        payload = (fetcher or _default_fetcher)() or []
    except Exception as e:  # noqa: BLE001
        logger.warning("poll_defillama_tvl fetch failed: %s", e)
        return 0

    events = defillama_payload_to_events(payload, trigger_pct=trigger_pct, now=now)
    written = 0
    for ev in events:
        if ingest_stream_event(engine, event=ev, now=now) is not None:
            written += 1
    return written


def _default_fetcher() -> List[Dict[str, Any]]:
    """Live HTTP call to DefiLlama protocols endpoint."""
    import requests

    resp = requests.get(DEFILLAMA_PROTOCOLS_URL, timeout=15)
    resp.raise_for_status()
    return resp.json() or []
