"""CBOE SKEW index source for the options pipeline (Phase 3).

The CBOE SKEW Index measures the perceived tail risk of S&P 500
returns. Higher SKEW = market pricing more downside fear; lower SKEW
= complacent. The wheel's macro overlay (Yusuf Hassan) reads the
latest snapshot to weigh whether to lean rich or thin on premium.

Source: FRED series ``SKEWINDX`` (free, no key required).

Per-underlying attribution is the index level — every wheel candidate
gets the same value tagged on its IntelCandidateOptions.cboe_skew
column on roll-up. We write a single ``IntelEventOptions`` row per
new SKEW reading with ``underlying='SPX'`` (a synthetic placeholder)
so the rollup query can pick it up.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

from trading_bot.pipelines.options.sources._base import (
    SourceResult,
    stable_event_hash,
    write_event,
)

logger = logging.getLogger(__name__)


FRED_SKEW_URL = "https://api.stlouisfed.org/fred/series/observations"


def _default_fetcher() -> Optional[tuple[float, dt.datetime]]:
    """Pull the latest SKEW observation from FRED (no key needed for the
    public observations endpoint via the alternative direct CSV).

    Returns (value, observation_date_utc) or None on failure.
    """
    try:
        import requests
    except ImportError:
        logger.warning("cboe_skew: requests not installed; skipping")
        return None
    # FRED's CSV download endpoint — no API key, no rate limit issues.
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SKEWINDX"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("cboe_skew fetcher failed: %s", e)
        return None
    text = (resp.text or "").strip()
    if not text:
        return None
    # CSV header: DATE,SKEWINDX
    # We want the last non-NaN row.
    last_value: Optional[float] = None
    last_date: Optional[dt.datetime] = None
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date_str, val_str = parts[0].strip(), parts[1].strip()
        if not val_str or val_str == ".":
            continue
        try:
            val = float(val_str)
            d = dt.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        last_value = val
        last_date = dt.datetime.combine(d, dt.time(21, 0), tzinfo=dt.timezone.utc)
    if last_value is None or last_date is None:
        return None
    return (last_value, last_date)


def poll_cboe_skew(
    engine: Any,
    *,
    fetcher: Optional[Callable[[], Optional[tuple[float, dt.datetime]]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Pull the latest CBOE SKEW reading and write one IntelEventOptions
    row tagged with underlying='SPX' (synthetic).

    Idempotent: re-running the same day is a no-op via the
    (underlying, source, event_hash) unique constraint.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    fetch = fetcher or _default_fetcher

    written = 0
    skipped = 0
    error: Optional[str] = None
    extra: dict = {}

    try:
        result = fetch()
    except Exception as e:  # noqa: BLE001 — fail-soft
        logger.warning("cboe_skew fetcher raised: %s", e)
        return SourceResult(
            source="cboe_skew", written=0, skipped=0, error=str(e),
        )

    if result is None:
        return SourceResult(source="cboe_skew", written=0, skipped=1)

    value, observed_at = result
    extra["skew_value"] = value
    extra["observed_at"] = observed_at.isoformat()

    # Sentiment heuristic: SKEW > 145 = elevated tail-risk pricing →
    # thinner premium, lean conservative; SKEW < 120 = complacent →
    # cheaper hedges, lean aggressive. Map onto [-1, +1].
    if value >= 145:
        sentiment = -0.5
    elif value <= 120:
        sentiment = 0.3
    else:
        sentiment = 0.0

    event_hash = stable_event_hash(
        "cboe_skew", "SPX", observed_at.date().isoformat(),
    )
    ok = write_event(
        engine,
        underlying="SPX",
        source="cboe_skew",
        headline=f"CBOE SKEW {value:.2f} (observed {observed_at.date().isoformat()})",
        event_at=observed_at,
        event_hash=event_hash,
        sentiment=sentiment,
        raw_score=value,
        now=now,
    )
    if ok:
        written += 1
    else:
        skipped += 1

    return SourceResult(
        source="cboe_skew",
        written=written, skipped=skipped, error=error, extra=extra,
    )
