"""Aggregator — rolls intel_events into the materialized intel_candidates table.

Scoring formula (deterministic, transparent, tunable):

  per_event_score = source_weight[source]
                  * exp(-age_hours / decay_hours[source])
                  * (1 + abs(sentiment) * 0.5)

  per_symbol_score = sum(per_event_score) * (1 + log(1 + n_distinct_sources))

The cross-source bonus penalizes single-source pumping: a symbol mentioned
12 times in WSB but nowhere else scores comparably to one mentioned in 3
news outlets and 1 SEC filing. That's the right bias for a system that
trades on internet signal.

Source weights and decay constants are module-level constants here for
auditability — the threshold tuner can override them in a future phase
once we have closed-trade outcomes joined back to intel events.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import desc
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from trading_bot.state_db import IntelCandidate, IntelEvent


# Source weights: how much one mention from this source counts in the
# aggregate. SEC filings beat news beats social — directionally what an
# experienced trader does already. Numbers are first-principles guesses
# and should be tuned once we have realized outcomes per source.
SOURCE_WEIGHTS: dict[str, float] = {
    "sec_8k":             5.0,
    "sec_form4":          2.5,
    "alpaca_news":        2.0,
    "finnhub_news":       2.0,
    "gdelt":              1.0,
    "apewisdom":          2.0,
    "vip_tweet":          3.0,
    "macro_shock":        4.0,   # cross-cutting market events
    "earnings_calendar":  1.5,
    # Phase 6 — crypto-specific sources
    "apewisdom_crypto":   2.0,   # r/CryptoCurrency mentions
    "coindesk_rss":       2.5,   # editorial, low-noise crypto news
    "cointelegraph_rss":  2.0,   # editorial crypto news
    "cryptopanic":        2.5,   # 100+ source aggregator + community vote sentiment
    # Phase A — diversified stock-news sources (defense-in-depth)
    "polygon_news":       2.5,   # Polygon News with native per-ticker sentiment
    "yahoo_rss":          1.5,   # broad publisher set, syndicated
    "googlenews_rss":     1.0,   # noisier fallback
    "reddit_news":        1.5,   # r/stocks, r/investing, r/options (broader than WSB)
    "newsapi":            1.8,   # 80k+ source aggregator (NewsAPI.org free tier)
}


# Half-life-ish: how long an event stays "current". A SEC 8-K is still
# relevant 2 days later; a tweet is yesterday's news after 6 hours.
DECAY_HOURS: dict[str, float] = {
    "sec_8k":             48.0,
    "sec_form4":          36.0,
    "alpaca_news":        12.0,
    "finnhub_news":       12.0,
    "gdelt":               8.0,
    "apewisdom":          24.0,
    "vip_tweet":           6.0,
    "macro_shock":        24.0,
    "earnings_calendar":  72.0,
    # Phase 6 — crypto-specific sources
    "apewisdom_crypto":   24.0,
    "coindesk_rss":       12.0,
    "cointelegraph_rss":  12.0,
    "cryptopanic":         8.0,
    # Phase A — diversified stock-news sources
    "polygon_news":       12.0,  # editorial-grade, similar to alpaca/finnhub
    "yahoo_rss":          12.0,  # broad publisher RSS, similar editorial half-life
    "googlenews_rss":      8.0,  # noisier; faster decay
    "reddit_news":        18.0,  # community discussion threads stay relevant longer
    "newsapi":            12.0,  # broad news aggregator, editorial half-life
}


# Default fallbacks for unknown sources (defensive — every source the
# aggregator sees should be in the maps above; this ensures forward
# compatibility if a new source ships ahead of weight tuning).
DEFAULT_SOURCE_WEIGHT = 1.0
DEFAULT_DECAY_HOURS = 12.0


# Aggregation lookback: events older than this don't contribute to score.
# Protects against a multi-day-old SEC filing dragging a candidate above
# the floor when nothing else has happened.
AGG_WINDOW_HOURS = 72


@dataclass
class _PerSymbolAccumulator:
    """In-memory accumulator while walking events for one (symbol, asset_class)."""
    score: float = 0.0
    n_mentions: int = 0
    sources: dict[str, int] = None  # type: ignore[assignment]
    sentiment_sum: float = 0.0
    sentiment_n: int = 0
    first_seen: dt.datetime | None = None
    last_seen: dt.datetime | None = None
    top_event_score: float = -1.0
    top_event_headline: str = ""

    def __post_init__(self):
        if self.sources is None:
            self.sources = defaultdict(int)


def event_score(
    *, source: str, sentiment: float | None, age_hours: float,
    weight_override: float | None = None,
) -> float:
    """Pure scoring function — no DB. Exposed for unit tests + tuning.

    Phase E: ``weight_override`` lets the caller inject a tuned weight
    (typically from ``adaptive_thresholds.lookup_source_weight``) without
    coupling the pure score function to a DB. When None, falls back to
    the static SOURCE_WEIGHTS map.
    """
    if weight_override is not None:
        weight = float(weight_override)
    else:
        weight = SOURCE_WEIGHTS.get(source, DEFAULT_SOURCE_WEIGHT)
    decay = DECAY_HOURS.get(source, DEFAULT_DECAY_HOURS)
    decay_factor = math.exp(-max(0.0, age_hours) / decay)
    s = float(sentiment) if sentiment is not None else 0.0
    sentiment_factor = 1.0 + abs(s) * 0.5
    return weight * decay_factor * sentiment_factor


def symbol_score(
    *, sum_event_score: float, n_distinct_sources: int
) -> float:
    """Cross-source bonus folded in. Pure — testable."""
    bonus = 1.0 + math.log(1.0 + max(0, n_distinct_sources))
    return sum_event_score * bonus


def roll_up(
    engine,
    *,
    now: dt.datetime | None = None,
    window_hours: int = AGG_WINDOW_HOURS,
) -> dict:
    """Walk recent events, materialize per-(symbol, asset_class) rows in
    ``intel_candidates``. Idempotent — re-running on the same events
    produces the same rows.

    Strategy:
      * Read all events newer than ``now - window_hours``.
      * Bucket by (symbol, asset_class).
      * Compute score, sentiment_avg, n_mentions, sources, top_reason.
      * UPSERT into intel_candidates (one row per key).
      * Decay-out: candidates with no fresh events get score=0 but stay
        in the table for audit. The pool reader filters by score / age.

    Returns a small summary dict for the caller (role) to log.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=window_hours)
    with Session(engine) as session:
        events = (
            session.query(IntelEvent)
            .filter(IntelEvent.ingested_at >= cutoff)
            .all()
        )

    # Phase E — resolve the tuned weight per unique source once (one
    # SQL query per source, then cached for all events in this rollup).
    weight_cache: dict[str, float] = {}
    try:
        from trading_bot.adaptive_thresholds import lookup_source_weight
        for e in events:
            if e.source not in weight_cache:
                weight_cache[e.source] = lookup_source_weight(engine, e.source)
    except Exception:
        # Fail-soft — if adaptive_thresholds blew up, fall back to static
        # weights via event_score's default path.
        weight_cache = {}

    by_key: dict[tuple[str, str], _PerSymbolAccumulator] = {}
    for e in events:
        # Effective event time: prefer event_at, fall back to ingested_at.
        et = e.event_at or e.ingested_at
        if et.tzinfo is None:
            et = et.replace(tzinfo=dt.timezone.utc)
        age_h = max(0.0, (now - et).total_seconds() / 3600.0)
        es = event_score(
            source=e.source, sentiment=e.sentiment, age_hours=age_h,
            weight_override=weight_cache.get(e.source),
        )

        key = (e.symbol, e.asset_class)
        acc = by_key.setdefault(key, _PerSymbolAccumulator())
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

    # Apply cross-source bonus.
    for acc in by_key.values():
        acc.score = symbol_score(
            sum_event_score=acc.score, n_distinct_sources=len(acc.sources),
        )

    # Phase F — pre-compute adversarial flags per (symbol, asset_class).
    # We need to re-fetch the events per key to pass them to compute_flags;
    # cheap because we already have them in memory partitioned by key.
    events_by_key: dict[tuple[str, str], list] = {}
    for e in events:
        events_by_key.setdefault((e.symbol, e.asset_class), []).append(e)

    flags_by_key: dict[tuple[str, str], object] = {}
    try:
        from trading_bot.intel.adversarial import compute_flags
        for key, evs in events_by_key.items():
            symbol, asset_class = key
            sources_count = dict(by_key[key].sources) if key in by_key else {}
            try:
                flags_by_key[key] = compute_flags(
                    engine, symbol=symbol, events=evs,
                    sources_count=sources_count, now=now,
                )
            except Exception as ex:  # noqa: BLE001
                # One symbol's failure mustn't break the rollup.
                pass
    except Exception:
        flags_by_key = {}

    n_upserted = 0
    with Session(engine) as session:
        for (symbol, asset_class), acc in by_key.items():
            sentiment_avg = (
                acc.sentiment_sum / acc.sentiment_n
                if acc.sentiment_n > 0 else None
            )
            sources_payload = json.dumps(dict(acc.sources), sort_keys=True)
            flags = flags_by_key.get((symbol, asset_class))
            dedup_payload = json.dumps(
                list(getattr(flags, "dedup_url_hashes", ())) if flags else []
            )
            stmt = sqlite_insert(IntelCandidate).values(
                symbol=symbol,
                asset_class=asset_class,
                score=round(acc.score, 4),
                n_mentions=acc.n_mentions,
                n_sources=len(acc.sources),
                first_seen=acc.first_seen or now,
                last_seen=acc.last_seen or now,
                top_reason=acc.top_event_headline,
                sources_json=sources_payload,
                sentiment_avg=sentiment_avg,
                rolled_up_at=now,
                dedup_url_hashes_json=dedup_payload,
                suspicious_spike=bool(getattr(flags, "suspicious_spike", False)),
                coordinated=bool(getattr(flags, "coordinated", False)),
                pump_signature=bool(getattr(flags, "pump_signature", False)),
            )
            # Upsert on (symbol, asset_class) — keep the original first_seen
            # but bump everything else.
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "asset_class"],
                set_={
                    "score": stmt.excluded.score,
                    "n_mentions": stmt.excluded.n_mentions,
                    "n_sources": stmt.excluded.n_sources,
                    "last_seen": stmt.excluded.last_seen,
                    "top_reason": stmt.excluded.top_reason,
                    "sources_json": stmt.excluded.sources_json,
                    "sentiment_avg": stmt.excluded.sentiment_avg,
                    "rolled_up_at": stmt.excluded.rolled_up_at,
                    "dedup_url_hashes_json": stmt.excluded.dedup_url_hashes_json,
                    "suspicious_spike": stmt.excluded.suspicious_spike,
                    "coordinated": stmt.excluded.coordinated,
                    "pump_signature": stmt.excluded.pump_signature,
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
    }


def write_event(
    engine,
    *,
    symbol: str,
    asset_class: str,
    source: str,
    headline: str = "",
    url: str = "",
    sentiment: float | None = None,
    raw_score: float | None = None,
    event_at: dt.datetime | None = None,
    event_hash: str | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Insert a single intel event. Idempotent via (symbol, source,
    event_hash) unique index. Returns True on insert, False on dedup.

    The unique index does the dedup work — we just catch IntegrityError
    and treat it as a no-op. ``event_hash`` defaults to a hash of source+url
    when not supplied.
    """
    import hashlib
    from sqlalchemy.exc import IntegrityError
    now = now or dt.datetime.now(dt.timezone.utc)
    if event_hash is None:
        h = hashlib.sha1()
        h.update(source.encode())
        h.update((url or headline or "").encode())
        event_hash = h.hexdigest()
    row = IntelEvent(
        symbol=symbol.upper(),
        asset_class=asset_class,
        source=source,
        headline=(headline or "")[:1000],
        url=(url or "")[:1000],
        sentiment=sentiment,
        raw_score=raw_score,
        event_at=event_at,
        ingested_at=now,
        event_hash=event_hash,
    )
    try:
        with Session(engine) as session:
            session.add(row)
            session.commit()
            return True
    except IntegrityError:
        return False
