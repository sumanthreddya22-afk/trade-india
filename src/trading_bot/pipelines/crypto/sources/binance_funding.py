"""Binance perpetual-funding-rate collector.

Funding rates measure perp-vs-spot basis: extreme positive funding
means longs are paying shorts heavily (over-leveraged longs), extreme
negative means shorts paying longs (squeeze setup). The plan uses
funding > 0.10%/8h as a soft signal and > 0.15%/8h as a hard hold-debate
trigger.

Public REST endpoint: https://fapi.binance.com/fapi/v1/premiumIndex
returns latest mark price + lastFundingRate per symbol.

Sentiment heuristic:
    funding rate >= +0.001 (>= 0.10%/8h)  →  -0.4   (over-leveraged longs)
    funding rate <= -0.001                →  +0.3   (squeeze setup)
    abs(funding) < 0.0005                 →   0.0   (normal)

Symbol mapping: Binance perp symbols are like ``BTCUSDT`` — we
normalise to canonical ``BTC/USD`` via ``normalize_crypto_symbol``.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    stable_event_hash,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

# Same per-process latch as binance_funding_stream — Binance permanently
# 451s US IPs, so log once and short-circuit on repeated polls.
_GEO_BLOCKED = False

BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

DEFAULT_TRACKED_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "LINKUSDT", "ADAUSDT",
    "AVAXUSDT", "ARBUSDT", "OPUSDT", "MATICUSDT",
)

DEFAULT_SOFT_TRIGGER = 0.0010   # 0.10%/8h
DEFAULT_NORMAL_FLOOR = 0.0005   # below this magnitude → no event written


def _classify(rate: float) -> float:
    """Map funding rate to sentiment. Threshold matches plan + config."""
    if rate >= DEFAULT_SOFT_TRIGGER:
        return -0.4
    if rate <= -DEFAULT_SOFT_TRIGGER:
        return 0.3
    return 0.0


def collect_binance_funding(
    engine: Any,
    *,
    settings: Any = None,
    tracked_symbols: Iterable[str] = DEFAULT_TRACKED_SYMBOLS,
    fetcher: Optional[Callable[[List[str]], List[Dict[str, Any]]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Pull current funding rates; write one event per symbol with material funding."""
    now = now or utcnow()
    targets = list(tracked_symbols)
    if not targets:
        return SourceResult(source="binance_funding", extra={"reason": "no symbols configured"})

    global _GEO_BLOCKED
    if _GEO_BLOCKED:
        return SourceResult(source="binance_funding", extra={"reason": "geo_blocked"})
    try:
        rows = (fetcher or _default_fetcher)(targets) or []
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "451" in msg and "binance" in msg.lower():
            _GEO_BLOCKED = True
            logger.info(
                "binance_funding: Binance returned 451 (US geo-block) — "
                "suppressing further polls until daemon restart"
            )
            return SourceResult(source="binance_funding", extra={"reason": "geo_blocked"})
        logger.warning("binance_funding fetch failed: %s", e)
        return SourceResult(source="binance_funding", error=str(e))

    written = 0
    skipped = 0
    for row in rows:
        try:
            raw_symbol = row.get("symbol") or ""
            try:
                rate = float(row.get("lastFundingRate") or 0.0)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if abs(rate) < DEFAULT_NORMAL_FLOOR:
                # No material signal — skip writing the event so the table
                # only contains rows worth scoring.
                skipped += 1
                continue

            sentiment = _classify(rate)
            symbol = normalize_crypto_symbol(raw_symbol)
            funding_pct = rate * 100  # convert to %/8h for the headline
            headline = f"[binance perp] {raw_symbol} funding {funding_pct:+.3f}%/8h"
            funding_time_ms = row.get("nextFundingTime")
            event_at: Optional[dt.datetime] = None
            if funding_time_ms:
                try:
                    event_at = dt.datetime.fromtimestamp(int(funding_time_ms) / 1000.0, tz=dt.timezone.utc)
                except (TypeError, ValueError):
                    event_at = None

            ok = write_event(
                engine,
                symbol=symbol,
                source="binance_funding",
                headline=headline[:1000],
                url=f"https://www.binance.com/en/futures/{raw_symbol}",
                sentiment=sentiment,
                raw_score=rate,
                event_at=event_at,
                # Hash includes the funding-time bucket so each new funding
                # cycle (every 8h) writes a fresh event rather than dedup'ing.
                event_hash=stable_event_hash(
                    "binance_funding", raw_symbol,
                    str(int((funding_time_ms or 0) // (8 * 3600 * 1000))),
                ),
                now=now,
            )
            if ok:
                written += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("binance_funding: skipped malformed row: %s", e)
            skipped += 1

    return SourceResult(source="binance_funding", written=written, skipped=skipped,
                        extra={"symbols_polled": len(targets)})


def _default_fetcher(symbols: List[str]) -> List[Dict[str, Any]]:
    """Live HTTP call to Binance premiumIndex. Returns one row per symbol.

    The endpoint returns ALL symbols when called without a query — we
    filter client-side to the tracked list.
    """
    import requests

    resp = requests.get(BINANCE_PREMIUM_INDEX_URL, timeout=10)
    resp.raise_for_status()
    payload = resp.json() or []
    wanted = set(symbols)
    return [row for row in payload if isinstance(row, dict) and row.get("symbol") in wanted]
