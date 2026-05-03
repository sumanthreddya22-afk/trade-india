"""Crypto stream adapters (Phase 1G.1 + 1G.2).

Each adapter pulls one external real-time data source (REST poll or
WebSocket) and converts the native payload into ``StreamEvent`` rows
that ``event_streamer.ingest_stream_event`` persists to
``intel_stream_events_crypto``. The express-lane dispatcher then
routes the events into either a hold debate (for held positions) or a
scout debate (for newly-elevated candidates) within ~60 seconds.

Adapters share the common ``poll_*`` shape:
    ``poll_<source>(engine, *, fetcher, ..., now=None) -> int``
returning the count of newly-ingested rows. Each adapter manages its
own dedup key and fail-soft behaviour so a single bad source can't
break the streamer loop.

Sources implemented:
  - whale_alert        — >$1M on-chain transfers (Phase 1G.1; lives in event_streamer.py)
  - defillama_tvl      — protocol-level TVL deltas
  - etherscan_whales   — tracked whale-wallet activity (ERC-20)
  - binance_funding    — funding-rate flips on tracked perps

Caller (the streamer role) typically invokes ``poll_all_streams``
which walks each adapter sequentially.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def poll_all_streams(
    engine: Any,
    *,
    settings: Any,
    skip: Optional[List[str]] = None,
    only: Optional[List[str]] = None,
    now: Optional[dt.datetime] = None,
) -> Dict[str, int]:
    """Run every wired stream adapter sequentially per ADR 0003.

    Returns a per-adapter dict of newly-ingested-row counts. Empty key
    settings → silent skip per adapter (no errors).

    ``skip`` / ``only`` let callers narrow the run for ad-hoc CLI use.
    """
    from trading_bot.pipelines.crypto.event_streamer import poll_whale_alert
    from trading_bot.pipelines.crypto.streams.defillama_stream import poll_defillama_tvl
    from trading_bot.pipelines.crypto.streams.etherscan_whale_stream import (
        poll_etherscan_whales,
    )
    from trading_bot.pipelines.crypto.streams.binance_funding_stream import (
        poll_binance_funding,
    )

    skip_set = set(skip or [])
    only_set = set(only or [])
    out: Dict[str, int] = {}

    adapters: List[tuple[str, Callable[..., int]]] = [
        ("whale_alert",      lambda: poll_whale_alert(
            engine, fetcher=_default_whale_fetcher,
            api_key=getattr(settings, "whale_alert_api_key", "") or "", now=now,
        )),
        ("defillama_tvl",    lambda: poll_defillama_tvl(engine, now=now)),
        ("etherscan_whales", lambda: poll_etherscan_whales(
            engine, settings=settings,
            watched_addresses=getattr(settings, "etherscan_whale_addresses", None) or [],
            now=now,
        )),
        ("binance_funding",  lambda: poll_binance_funding(engine, now=now)),
    ]

    for name, fn in adapters:
        if only_set and name not in only_set:
            continue
        if name in skip_set:
            continue
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 — per-adapter fail-soft
            logger.warning("poll_all_streams %s failed: %s", name, e)
            out[name] = 0
    return out


def _default_whale_fetcher(*, api_key: str, start_unix: int, min_usd: int):
    """Live HTTP call to Whale Alert. Imported lazily to keep tests fast."""
    from trading_bot.pipelines.crypto.sources.whale_alert import _default_fetcher
    return _default_fetcher(api_key=api_key, start_unix=start_unix, min_usd=min_usd)
