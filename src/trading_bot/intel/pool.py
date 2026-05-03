"""Read API for the intel candidate pool.

Daemon scans call ``top_for_asset_class`` to get the candidate list before
falling back to existing screeners. Dashboard calls ``list_active`` and
``recent_events`` for observability.

This module is intentionally read-only. Writes go through ``aggregator``
and ``sources`` so the read path stays cheap and predictable.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from trading_bot.state_db import IntelCandidate, IntelEvent


def _not_dismissed_filter(now: dt.datetime):
    """SQLAlchemy filter expression: row is NOT under an active scout dismissal.

    Phase B: scout debate may set ``scout_dismissed_until`` to a future
    timestamp. Pool readers must hide those rows from strategy-resolution
    and scanner consumers until the TTL expires. NULL = not dismissed.
    """
    return or_(
        IntelCandidate.scout_dismissed_until.is_(None),
        IntelCandidate.scout_dismissed_until < now,
    )


# Lookback window for "fresh" pool candidates. Events older than this fall
# off the score (via the time-decay in the aggregator). Candidates with
# last_seen older than this are treated as not in the pool.
DEFAULT_MAX_AGE_HOURS = 24

# Score floor: candidates with score below this are treated as "in the
# pool but not surfacing" — won't be returned by top_for_asset_class even
# if they sit in the table. Filters out one-mention noise.
DEFAULT_MIN_SCORE = 0.5


@dataclass(frozen=True)
class PoolEntry:
    symbol: str
    asset_class: str
    score: float
    n_mentions: int
    n_sources: int
    last_seen: dt.datetime
    top_reason: str
    sources: dict[str, int]
    sentiment_avg: float | None
    # Phase B — scout debate state surfaced for downstream consumers.
    scout_verdict: str | None = None
    scout_dismissed_until: dt.datetime | None = None


def top_for_asset_class(
    engine,
    asset_class: str,
    *,
    n: int = 50,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    min_score: float = DEFAULT_MIN_SCORE,
    now: dt.datetime | None = None,
) -> list[PoolEntry]:
    """Return up to ``n`` highest-scoring candidates for the given asset
    class. Candidates older than ``max_age_hours`` (last_seen) or below
    ``min_score`` are excluded.

    Daemon callers typically pass:
      * stocks: asset_class='stock', n=200
      * crypto: asset_class='crypto', n=20
      * wheel:  asset_class='option_underlying', n=30

    Returns ``[]`` when the pool has nothing fresh — caller falls back to
    its existing universe source (cold-start safety net).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=max_age_hours)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidate)
            .filter(IntelCandidate.asset_class == asset_class)
            .filter(IntelCandidate.last_seen >= cutoff)
            .filter(IntelCandidate.score >= min_score)
            .filter(_not_dismissed_filter(now))
            .order_by(desc(IntelCandidate.score))
            .limit(n)
            .all()
        )
    return [_row_to_entry(r) for r in rows]


def lookup(
    engine, symbol: str, asset_class: str,
    *, now: dt.datetime | None = None,
    respect_scout_dismissal: bool = True,
) -> PoolEntry | None:
    """Per-symbol pool lookup. Returns None when the row doesn't exist OR
    when an active scout dismissal hides it (``respect_scout_dismissal``,
    default True). Pass ``respect_scout_dismissal=False`` to bypass the
    filter (used by the scout-debate override path that re-checks 8-K
    arrivals against currently-dismissed symbols).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        q = (
            session.query(IntelCandidate)
            .filter(IntelCandidate.symbol == symbol)
            .filter(IntelCandidate.asset_class == asset_class)
        )
        if respect_scout_dismissal:
            q = q.filter(_not_dismissed_filter(now))
        row = q.first()
    return _row_to_entry(row) if row else None


def lookup_score(
    engine,
    symbol: str,
    asset_class: str,
    *,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    now: dt.datetime | None = None,
) -> float | None:
    """Cheap accessor: just the candidate's current score, or ``None`` if no
    row exists, the row is stale (last_seen older than ``max_age_hours``), or
    the lookup itself errors. Used by the orchestrator's per-ticker strategy
    selection (regime-override path). Stale = ``None`` so a long-dormant
    candidate doesn't accidentally unlock a regime override.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        entry = lookup(engine, symbol, asset_class)
    except Exception:
        return None
    if entry is None:
        return None
    last_seen = entry.last_seen
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=dt.timezone.utc)
    if (now - last_seen).total_seconds() > max_age_hours * 3600:
        return None
    return float(entry.score)


def list_active(
    engine,
    *,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    min_score: float = 0.0,  # dashboard wants to see everything
    limit: int = 200,
    now: dt.datetime | None = None,
) -> list[PoolEntry]:
    """Used by the dashboard tile — every fresh candidate, regardless of
    score, up to ``limit``. Sort by score desc."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=max_age_hours)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidate)
            .filter(IntelCandidate.last_seen >= cutoff)
            .filter(IntelCandidate.score >= min_score)
            .order_by(desc(IntelCandidate.score))
            .limit(limit)
            .all()
        )
    return [_row_to_entry(r) for r in rows]


def recent_events(
    engine,
    *,
    symbol: str | None = None,
    limit: int = 50,
) -> list[IntelEvent]:
    """Audit-trail view: most recent events, optionally filtered by symbol."""
    with Session(engine) as session:
        q = session.query(IntelEvent).order_by(desc(IntelEvent.ingested_at))
        if symbol is not None:
            q = q.filter(IntelEvent.symbol == symbol)
        return q.limit(limit).all()


def is_pool_fresh(
    engine, *, max_age_hours: int = 2, now: dt.datetime | None = None
) -> bool:
    """Has the pool been refreshed recently?

    Universe sources call this to decide whether to consult the pool or
    fall through to the static screener. ``max_age_hours=2`` accommodates
    a missed nightly tick during US market hours; outside market hours
    the daemon doesn't trade so freshness matters less.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=max_age_hours)
    with Session(engine) as session:
        most_recent = (
            session.query(IntelCandidate)
            .order_by(desc(IntelCandidate.rolled_up_at))
            .first()
        )
    if most_recent is None:
        return False
    rolled_at = most_recent.rolled_up_at
    if rolled_at.tzinfo is None:
        rolled_at = rolled_at.replace(tzinfo=dt.timezone.utc)
    return rolled_at >= cutoff


def _row_to_entry(row: IntelCandidate) -> PoolEntry:
    try:
        sources = json.loads(row.sources_json or "{}")
        if not isinstance(sources, dict):
            sources = {}
    except Exception:
        sources = {}
    return PoolEntry(
        symbol=row.symbol,
        asset_class=row.asset_class,
        score=float(row.score),
        n_mentions=int(row.n_mentions),
        n_sources=int(row.n_sources),
        last_seen=row.last_seen,
        top_reason=row.top_reason or "",
        sources=sources,
        sentiment_avg=float(row.sentiment_avg) if row.sentiment_avg is not None else None,
        scout_verdict=getattr(row, "scout_verdict", None),
        scout_dismissed_until=getattr(row, "scout_dismissed_until", None),
    )


def top_symbols(entries: list[PoolEntry]) -> set[str]:
    """Convenience: extract a set of symbols from a list of pool entries.
    Daemon callers typically intersect this with their tradable set."""
    return {e.symbol for e in entries}
