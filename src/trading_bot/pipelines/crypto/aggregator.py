"""Crypto aggregator — rolls intel_events_crypto into intel_candidates_crypto.

Per Option 2 (independent pipelines), this is a per-pipeline copy of
the stocks aggregator's scoring + roll-up logic. Reads only from the
crypto-owned tables; writes only to ``intel_candidates_crypto``.

Scoring formula (same shape as stocks for portability — divergences
will be tuned separately by Phase 1E adaptive thresholds):

  per_event_score = source_weight[source]
                  * exp(-age_hours / decay_hours[source])
                  * (1 + abs(sentiment) * 0.5)

  per_symbol_score = sum(per_event_score) * (1 + log(1 + n_distinct_sources))

The cross-source bonus penalises single-source pumping — a token
mentioned 12× in WSB but nowhere else scores comparably to one
mentioned in 3 editorial outlets + 1 governance proposal. That's the
right bias for a system that trades on internet signal.

Phase 1F (adversarial) flags are computed once per (symbol) and
written into ``intel_candidates_crypto`` so the scout/hold debates can
read them later without recomputing. The adversarial module itself
ships in Phase 1F; until then ``compute_flags`` returns empty defaults.
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

from trading_bot.pipelines.crypto.sources._base import (
    CRYPTO_DECAY_HOURS,
    CRYPTO_SOURCE_WEIGHTS,
    DEFAULT_DECAY_HOURS,
    DEFAULT_SOURCE_WEIGHT,
)
from trading_bot.pipelines.crypto.state_db import (
    IntelCandidateCrypto,
    IntelEventCrypto,
)

logger = logging.getLogger(__name__)


# Aggregation lookback. Events older than this don't contribute to score.
# Protects against a multi-day-old event dragging a candidate above the
# floor when nothing else has happened.
AGG_WINDOW_HOURS = 72


@dataclass
class _PerSymbolAccumulator:
    score: float = 0.0
    n_mentions: int = 0
    sources: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sentiment_sum: float = 0.0
    sentiment_n: int = 0
    first_seen: Optional[dt.datetime] = None
    last_seen: Optional[dt.datetime] = None
    top_event_score: float = -1.0
    top_event_headline: str = ""


def event_score(
    *,
    source: str,
    sentiment: Optional[float],
    age_hours: float,
    weight_override: Optional[float] = None,
) -> float:
    """Pure scoring function — no DB access. Exposed for unit tests + tuning."""
    if weight_override is not None:
        weight = float(weight_override)
    else:
        weight = CRYPTO_SOURCE_WEIGHTS.get(source, DEFAULT_SOURCE_WEIGHT)
    decay = CRYPTO_DECAY_HOURS.get(source, DEFAULT_DECAY_HOURS)
    decay_factor = math.exp(-max(0.0, age_hours) / decay)
    s = float(sentiment) if sentiment is not None else 0.0
    sentiment_factor = 1.0 + abs(s) * 0.5
    return weight * decay_factor * sentiment_factor


def symbol_score(*, sum_event_score: float, n_distinct_sources: int) -> float:
    """Cross-source bonus folded in. Pure — testable in isolation."""
    bonus = 1.0 + math.log(1.0 + max(0, n_distinct_sources))
    return sum_event_score * bonus


def roll_up(
    engine: Any,
    *,
    now: Optional[dt.datetime] = None,
    window_hours: int = AGG_WINDOW_HOURS,
    adversarial_context_lookup: Optional[Any] = None,
) -> Dict[str, Any]:
    """Read recent crypto events, compute per-symbol scores, upsert into
    ``intel_candidates_crypto``. Idempotent — re-running on the same
    events produces the same rows.

    ``adversarial_context_lookup`` (Phase 1F): optional callable
    ``(symbol) -> AdversarialContext``. When supplied, the aggregator
    runs ``adversarial.compute_flags()`` per candidate and writes the
    resulting flags + score multiplier into ``intel_candidates_crypto``.
    When None, all flags default to False and the score multiplier
    is 1.0 (Phase 1A behaviour preserved).

    Returns a small summary dict the caller (intel ingestor role) logs.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=window_hours)

    with Session(engine) as session:
        events = (
            session.query(IntelEventCrypto)
            .filter(IntelEventCrypto.ingested_at >= cutoff)
            .all()
        )

    by_symbol: Dict[str, _PerSymbolAccumulator] = {}
    for e in events:
        et = e.event_at or e.ingested_at
        if et is None:
            continue
        if et.tzinfo is None:
            et = et.replace(tzinfo=dt.timezone.utc)
        age_h = max(0.0, (now - et).total_seconds() / 3600.0)
        es = event_score(source=e.source, sentiment=e.sentiment, age_hours=age_h)

        acc = by_symbol.setdefault(e.symbol, _PerSymbolAccumulator())
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
    for acc in by_symbol.values():
        acc.score = symbol_score(
            sum_event_score=acc.score,
            n_distinct_sources=len(acc.sources),
        )

    # Phase 1F — compute crypto adversarial flags per symbol if a context
    # lookup is provided. Fail-soft: per-symbol exception during flag
    # computation defaults to all-False flags + 1.0 multiplier so a
    # noisy upstream signal doesn't poison the entire roll-up.
    adv_flags_by_symbol: Dict[str, Any] = {}
    if adversarial_context_lookup is not None:
        try:
            from trading_bot.pipelines.crypto.adversarial import compute_flags
            for symbol in by_symbol:
                try:
                    ctx = adversarial_context_lookup(symbol)
                    if ctx is not None:
                        adv_flags_by_symbol[symbol] = compute_flags(ctx)
                except Exception as ex:  # noqa: BLE001
                    logger.warning("adversarial flag computation failed for %s: %s", symbol, ex)
        except Exception as ex:  # noqa: BLE001 — module-level import / wiring issue
            logger.warning("adversarial module wiring failed: %s", ex)
            adv_flags_by_symbol = {}

    # Apply score multipliers from adversarial flags (honeypot → 0.0 etc.)
    for symbol, acc in by_symbol.items():
        flags = adv_flags_by_symbol.get(symbol)
        if flags is not None and flags.score_multiplier != 1.0:
            acc.score *= flags.score_multiplier

    n_upserted = 0
    with Session(engine) as session:
        for symbol, acc in by_symbol.items():
            sentiment_avg = (
                acc.sentiment_sum / acc.sentiment_n if acc.sentiment_n > 0 else None
            )
            sources_payload = json.dumps(dict(acc.sources), sort_keys=True)
            flags = adv_flags_by_symbol.get(symbol)
            stmt = sqlite_insert(IntelCandidateCrypto).values(
                symbol=symbol,
                score=round(acc.score, 4),
                n_mentions=acc.n_mentions,
                n_sources=len(acc.sources),
                first_seen=acc.first_seen or now,
                last_seen=acc.last_seen or now,
                top_reason=acc.top_event_headline,
                sources_json=sources_payload,
                sentiment_avg=sentiment_avg,
                rolled_up_at=now,
                dedup_url_hashes_json="[]",
                suspicious_spike=False,
                coordinated=False,
                pump_signature=bool(getattr(flags, "pump_signature", False)),
                cold_start_token=bool(getattr(flags, "cold_start_token", False)),
                whale_concentration=bool(getattr(flags, "whale_concentration", False)),
                honeypot_detected=bool(getattr(flags, "honeypot_detected", False)),
                sybil_coordinated=bool(getattr(flags, "sybil_coordinated", False)),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol"],
                set_={
                    "score": stmt.excluded.score,
                    "n_mentions": stmt.excluded.n_mentions,
                    "n_sources": stmt.excluded.n_sources,
                    "last_seen": stmt.excluded.last_seen,
                    "top_reason": stmt.excluded.top_reason,
                    "sources_json": stmt.excluded.sources_json,
                    "sentiment_avg": stmt.excluded.sentiment_avg,
                    "rolled_up_at": stmt.excluded.rolled_up_at,
                    "pump_signature": stmt.excluded.pump_signature,
                    "cold_start_token": stmt.excluded.cold_start_token,
                    "whale_concentration": stmt.excluded.whale_concentration,
                    "honeypot_detected": stmt.excluded.honeypot_detected,
                    "sybil_coordinated": stmt.excluded.sybil_coordinated,
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
