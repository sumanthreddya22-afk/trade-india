"""Nightly wheel-universe builder.

Discovers wheel-eligible symbols from Alpaca's optionable universe and
Finnhub quality data; writes results to `wheel_universe_cache`. The wheel
runner reads from this cache — no per-scan Finnhub calls, no hand-curated
YAML as the source of truth.

Filters (all must hold for an equity to be eligible):
  * Listed in Alpaca's optionable us_equities set
  * Market cap ≥ $10B (Finnhub /stock/profile2). ETFs auto-pass since
    Finnhub doesn't surface market cap for funds.
  * Listed ≥ 3 years (Finnhub IPO date)
  * Not in operator's wheel_blocklist.yaml

Operator overrides:
  * `wheel_allowlist.yaml` — force-include even if Finnhub filters fail
  * `wheel_blocklist.yaml` — hard-exclude regardless of everything else

Cache TTL: 14 days. The runner skips symbols that were checked recently
to keep Finnhub quota free for new/expired entries.

Cadence: nightly @ 21:30 ET. First-ever run is ~100 min (Finnhub free
tier 60 calls/min × ~6,000 names). Subsequent nights only re-check
expired entries (~430/day on average → ~7 min)."""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.alerts import AlertEvent, queue_alert
from trading_bot.intelligence_finnhub import (
    CompanyProfile, FinnhubClient, FinnhubUnavailable,
)
from trading_bot.state_db import WheelUniverseCache


log = logging.getLogger(__name__)


# Quality thresholds. These match wheel_universe.py's static thresholds so
# behavior is consistent whichever path runs the filter.
_MIN_MARKET_CAP_MUSD = 10_000.0  # $10B in $-millions
_MIN_LISTING_YEARS = 3
_CACHE_TTL = dt.timedelta(days=14)
# Bucket C: a transient Finnhub outage must NOT purge a real $10B+ name from
# the universe for 14 days. Stamp the cached_at 13 days back when a Finnhub
# call fails so the next nightly build re-checks the symbol within ~24h.
_FINNHUB_RETRY_BACKOFF = dt.timedelta(days=13)
_FINNHUB_RATE_DELAY_S = 1.05  # ~57 calls/min, comfortably under 60/min ceiling

# Bucket C: alert thresholds. If the eligible-set count drops below this
# floor OR the per-build Finnhub-unavailable rate exceeds this fraction of
# the total processed names, fire a daemon_critical alert. Operator decides
# whether to investigate (real issue) or wait it out (transient outage).
_ELIGIBLE_FLOOR_ALERT = 50
_UNAVAILABLE_RATE_ALERT = 0.10  # 10% of processed names


# Sector-ETF detection: yfinance has no market cap for ETFs. We treat any
# symbol in this static set OR any symbol with `market_cap_musd is None`
# AND exchange is an ETF venue as an ETF (passes market-cap filter).
_KNOWN_ETF_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
    "XLK", "VGT", "XLF", "VFH", "XLE", "VDE", "XLV", "VHT",
    "XLI", "VIS", "XLY", "VCR", "XLP", "VDC", "XLU", "VPU",
    "XLB", "VAW", "XLRE", "VNQ", "XLC", "VOX",
    "TLT", "GLD", "SLV", "USO", "HYG", "LQD", "BND", "AGG",
    "EFA", "EEM", "VEA", "VWO", "VEU",
})
_ETF_EXCHANGE_HINTS = frozenset({"ARCA", "BATS", "NYSEARCA", "NYSE ARCA"})


@dataclass(frozen=True)
class BuilderDeps:
    engine: Engine
    optionable_set: set[str]
    finnhub: FinnhubClient
    blocklist: set[str]
    allowlist: set[str]
    today: dt.date
    # Per-Finnhub-call delay in seconds. Default 1.05s = ~57 calls/min,
    # comfortably under the 60/min free-tier ceiling. Tests pass 0.0.
    rate_delay_s: float = _FINNHUB_RATE_DELAY_S


def _is_etf(symbol: str, profile: CompanyProfile) -> bool:
    """ETF detection. Static-map hit OR (no market cap AND exchange is an
    ETF venue) → treat as ETF. Equities with a real market_cap_musd are
    never ETFs even if they trade on ARCA."""
    if symbol.upper() in _KNOWN_ETF_SYMBOLS:
        return True
    if profile.market_cap_musd is not None and profile.market_cap_musd > 0:
        return False
    return profile.exchange.upper() in _ETF_EXCHANGE_HINTS or profile.market_cap_musd is None


def _evaluate(symbol: str, profile: CompanyProfile, today: dt.date) -> tuple[bool, str]:
    """Apply quality filters. Returns (eligible, reason)."""
    if _is_etf(symbol, profile):
        return True, "etf"
    if (profile.market_cap_musd or 0.0) < _MIN_MARKET_CAP_MUSD:
        return False, f"market_cap (<${_MIN_MARKET_CAP_MUSD:.0f}M)"
    if profile.ipo_date is not None:
        years = (today - profile.ipo_date).days / 365.25
        if years < _MIN_LISTING_YEARS:
            return False, f"listing_age ({years:.1f}y < {_MIN_LISTING_YEARS}y)"
    return True, "ok"


def _upsert(
    session: Session, symbol: str, *, eligible: bool, reason: str,
    cached_at: dt.datetime,
) -> None:
    existing = session.query(WheelUniverseCache).filter_by(symbol=symbol).one_or_none()
    if existing is not None:
        existing.eligible = eligible
        existing.reason = reason
        existing.cached_at = cached_at
    else:
        session.add(WheelUniverseCache(
            symbol=symbol, eligible=eligible, reason=reason, cached_at=cached_at,
        ))


def run_universe_build(deps: BuilderDeps) -> dict[str, int]:
    """Build / refresh the wheel universe cache. Returns counts dict for
    {eligible, rejected, unavailable, cached_skip, fell_out}."""
    counts = {"eligible": 0, "rejected": 0, "unavailable": 0,
              "cached_skip": 0, "fell_out": 0}
    now = dt.datetime.now(dt.timezone.utc)
    cache_floor = now - _CACHE_TTL

    with Session(deps.engine) as s:
        existing = {r.symbol: r for r in s.query(WheelUniverseCache).all()}

    # 1) Mark fall-outs: previously cached as eligible, no longer optionable
    fell_out_symbols: list[str] = []
    for sym, row in existing.items():
        if sym not in deps.optionable_set and row.eligible:
            fell_out_symbols.append(sym)
    if fell_out_symbols:
        with Session(deps.engine) as s:
            for sym in fell_out_symbols:
                _upsert(s, sym, eligible=False, reason="no_longer_optionable",
                        cached_at=now)
            s.commit()
        counts["fell_out"] = len(fell_out_symbols)

    # 2) Iterate the live optionable set
    for symbol in sorted(deps.optionable_set):
        # Operator blocklist — hard exclude, no Finnhub call needed
        if symbol in deps.blocklist:
            with Session(deps.engine) as s:
                _upsert(s, symbol, eligible=False, reason="blocklist", cached_at=now)
                s.commit()
            counts["rejected"] += 1
            continue

        # Operator allowlist — force include, no Finnhub call needed
        if symbol in deps.allowlist:
            with Session(deps.engine) as s:
                _upsert(s, symbol, eligible=True, reason="allowlist", cached_at=now)
                s.commit()
            counts["eligible"] += 1
            continue

        # Cache TTL: skip if checked recently
        existing_row = existing.get(symbol)
        if existing_row is not None:
            cached_at = existing_row.cached_at
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=dt.timezone.utc)
            if cached_at >= cache_floor:
                counts["cached_skip"] += 1
                continue

        # Finnhub lookup — rate-limited
        try:
            profile = deps.finnhub.company_profile(symbol)
        except FinnhubUnavailable:
            # Bucket C: stamp cached_at well in the past so the next
            # nightly run re-checks this symbol — DON'T let a transient
            # Finnhub outage purge a good name for 14 days.
            retry_at = now - _FINNHUB_RETRY_BACKOFF
            with Session(deps.engine) as s:
                _upsert(s, symbol, eligible=False,
                        reason="finnhub_unavailable", cached_at=retry_at)
                s.commit()
            counts["unavailable"] += 1
            if deps.rate_delay_s > 0:
                time.sleep(deps.rate_delay_s)
            continue

        eligible, reason = _evaluate(symbol, profile, deps.today)
        with Session(deps.engine) as s:
            _upsert(s, symbol, eligible=eligible, reason=reason, cached_at=now)
            s.commit()
        if eligible:
            counts["eligible"] += 1
        else:
            counts["rejected"] += 1
        if deps.rate_delay_s > 0:
            time.sleep(deps.rate_delay_s)

    log.info("wheel_universe_build complete: %s", counts)
    _alert_if_unhealthy(counts, engine=deps.engine,
                         operator_has_allowlist=bool(deps.allowlist))
    return counts


def _alert_if_unhealthy(counts: dict[str, int], *, engine: Engine,
                          operator_has_allowlist: bool = False) -> None:
    """Bucket C: fire a daemon_critical alert when the cache looks unhealthy.

    Two triggers:
    1. Total eligible rows in `wheel_universe_cache` below floor → cache
       collapse (filter bug, mass de-listing, persistent Finnhub outage).
       NOTE: we read the cache, not `counts["eligible"]` — that count only
       reflects symbols *re-evaluated this run*. On any subsequent (cached)
       build it's near-zero even when the cache is healthy with 2k+ names.
    2. This run's unavailable rate above threshold → Finnhub flaking, which
       would silently shrink the cache over time without this signal.

    Best-effort — alerting must never raise. Idempotent dedup keys so the
    operator gets one alert per build, not per failure.
    """
    # When the operator runs in allowlist mode (curated list non-empty),
    # the wheel doesn't read from the discovered cache — alerts about
    # cache health become operator noise.
    if operator_has_allowlist:
        return
    try:
        cached_eligible = (
            Session(engine)
            .query(WheelUniverseCache)
            .filter_by(eligible=True)
            .count()
        )
        run_eligible = counts.get("eligible", 0)
        unavailable = counts.get("unavailable", 0)
        rejected = counts.get("rejected", 0)
        processed = run_eligible + unavailable + rejected
        unavailable_rate = unavailable / processed if processed else 0.0

        msgs: list[str] = []
        if cached_eligible < _ELIGIBLE_FLOOR_ALERT:
            msgs.append(
                f"<b>cache eligible={cached_eligible}</b> &lt; floor "
                f"{_ELIGIBLE_FLOOR_ALERT}"
            )
        if unavailable_rate > _UNAVAILABLE_RATE_ALERT:
            msgs.append(
                f"<b>finnhub_unavailable rate {unavailable_rate:.1%}</b> "
                f"&gt; {_UNAVAILABLE_RATE_ALERT:.0%}"
            )
        if not msgs:
            return
        detail = (
            "<p>Wheel universe cache looks unhealthy.</p>"
            f"<p>Cache: eligible={cached_eligible} total.</p>"
            f"<p>This run: re-evaluated={run_eligible} eligible, "
            f"rejected={rejected}, unavailable={unavailable}, "
            f"fell_out={counts.get('fell_out', 0)}, "
            f"cached_skip={counts.get('cached_skip', 0)}.</p>"
            "<ul>" + "".join(f"<li>{m}</li>" for m in msgs) + "</ul>"
            "<p>Affected names will be re-checked tomorrow due to the "
            "Bucket-C retry backoff. If this persists 2+ nights, investigate "
            "Finnhub status or the optionable-set source.</p>"
        )
        queue_alert(AlertEvent(
            kind="daemon_critical",
            severity="warn",
            title=f"Wheel universe degraded: cache_eligible={cached_eligible}",
            detail_html=detail,
            fired_at=dt.datetime.now(dt.timezone.utc),
            dedup_key=f"wheel_universe_unhealthy:{dt.date.today().isoformat()}",
        ))
    except Exception as e:  # pragma: no cover - alert path is best-effort
        log.warning("wheel_universe_unhealthy alert failed: %s", e)
