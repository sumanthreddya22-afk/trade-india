"""Options aggregator — rolls intel_events_options into intel_candidates_options.

Per Option 2 (independent pipelines), this is a per-pipeline copy of
the stocks/crypto aggregator's scoring + roll-up logic. Reads only
from the options-owned tables; writes only to ``intel_candidates_options``.

Scoring formula mirrors crypto's shape:

  per_event_score = source_weight[source]
                  * exp(-age_hours / decay_hours[source])
                  * (1 + abs(sentiment) * 0.5)

  per_underlying_score = sum(per_event_score) * (1 + log(1 + n_distinct_sources))

The cross-source bonus rewards an underlying mentioned by multiple
distinct signals (earnings_calendar + cboe_skew + iv_capture) over one
mentioned only by a single source — exactly the right bias for a
wheel candidate gate.

Earnings-window flag: when an ``earnings_calendar`` event for the
underlying lies inside the wheel's DTE lookahead, the aggregator sets
``earnings_in_dte_window=True`` and writes ``days_to_earnings``. Yusuf's
neutral brief reads those fields directly.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.sources._base import (
    DEFAULT_DECAY_HOURS,
    DEFAULT_SOURCE_WEIGHT,
    OPTIONS_DECAY_HOURS,
    OPTIONS_SOURCE_WEIGHTS,
)
from trading_bot.pipelines.options.state_db import (
    IntelCandidateOptions,
    IntelEventOptions,
)

logger = logging.getLogger(__name__)


# Aggregation lookback. Events older than this don't contribute to score.
AGG_WINDOW_HOURS = 168  # 7d — wheel cadence is slower than crypto's 72h


@dataclass
class _PerUnderlyingAccumulator:
    score: float = 0.0
    n_mentions: int = 0
    sources: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sentiment_sum: float = 0.0
    sentiment_n: int = 0
    first_seen: Optional[dt.datetime] = None
    last_seen: Optional[dt.datetime] = None
    top_event_score: float = -1.0
    top_event_headline: str = ""
    # Options-native context fields surfaced from specific sources.
    earnings_at: Optional[dt.datetime] = None
    cboe_skew: Optional[float] = None


def event_score(
    *,
    source: str,
    sentiment: Optional[float],
    age_hours: float,
    weight_override: Optional[float] = None,
) -> float:
    """Pure scoring function — no DB access. Exposed for unit tests + tuning."""
    weight = (
        float(weight_override) if weight_override is not None
        else OPTIONS_SOURCE_WEIGHTS.get(source, DEFAULT_SOURCE_WEIGHT)
    )
    decay = OPTIONS_DECAY_HOURS.get(source, DEFAULT_DECAY_HOURS)
    decay_factor = math.exp(-max(0.0, age_hours) / decay)
    s = float(sentiment) if sentiment is not None else 0.0
    sentiment_factor = 1.0 + abs(s) * 0.5
    return weight * decay_factor * sentiment_factor


def underlying_score(*, sum_event_score: float, n_distinct_sources: int) -> float:
    """Cross-source bonus folded in. Pure — testable in isolation."""
    bonus = 1.0 + math.log(1.0 + max(0, n_distinct_sources))
    return sum_event_score * bonus


def roll_up(
    engine: Any,
    *,
    now: Optional[dt.datetime] = None,
    window_hours: int = AGG_WINDOW_HOURS,
    earnings_dte_window_days: int = 45,
) -> Dict[str, Any]:
    """Read recent options intel events, compute per-underlying scores,
    upsert into ``intel_candidates_options``. Idempotent — re-running on
    the same events produces the same rows.

    ``earnings_dte_window_days`` controls the boundary for the
    ``earnings_in_dte_window`` flag: any earnings_calendar event whose
    ``event_at`` is within that lookahead from ``now`` flips the flag
    on the underlying's candidate row.

    Returns a small summary dict the caller (intel ingestor / scanner)
    logs.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=window_hours)

    with Session(engine) as session:
        events = (
            session.query(IntelEventOptions)
            .filter(IntelEventOptions.ingested_at >= cutoff)
            .all()
        )

    by_underlying: Dict[str, _PerUnderlyingAccumulator] = {}
    # Track the latest CBOE SKEW reading separately — it's an index-level
    # signal that propagates to every candidate, not a per-underlying one.
    latest_skew: Optional[float] = None
    for e in events:
        et = e.event_at or e.ingested_at
        if et is None:
            continue
        if et.tzinfo is None:
            et = et.replace(tzinfo=dt.timezone.utc)
        age_h = max(0.0, (now - et).total_seconds() / 3600.0)
        es = event_score(source=e.source, sentiment=e.sentiment, age_hours=age_h)

        # CBOE SKEW is index-level: don't roll into the per-underlying
        # accumulator (would force every wheel name to share its row).
        # Instead capture the latest reading and apply it to all
        # underlyings on write below.
        if e.source == "cboe_skew":
            if e.raw_score is not None:
                latest_skew = float(e.raw_score)
            continue

        acc = by_underlying.setdefault(e.underlying, _PerUnderlyingAccumulator())
        acc.score += es
        acc.n_mentions += 1
        acc.sources[e.source] += 1
        if e.sentiment is not None:
            acc.sentiment_sum += float(e.sentiment)
            acc.sentiment_n += 1
        if acc.first_seen is None or et < acc.first_seen:
            acc.first_seen = et
        if acc.last_seen is None or et > acc.last_seen:
            acc.last_seen = et
        if es > acc.top_event_score:
            acc.top_event_score = es
            acc.top_event_headline = (e.headline or "")[:240]
        # Surface the earnings event time so the candidate row can flag
        # earnings-in-DTE-window.
        if e.source == "earnings_calendar" and e.event_at is not None:
            ev_at = e.event_at
            if ev_at.tzinfo is None:
                ev_at = ev_at.replace(tzinfo=dt.timezone.utc)
            if acc.earnings_at is None or ev_at < acc.earnings_at:
                acc.earnings_at = ev_at

    # Apply cross-source bonus.
    for acc in by_underlying.values():
        acc.score = underlying_score(
            sum_event_score=acc.score,
            n_distinct_sources=len(acc.sources),
        )

    # Earnings-in-DTE window evaluator.
    earnings_cutoff = now + dt.timedelta(days=earnings_dte_window_days)

    n_upserted = 0
    with Session(engine) as session:
        for underlying, acc in by_underlying.items():
            sentiment_avg = (
                acc.sentiment_sum / acc.sentiment_n if acc.sentiment_n > 0 else None
            )
            sources_payload = json.dumps(dict(acc.sources), sort_keys=True)
            earnings_in_window = False
            days_to_earn: Optional[int] = None
            if acc.earnings_at is not None:
                if now <= acc.earnings_at <= earnings_cutoff:
                    earnings_in_window = True
                    days_to_earn = (acc.earnings_at - now).days

            stmt = sqlite_insert(IntelCandidateOptions).values(
                underlying=underlying,
                score=round(acc.score, 4),
                n_mentions=acc.n_mentions,
                n_sources=len(acc.sources),
                first_seen=acc.first_seen or now,
                last_seen=acc.last_seen or now,
                top_reason=acc.top_event_headline,
                sources_json=sources_payload,
                sentiment_avg=sentiment_avg,
                rolled_up_at=now,
                iv_rank=None,  # filled by iv_capture join in a later phase
                earnings_in_dte_window=earnings_in_window,
                days_to_earnings=days_to_earn,
                cboe_skew=latest_skew,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["underlying"],
                set_={
                    "score": stmt.excluded.score,
                    "n_mentions": stmt.excluded.n_mentions,
                    "n_sources": stmt.excluded.n_sources,
                    "last_seen": stmt.excluded.last_seen,
                    "top_reason": stmt.excluded.top_reason,
                    "sources_json": stmt.excluded.sources_json,
                    "sentiment_avg": stmt.excluded.sentiment_avg,
                    "rolled_up_at": stmt.excluded.rolled_up_at,
                    "earnings_in_dte_window": stmt.excluded.earnings_in_dte_window,
                    "days_to_earnings": stmt.excluded.days_to_earnings,
                    "cboe_skew": stmt.excluded.cboe_skew,
                },
            )
            session.execute(stmt)
            n_upserted += 1
        session.commit()

    return {
        "events_considered": len(events),
        "candidates_upserted": n_upserted,
        "window_hours": window_hours,
        "rolled_up_at": now.isoformat(),
        "latest_cboe_skew": latest_skew,
    }
