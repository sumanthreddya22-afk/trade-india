"""TokenUnlocks + DefiLlama collector — two free APIs, one collector.

These are paired because both surface "supply / TVL shock" data and
share the same fail-soft + dedup discipline. Writes events under two
distinct ``source`` strings so weights/decays remain independent.

TokenUnlocks (token_unlocks):
    Cliff / vesting events of >$10M USD value → -0.5 sentiment
    (sudden supply hitting the market is a structural overhang)

DefiLlama (defillama_tvl):
    24h TVL change > +10% → +0.4 (capital inflows, protocol confidence)
    24h TVL change < -10% → -0.5 (run-on-the-bank signal)
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

TOKENUNLOCKS_API_URL = "https://token.unlocks.app/api/v1/events"
DEFILLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"

DEFAULT_UNLOCK_USD_FLOOR = 10_000_000
DEFAULT_TVL_PCT_TRIGGER = 10.0


def _classify_unlock(usd_value: float) -> float:
    return -0.5 if usd_value >= DEFAULT_UNLOCK_USD_FLOOR else 0.0


def _classify_tvl_delta(pct_24h: float, trigger_pct: float) -> float:
    if pct_24h >= trigger_pct:
        return 0.4
    if pct_24h <= -trigger_pct:
        return -0.5
    return 0.0


def collect_token_unlocks_defillama(
    engine: Any,
    *,
    settings: Any = None,
    unlock_usd_floor: int = DEFAULT_UNLOCK_USD_FLOOR,
    tvl_pct_trigger: float = DEFAULT_TVL_PCT_TRIGGER,
    unlock_fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    tvl_fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Run both sub-collectors. Returns one combined SourceResult so the
    orchestration layer treats this as a single source line.
    """
    now = now or utcnow()
    written = 0
    skipped = 0
    errors: List[str] = []

    # ---- TokenUnlocks ----------------------------------------------------
    try:
        unlocks = list((unlock_fetcher or _default_unlock_fetcher)() or [])
        for ev in unlocks:
            try:
                w, s = _write_unlock(engine, ev, floor=unlock_usd_floor, now=now)
                written += w; skipped += s
            except Exception as e:  # noqa: BLE001
                logger.warning("token_unlocks: skipped malformed: %s", e)
                skipped += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("token_unlocks fetch failed: %s", e)
        errors.append(f"token_unlocks:{e}")

    # ---- DefiLlama TVL ---------------------------------------------------
    try:
        protocols = list((tvl_fetcher or _default_tvl_fetcher)() or [])
        for proto in protocols:
            try:
                w, s = _write_tvl(engine, proto, trigger_pct=tvl_pct_trigger, now=now)
                written += w; skipped += s
            except Exception as e:  # noqa: BLE001
                logger.warning("defillama_tvl: skipped malformed: %s", e)
                skipped += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("defillama_tvl fetch failed: %s", e)
        errors.append(f"defillama_tvl:{e}")

    # Combined source label so collect_all reports a single line; sub-source
    # names are visible inside the events themselves.
    return SourceResult(
        source="token_unlocks_defillama",
        written=written,
        skipped=skipped,
        error="; ".join(errors) if errors else None,
        extra={"unlock_usd_floor": unlock_usd_floor, "tvl_pct_trigger": tvl_pct_trigger},
    )


def _write_unlock(
    engine: Any, ev: Dict[str, Any], *, floor: int, now: dt.datetime,
) -> tuple[int, int]:
    raw_symbol = (ev.get("symbol") or ev.get("token") or "").upper()
    usd_value = float(ev.get("usd_value") or ev.get("value_usd") or 0.0)
    if not raw_symbol or usd_value < floor:
        return (0, 1)

    unlock_at_ts = ev.get("unlock_at") or ev.get("timestamp")
    event_at = _parse_timestamp(unlock_at_ts)
    sentiment = _classify_unlock(usd_value)
    headline = f"[unlock] {raw_symbol} ${usd_value/1_000_000:.0f}M unlocking"
    event_id = str(ev.get("id") or f"{raw_symbol}-{unlock_at_ts}")

    ok = write_event(
        engine,
        symbol=normalize_crypto_symbol(raw_symbol),
        source="token_unlocks",
        headline=headline[:1000],
        url=f"https://token.unlocks.app/{raw_symbol.lower()}",
        sentiment=sentiment,
        raw_score=usd_value,
        event_at=event_at,
        event_hash=stable_event_hash("token_unlocks", event_id),
        now=now,
    )
    return (1, 0) if ok else (0, 1)


def _write_tvl(
    engine: Any, proto: Dict[str, Any], *, trigger_pct: float, now: dt.datetime,
) -> tuple[int, int]:
    raw_symbol = (proto.get("symbol") or proto.get("token") or "").upper()
    if not raw_symbol or raw_symbol == "-":
        return (0, 1)
    try:
        pct_24h = float(proto.get("change_1d") or proto.get("pct_change_24h") or 0.0)
    except (TypeError, ValueError):
        return (0, 1)
    sentiment = _classify_tvl_delta(pct_24h, trigger_pct)
    if sentiment == 0.0:
        # No material delta — skip rather than spam the table with
        # neutral-sentiment noise.
        return (0, 1)
    name = proto.get("name") or raw_symbol
    headline = f"[defillama] {name} TVL {pct_24h:+.1f}% 24h"
    # Bucket the timestamp by day so re-running the same day dedups
    # but the next day's call writes a new event.
    today_bucket = now.date().isoformat()
    event_id = stable_event_hash("defillama_tvl", raw_symbol, today_bucket)

    ok = write_event(
        engine,
        symbol=normalize_crypto_symbol(raw_symbol),
        source="defillama_tvl",
        headline=headline[:1000],
        url=f"https://defillama.com/protocol/{(name or '').lower().replace(' ', '-')}",
        sentiment=sentiment,
        raw_score=pct_24h,
        event_at=now,
        event_hash=event_id,
        now=now,
    )
    return (1, 0) if ok else (0, 1)


def _parse_timestamp(raw: Any) -> Optional[dt.datetime]:
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.timezone.utc)
    try:
        v = int(raw)
        if v > 10_000_000_000:
            v //= 1000
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_unlock_fetcher() -> Iterable[Dict[str, Any]]:
    """Live call to TokenUnlocks API (free, no key required for public events)."""
    import requests

    resp = requests.get(TOKENUNLOCKS_API_URL, timeout=10)
    resp.raise_for_status()
    payload = resp.json() or {}
    return payload.get("events") or payload.get("data") or []


def _default_tvl_fetcher() -> Iterable[Dict[str, Any]]:
    """Live call to DefiLlama protocols list."""
    import requests

    resp = requests.get(DEFILLAMA_PROTOCOLS_URL, timeout=15)
    resp.raise_for_status()
    return resp.json() or []
