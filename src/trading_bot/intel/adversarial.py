"""Phase F — Adversarial intel defense.

Pure functions that detect manipulation signatures BEFORE the aggregator
counts cross-source confirmation. The aggregator calls these on each
candidate's events and writes the resulting flags to the IntelCandidate
row; the scout-debate brief surfaces them so the judge can bias toward
dismiss when adversarial signals fire.

Detections:

  1. URL hash dedup — articles republished verbatim across sources don't
     count as cross-source confirmation. Returns the unique-URL count.

  2. Velocity anomaly — current-tick mention spike vs trailing 30-day
     median. >10x spike with no prior baseline = cold-start attack.

  3. Source coordination — 3+ near-identical headlines within 5 min for
     a symbol with no prior 24h mentions = coordinated promotion.

  4. Pump signature — small-cap (market cap unknown but inferable from
     symbol type) + heavy WSB/Reddit + neutral-to-absent news =
     classic pump.

All functions are pure-ish (engine for the SQL queries; no side effects
besides the SELECT). The aggregator wires them into the rollup loop.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from trading_bot.state_db import IntelEvent


log = logging.getLogger(__name__)


DEFAULT_VELOCITY_SPIKE_THRESHOLD = 10.0
DEFAULT_COLD_START_LOOKBACK_DAYS = 30
DEFAULT_COORDINATION_WINDOW_MINUTES = 5
DEFAULT_PUMP_SOCIAL_FLOOR = 50          # apewisdom + reddit_news combined
DEFAULT_PUMP_NEWS_CEILING = 1           # news mentions must be ≤ this
PUMP_SMALL_CAP_LIKELY_PATTERNS = (
    re.compile(r"^[A-Z]{4,5}$"),         # 4-5 char tickers (often small-caps)
)


@dataclass(frozen=True)
class AdversarialFlags:
    suspicious_spike: bool = False
    coordinated: bool = False
    pump_signature: bool = False
    dedup_url_hashes: tuple[str, ...] = ()
    distinct_url_count: int = 0


# ---------------------------------------------------------------------------
# URL hash dedup
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    """Strip query strings + fragments + scheme — articles syndicated
    across sources keep the path stable but mutate the query."""
    if not url:
        return ""
    s = url.strip().lower()
    # Strip scheme
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Strip query and fragment
    for sep in ("?", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    # Strip trailing slash
    return s.rstrip("/")


def url_hash(url: str, headline: str = "") -> str:
    """Hash on (normalized URL + first 100 chars of headline). Empty URL
    falls back to headline alone — RSS items sometimes lack a stable URL.
    """
    norm = _normalize_url(url)
    h = norm if norm else (headline or "").strip().lower()[:100]
    if not h:
        return ""
    return hashlib.sha1(h.encode("utf-8")).hexdigest()


def distinct_urls(events: list[IntelEvent]) -> tuple[set[str], int]:
    """Return the set of unique URL hashes + the distinct count.

    Used by the cross-source bonus computation: an article reprinted by
    4 sources should count once, not four times.
    """
    hashes: set[str] = set()
    for e in events:
        h = url_hash(e.url or "", e.headline or "")
        if h:
            hashes.add(h)
    return hashes, len(hashes)


# ---------------------------------------------------------------------------
# Velocity / cold-start spike
# ---------------------------------------------------------------------------


def detect_suspicious_spike(
    engine,
    *,
    symbol: str,
    current_count: int,
    spike_threshold: float = DEFAULT_VELOCITY_SPIKE_THRESHOLD,
    lookback_days: int = DEFAULT_COLD_START_LOOKBACK_DAYS,
    now: dt.datetime | None = None,
) -> bool:
    """True when current_count is more than ``spike_threshold`` × the
    trailing per-day median count of mentions for this symbol.

    For a cold-start symbol (no prior mentions), the median is 0; we
    fire the spike when current_count >= spike_threshold (treating
    "0 → 10+" as the canonical cold-start attack).
    """
    if current_count <= 0:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=lookback_days)
    window_start = now - dt.timedelta(hours=2)
    with Session(engine) as session:
        prior = (
            session.query(IntelEvent)
            .filter(IntelEvent.symbol == symbol)
            .filter(IntelEvent.ingested_at >= cutoff)
            .filter(IntelEvent.ingested_at < window_start)
            .all()
        )
    if not prior:
        # Cold start: any spike at or above threshold fires
        return current_count >= int(spike_threshold)
    # Compute per-day count for the prior window
    by_day: dict[dt.date, int] = defaultdict(int)
    for e in prior:
        d = (e.ingested_at or now).date()
        by_day[d] += 1
    counts = sorted(by_day.values())
    median = counts[len(counts) // 2] if counts else 0
    if median <= 0:
        return current_count >= int(spike_threshold)
    return (current_count / median) >= spike_threshold


# ---------------------------------------------------------------------------
# Source coordination
# ---------------------------------------------------------------------------


def _normalize_headline(headline: str) -> str:
    """Lowercase + strip punctuation/whitespace + take first 60 chars."""
    if not headline:
        return ""
    s = re.sub(r"[^\w\s]", "", headline.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60]


def detect_coordinated(
    engine,
    *,
    symbol: str,
    window_minutes: int = DEFAULT_COORDINATION_WINDOW_MINUTES,
    cold_start_lookback_hours: int = 24,
    min_distinct_sources: int = 3,
    now: dt.datetime | None = None,
) -> bool:
    """True when 3+ near-identical headlines appear from distinct sources
    within ``window_minutes`` for a symbol that had zero mentions in the
    prior ``cold_start_lookback_hours``.

    Real news propagates over hours; coordinated promotion compresses to
    minutes from a cold base.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(minutes=window_minutes)
    cold_start_cutoff = now - dt.timedelta(hours=cold_start_lookback_hours)

    with Session(engine) as session:
        recent = (
            session.query(IntelEvent)
            .filter(IntelEvent.symbol == symbol)
            .filter(IntelEvent.ingested_at >= window_start)
            .all()
        )
        prior = (
            session.query(IntelEvent)
            .filter(IntelEvent.symbol == symbol)
            .filter(IntelEvent.ingested_at >= cold_start_cutoff)
            .filter(IntelEvent.ingested_at < window_start)
            .count()
        )
    if prior > 0:
        # Not cold-start
        return False
    if len(recent) < min_distinct_sources:
        return False
    # Group by normalised headline; need to see ≥3 distinct sources sharing
    # one normalised headline (the same story repeated)
    by_headline: dict[str, set[str]] = defaultdict(set)
    for e in recent:
        nh = _normalize_headline(e.headline or "")
        if not nh:
            continue
        by_headline[nh].add(e.source)
    return any(len(srcs) >= min_distinct_sources for srcs in by_headline.values())


# ---------------------------------------------------------------------------
# Pump signature
# ---------------------------------------------------------------------------


_SOCIAL_SOURCES = ("apewisdom", "reddit_news", "vip_tweet")
_NEWS_SOURCES = (
    "polygon_news", "alpaca_news", "newsapi", "yahoo_rss",
    "googlenews_rss", "sec_8k",
)


def _likely_small_cap(symbol: str) -> bool:
    """Heuristic: 4-5 char ticker (small/mid-cap range) without a slash
    (slash → crypto pair). Conservative — false negatives are fine here
    (we miss flagging some pumps); false positives bias against valid
    small-cap catalysts which is the wrong tradeoff."""
    if not symbol or "/" in symbol:
        return False
    for pat in PUMP_SMALL_CAP_LIKELY_PATTERNS:
        if pat.match(symbol):
            return True
    return False


def detect_pump_signature(
    *,
    symbol: str,
    sources_count: dict[str, int],
    social_floor: int = DEFAULT_PUMP_SOCIAL_FLOOR,
    news_ceiling: int = DEFAULT_PUMP_NEWS_CEILING,
) -> bool:
    """Pure: heavy social mentions (apewisdom + reddit + vip_tweets) +
    neutral-to-absent news + plausibly small-cap symbol = pump signature.
    """
    social_total = sum(sources_count.get(s, 0) for s in _SOCIAL_SOURCES)
    news_total = sum(sources_count.get(s, 0) for s in _NEWS_SOURCES)
    if social_total < social_floor:
        return False
    if news_total > news_ceiling:
        return False
    return _likely_small_cap(symbol)


# ---------------------------------------------------------------------------
# Composite — called from the aggregator
# ---------------------------------------------------------------------------


def compute_flags(
    engine,
    *,
    symbol: str,
    events: list[IntelEvent],
    sources_count: dict[str, int] | None = None,
    now: dt.datetime | None = None,
) -> AdversarialFlags:
    """Run all four detectors and return the composite AdversarialFlags
    for one (symbol, asset_class) accumulator.

    Sequential: each detector runs one at a time. SQL queries inside
    ``detect_suspicious_spike`` and ``detect_coordinated`` are short-lived.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    sources_count = sources_count or {}

    hashes, distinct = distinct_urls(events)

    spike = False
    try:
        spike = detect_suspicious_spike(
            engine, symbol=symbol, current_count=len(events), now=now,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("adversarial.detect_suspicious_spike failed for %s: %s", symbol, e)

    coordinated = False
    try:
        coordinated = detect_coordinated(engine, symbol=symbol, now=now)
    except Exception as e:  # noqa: BLE001
        log.warning("adversarial.detect_coordinated failed for %s: %s", symbol, e)

    pump = detect_pump_signature(symbol=symbol, sources_count=sources_count)

    return AdversarialFlags(
        suspicious_spike=spike,
        coordinated=coordinated,
        pump_signature=pump,
        dedup_url_hashes=tuple(sorted(hashes)),
        distinct_url_count=distinct,
    )
