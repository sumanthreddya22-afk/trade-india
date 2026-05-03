"""Options circuit breaker (Phase 3).

Independent from the stocks + crypto breakers — different trip
conditions, different lookback windows, different state. Per ADR 0001:

  Trip when ANY of:
    1. VIX > 35                           (REASON_OPTIONS_VIX_SPIKE)
    2. VIX term-structure inversion (front > back)
                                           (REASON_OPTIONS_TERM_INVERSION)
    3. Earnings cluster: > 50% of held wheel cycles' underlyings have
       earnings within next 5 trading days
                                           (REASON_OPTIONS_EARNINGS_CLUSTER)
    4. Bid-ask widening: median wheel-eligible spread > 8% of mid
                                           (REASON_OPTIONS_LIQUIDITY)
    5. Realised vs. implied gap: realised 30d vol / IV > 1.5 (premium
       too cheap → MM dominance flipped, retail short-vol unwind risk)
                                           (REASON_OPTIONS_REALIZED_VOL)

When tripped:
  - new wheel-cycle entries blocked (orchestrator reads ``is_tripped``
    in the pre-strategy gate).
  - existing-cycle management ALWAYS allowed: rolls, close-for-loss,
    and assignment handling never blocked. The wheel's worst case is
    a stuck CSP after IV crush; we must always be able to close it.

Cooldown: 60 minutes default (longer than crypto because options vol
regime shifts persist; shorter than stocks because options markets
recover when VIX leg comes off).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from trading_bot.pipelines.options.state_db import CircuitBreakerEventOptions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trip reasons + severity
# ---------------------------------------------------------------------------


class TripReason(str, Enum):
    VIX_SPIKE = "options_vix_spike"
    TERM_INVERSION = "options_term_inversion"
    EARNINGS_CLUSTER = "options_earnings_cluster"
    LIQUIDITY = "options_liquidity_crisis"
    REALIZED_VOL_GAP = "options_realized_vol_gap"


class TripSeverity(str, Enum):
    WARNING = "warning"   # log + alert; allow new entries
    HARD = "hard"         # halt new entries; existing-cycle management always allowed


# ---------------------------------------------------------------------------
# Default thresholds (tunable in Phase 3+ adaptive thresholds)
# ---------------------------------------------------------------------------


@dataclass
class OptionsBreakerThresholds:
    vix_spike_level: float = 35.0
    vix_term_inversion_min_pct: float = 1.0      # front > back by >= 1%
    earnings_cluster_pct: float = 50.0           # >50% of held cycles
    earnings_cluster_lookahead_days: int = 5
    liquidity_max_spread_pct: float = 8.0        # median spread / mid
    realized_implied_ratio: float = 1.5          # realized 30d / IV
    cooldown_minutes: int = 60


# ---------------------------------------------------------------------------
# Trip decision
# ---------------------------------------------------------------------------


@dataclass
class TripDecision:
    should_trip: bool
    reason: Optional[TripReason] = None
    severity: TripSeverity = TripSeverity.HARD
    state: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure evaluator — testable in isolation
# ---------------------------------------------------------------------------


def evaluate_options_metrics(
    *,
    vix_level: Optional[float] = None,
    vix_term_front: Optional[float] = None,
    vix_term_back: Optional[float] = None,
    earnings_cluster_pct: Optional[float] = None,
    median_spread_pct: Optional[float] = None,
    realized_30d_vol: Optional[float] = None,
    atm_iv: Optional[float] = None,
    thresholds: Optional[OptionsBreakerThresholds] = None,
) -> TripDecision:
    """Pure evaluator — given current options-tail-risk metrics, decide
    whether the options breaker should trip and which reason wins.

    Priority when multiple conditions trip simultaneously:
      VIX_SPIKE > TERM_INVERSION > REALIZED_VOL_GAP > LIQUIDITY > EARNINGS_CLUSTER
    """
    th = thresholds or OptionsBreakerThresholds()
    state: Dict[str, Any] = {}
    reasons: List[TripReason] = []

    if vix_level is not None:
        state["vix_level"] = vix_level
        if vix_level >= th.vix_spike_level:
            reasons.append(TripReason.VIX_SPIKE)

    if vix_term_front is not None and vix_term_back is not None:
        state["vix_term_front"] = vix_term_front
        state["vix_term_back"] = vix_term_back
        if vix_term_back > 0:
            inversion_pct = (vix_term_front - vix_term_back) / vix_term_back * 100.0
            state["vix_term_inversion_pct"] = round(inversion_pct, 4)
            if inversion_pct >= th.vix_term_inversion_min_pct:
                reasons.append(TripReason.TERM_INVERSION)

    if earnings_cluster_pct is not None:
        state["earnings_cluster_pct"] = earnings_cluster_pct
        if earnings_cluster_pct >= th.earnings_cluster_pct:
            reasons.append(TripReason.EARNINGS_CLUSTER)

    if median_spread_pct is not None:
        state["median_spread_pct"] = median_spread_pct
        if median_spread_pct >= th.liquidity_max_spread_pct:
            reasons.append(TripReason.LIQUIDITY)

    if realized_30d_vol is not None and atm_iv is not None and atm_iv > 0:
        ratio = realized_30d_vol / atm_iv
        state["realized_implied_ratio"] = round(ratio, 4)
        if ratio >= th.realized_implied_ratio:
            reasons.append(TripReason.REALIZED_VOL_GAP)

    if not reasons:
        return TripDecision(should_trip=False, state=state)

    priority = (
        TripReason.VIX_SPIKE,
        TripReason.TERM_INVERSION,
        TripReason.REALIZED_VOL_GAP,
        TripReason.LIQUIDITY,
        TripReason.EARNINGS_CLUSTER,
    )
    primary = sorted(reasons, key=lambda r: priority.index(r))[0]
    state["all_reasons"] = sorted({r.value for r in reasons})

    # Earnings cluster is a soft warning (we may still want to enter
    # CSPs on names without earnings); everything else is hard halt.
    severity = (
        TripSeverity.WARNING
        if primary == TripReason.EARNINGS_CLUSTER
        else TripSeverity.HARD
    )
    return TripDecision(should_trip=True, reason=primary, severity=severity, state=state)


# ---------------------------------------------------------------------------
# Persistence + state queries
# ---------------------------------------------------------------------------


def trip(
    engine: Any,
    *,
    decision: TripDecision,
    cooldown_minutes: Optional[int] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    """Persist a trip event. Returns the new row id."""
    if not decision.should_trip or decision.reason is None:
        raise ValueError("trip() called without a tripped decision")
    now = now or dt.datetime.now(dt.timezone.utc)
    cooldown = (
        cooldown_minutes
        if cooldown_minutes is not None
        else OptionsBreakerThresholds().cooldown_minutes
    )
    with Session(engine) as session:
        row = CircuitBreakerEventOptions(
            tripped_at=now,
            cleared_at=None,
            reason=decision.reason.value,
            severity=decision.severity.value,
            trip_state_json=json.dumps(decision.state, sort_keys=True, default=str),
            cooldown_minutes=cooldown,
        )
        session.add(row)
        session.commit()
        return row.id


def is_tripped(
    engine: Any,
    *,
    now: Optional[dt.datetime] = None,
) -> Optional[CircuitBreakerEventOptions]:
    """Return the active trip row if the options breaker is currently tripped,
    else None. Active = ``cleared_at`` IS NULL AND ``tripped_at + cooldown >= now``.

    Existing-cycle management (rolls, close-for-loss, assignment) is
    NEVER blocked by this — caller treats those code paths as exempt.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(CircuitBreakerEventOptions)
            .filter(CircuitBreakerEventOptions.cleared_at.is_(None))
            .order_by(CircuitBreakerEventOptions.tripped_at.desc())
            .all()
        )
    for row in rows:
        tripped_at = row.tripped_at
        if tripped_at.tzinfo is None:
            tripped_at = tripped_at.replace(tzinfo=dt.timezone.utc)
        cooldown_end = tripped_at + dt.timedelta(minutes=row.cooldown_minutes)
        if now < cooldown_end:
            return row
    return None


def clear(
    engine: Any,
    *,
    now: Optional[dt.datetime] = None,
) -> int:
    """Manually clear all active trips. Returns count cleared."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(CircuitBreakerEventOptions)
            .filter(CircuitBreakerEventOptions.cleared_at.is_(None))
            .all()
        )
        for r in rows:
            r.cleared_at = now
        session.commit()
        return len(rows)


def auto_clear_expired(
    engine: Any,
    *,
    now: Optional[dt.datetime] = None,
) -> int:
    """Mark trips whose cooldown has elapsed as cleared. Returns count cleared."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cleared = 0
    with Session(engine) as session:
        rows = (
            session.query(CircuitBreakerEventOptions)
            .filter(CircuitBreakerEventOptions.cleared_at.is_(None))
            .all()
        )
        for r in rows:
            tripped_at = r.tripped_at
            if tripped_at.tzinfo is None:
                tripped_at = tripped_at.replace(tzinfo=dt.timezone.utc)
            cooldown_end = tripped_at + dt.timedelta(minutes=r.cooldown_minutes)
            if now >= cooldown_end:
                r.cleared_at = now
                cleared += 1
        session.commit()
    return cleared
