"""Earnings-calendar source for the options pipeline (Phase 3).

For each underlying in the wheel-eligible universe, look up the next
earnings date and write one ``IntelEventOptions`` row when an earnings
event falls within the wheel's DTE lookahead window. The presence of
that row gates the scout debate's ``earnings_in_dte_window`` flag.

Default fetcher uses ``yfinance.Ticker.calendar`` (free, no key
required). Easily swappable for a paid feed (Polygon earnings,
Finnhub) when accuracy matters in production.

Fail-soft: a failing per-symbol lookup writes nothing for that symbol
but never blocks the rest of the universe scan.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Iterable, Optional, Sequence

from trading_bot.pipelines.options.sources._base import (
    SourceResult,
    stable_event_hash,
    write_event,
)

logger = logging.getLogger(__name__)


def _default_fetcher(symbol: str) -> Optional[dt.datetime]:
    """Return the next earnings datetime (UTC) for ``symbol`` via yfinance.

    Best-effort: yfinance occasionally returns DataFrames vs dicts
    across versions. We accept both and bail to None on parse errors.
    """
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("earnings_calendar: yfinance not installed; skipping")
        return None
    try:
        ticker = yf.Ticker(symbol)
        cal = getattr(ticker, "calendar", None)
        if cal is None:
            return None
        # yfinance returns dict in 0.2.x; older returned DataFrame.
        candidate: Any = None
        if isinstance(cal, dict):
            candidate = cal.get("Earnings Date")
            if isinstance(candidate, list) and candidate:
                candidate = candidate[0]
        else:
            # DataFrame: row "Earnings Date" col 0.
            try:
                candidate = cal.loc["Earnings Date"][0]
            except Exception:
                candidate = None
        if candidate is None:
            return None
        if isinstance(candidate, dt.datetime):
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=dt.timezone.utc)
            return candidate
        if isinstance(candidate, dt.date):
            return dt.datetime.combine(candidate, dt.time(13, 30), tzinfo=dt.timezone.utc)
        # ISO string fallback
        try:
            return dt.datetime.fromisoformat(str(candidate))
        except Exception:
            return None
    except Exception as e:
        logger.debug("earnings_calendar fetch failed for %s: %s", symbol, e)
        return None


def poll_earnings_calendar(
    engine: Any,
    *,
    symbols: Sequence[str],
    lookahead_days: int = 45,
    fetcher: Optional[Callable[[str], Optional[dt.datetime]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Look up the next earnings date for each symbol; write events for
    those whose date falls within ``lookahead_days``.

    Returns a SourceResult with ``written`` = number of new rows.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    fetch = fetcher or _default_fetcher
    cutoff = now + dt.timedelta(days=lookahead_days)
    written = 0
    skipped = 0
    error: Optional[str] = None

    for symbol in symbols:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            continue
        try:
            earnings_at = fetch(symbol)
        except Exception as e:  # noqa: BLE001 — per-symbol fail-soft
            logger.debug("earnings_calendar fetcher raised for %s: %s", symbol, e)
            earnings_at = None
        if earnings_at is None:
            skipped += 1
            continue
        if earnings_at < now or earnings_at > cutoff:
            skipped += 1
            continue
        days_to = (earnings_at - now).days
        # Stable per-(symbol, earnings_date) hash so re-polls dedup.
        event_hash = stable_event_hash(
            "earnings_calendar", symbol, earnings_at.date().isoformat(),
        )
        ok = write_event(
            engine,
            underlying=symbol,
            source="earnings_calendar",
            headline=f"{symbol} earnings in {days_to}d on {earnings_at.date().isoformat()}",
            event_at=earnings_at,
            event_hash=event_hash,
            sentiment=-0.3,  # earnings = binary risk → mild negative for wheel
            raw_score=float(days_to),
            now=now,
        )
        if ok:
            written += 1
        else:
            skipped += 1

    return SourceResult(
        source="earnings_calendar",
        written=written, skipped=skipped, error=error,
        extra={"lookahead_days": lookahead_days, "n_symbols": len(symbols)},
    )
