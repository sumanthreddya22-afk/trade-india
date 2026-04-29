"""Signal-driven wheel candidate sourcing.

The wheel only acts when news/intel surfaces a reason. We don't enumerate
the 6,000+ optionable equities and probe Finnhub per name — that's expensive
and unfocused. Instead, this module produces a small ranked list of candidates
each scan, drawing from:

  1. **post_earnings_iv_crush** — earnings landed in the last 1–3 days AND
     local IV history says current IV is in the top half of trailing range.
     Classic CSP setup: IV is elevated post-earnings and reverts down.

  2. **stable_elevated_iv** — sentiment is calm-to-mildly-positive (Polygon
     score in [0.0, 0.5]) AND IV rank ≥ floor. Defensive premium-collection
     setup on names that aren't melting down or euphoric.

A global VIX gate (15 ≤ VIX ≤ 30) cancels the entire scan when conditions
disqualify wheel-selling regardless of single-name signals.

Candidate selection NEVER fetches option chains directly — it reads IV
history written by a separate `iv_capture` job. The wheel runner then
fetches one chain per surfaced candidate. End-to-end chain-fetch budget is
~30/day total, all justified by a signal."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.config import WheelConfig
from trading_bot.intelligence_finnhub import FinnhubClient, FinnhubUnavailable
from trading_bot.options.iv_rank import compute_iv_rank
from trading_bot.state_db import OptionIvHistory


@dataclass(frozen=True)
class WheelCandidate:
    symbol: str
    signal: str  # "post_earnings_iv_crush" | "stable_elevated_iv"
    confidence: float  # 0.0–1.0; higher = stronger signal
    reason: str
    iv_rank: float | None
    last_iv: float | None


@dataclass(frozen=True)
class SignalDeps:
    finnhub: FinnhubClient
    iv_engine: Engine
    sentiment_for: Callable[[str], float | None]
    macro_snapshotter: object
    today: dt.date


# Signal threshold: post-earnings looks 1–3 days back to catch the IV-crush
# window without missing a same-day-reported name; sentiment band picks up
# names that are "stable" (not crashing, not euphoric).
_EARNINGS_LOOKBACK_DAYS = 3
_SENTIMENT_LOW = 0.0
_SENTIMENT_HIGH = 0.5
# IV-rank min_history is intentionally permissive (5 days) — the bot can
# start producing signals after a week of iv_capture, not after a year.
_IV_MIN_HISTORY = 5


def _read_last_iv(engine: Engine, symbol: str) -> float | None:
    with Session(engine) as s:
        row = s.execute(
            select(OptionIvHistory.atm_iv_30d)
            .where(OptionIvHistory.symbol == symbol)
            .order_by(desc(OptionIvHistory.recorded_at))
            .limit(1)
        ).scalar_one_or_none()
        return float(row) if row is not None else None


def _vix_in_band(vix: float | None, cfg: WheelConfig) -> bool:
    return vix is not None and cfg.vix_floor <= vix <= cfg.vix_ceiling


def produce_candidates(
    deps: SignalDeps, *, eligible: set[str], cfg: WheelConfig,
) -> list[WheelCandidate]:
    """Return ranked wheel candidates for today.

    `eligible` is the caller-curated set (allowlist minus blocklist). We
    only consider these symbols — never the full optionable universe. The
    output is sorted by confidence descending, so the runner can act on
    the strongest signals first.
    """
    macro = deps.macro_snapshotter.snapshot()
    if not _vix_in_band(getattr(macro, "vix", None), cfg):
        return []
    if not eligible:
        return []

    # Pull recent earnings ONCE for the eligible set. Finnhub returns the
    # whole window across the market; we filter to eligible. If Finnhub is
    # down, fall back to no-earnings-signal — stable_elevated_iv may still fire.
    earnings_recent: set[str] = set()
    try:
        rows = deps.finnhub.earnings_calendar(
            deps.today - dt.timedelta(days=_EARNINGS_LOOKBACK_DAYS),
            deps.today,
        )
        earnings_recent = {r.symbol for r in rows if r.symbol in eligible}
    except FinnhubUnavailable:
        pass

    candidates: list[WheelCandidate] = []
    for sym in eligible:
        last_iv = _read_last_iv(deps.iv_engine, sym)
        if last_iv is None:
            continue  # no history → cannot rank → skip
        rank = compute_iv_rank(
            deps.iv_engine, sym, current_iv=last_iv, min_history=_IV_MIN_HISTORY,
        )
        if rank is None or rank < cfg.iv_rank_floor:
            continue

        # Priority 1: post-earnings (signal ranks higher because the catalyst is concrete)
        if sym in earnings_recent:
            candidates.append(WheelCandidate(
                symbol=sym, signal="post_earnings_iv_crush",
                confidence=min(1.0, rank / 100 + 0.10),  # +10pp boost vs. plain elevated
                reason=f"earnings 1-{_EARNINGS_LOOKBACK_DAYS}d ago, IV rank {rank:.0f}",
                iv_rank=rank, last_iv=last_iv,
            ))
            continue

        # Priority 2: sentiment-stable elevated IV
        sent = deps.sentiment_for(sym)
        if sent is None or _SENTIMENT_LOW <= sent <= _SENTIMENT_HIGH:
            candidates.append(WheelCandidate(
                symbol=sym, signal="stable_elevated_iv",
                confidence=min(1.0, rank / 100),
                reason=(f"IV rank {rank:.0f}, "
                        f"sentiment {sent:.2f}" if sent is not None
                        else f"IV rank {rank:.0f}, no sentiment data"),
                iv_rank=rank, last_iv=last_iv,
            ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates
