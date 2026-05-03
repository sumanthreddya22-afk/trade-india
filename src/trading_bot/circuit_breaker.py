"""Phase F — Circuit Breakers.

Lightweight active-state machine on top of ``circuit_breaker_events``
audit table:

  * ``trip(reason, expires_at, detail)`` writes a 'tripped' row.
  * ``clear(reason)`` writes a 'cleared' row that supersedes any active trip.
  * ``is_tripped()`` reads the most recent row and returns whether the
    breaker is active. Auto-clears on expires_at < now.

Trip reasons are bucket strings — the orchestrator's scan path checks
``is_tripped()`` BEFORE strategy resolution and skips the entry path
when active. Hold-debate exit_now is intentionally NOT gated by the
breaker (we always preserve the ability to cut losses).

Pure SQL — no LLM, no network. Cheap and fast.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import desc as _desc
from sqlalchemy.orm import Session

from trading_bot.state_db import CircuitBreakerEvent


log = logging.getLogger(__name__)


# Reason codes — short strings used as the audit reason and the trip key.
REASON_VIX_SPIKE = "vix_spike"
REASON_DRAWDOWN = "daily_drawdown"
REASON_CONSECUTIVE_LOSSES = "consecutive_losses"
REASON_FAST_STOPS = "fast_stops"
REASON_API_ERROR_RATE = "api_error_rate"
REASON_OPERATOR = "operator_manual"


@dataclass(frozen=True)
class BreakerState:
    tripped: bool
    reason: str | None
    expires_at: dt.datetime | None
    event_at: dt.datetime | None
    detail: dict


def trip(
    engine,
    *,
    reason: str,
    detail: dict | None = None,
    cooldown_minutes: int = 60,
    now: dt.datetime | None = None,
) -> CircuitBreakerEvent:
    """Write a 'tripped' row. Returns the row.

    ``cooldown_minutes`` sets ``expires_at`` so the breaker auto-clears
    after the cooldown without an explicit clear() call (defensive — a
    monitoring agent may forget to clear).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    expires_at = now + dt.timedelta(minutes=int(cooldown_minutes)) if cooldown_minutes > 0 else None
    row = CircuitBreakerEvent(
        event_at=now,
        action="tripped",
        reason=reason,
        detail_json=json.dumps(detail or {}, default=str),
        expires_at=expires_at,
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    log.warning("circuit_breaker tripped: reason=%s detail=%s", reason, detail)
    return row


def clear(
    engine, *, reason: str = "operator_manual", now: dt.datetime | None = None,
) -> CircuitBreakerEvent:
    now = now or dt.datetime.now(dt.timezone.utc)
    row = CircuitBreakerEvent(
        event_at=now, action="cleared", reason=reason, detail_json="{}",
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    log.info("circuit_breaker cleared: reason=%s", reason)
    return row


def state(engine, *, now: dt.datetime | None = None) -> BreakerState:
    """Compute current breaker state from the most recent event.

    Logic:
      - Most recent row is 'cleared' → not tripped
      - Most recent row is 'tripped' AND expires_at is in the past → not tripped
      - Most recent row is 'tripped' AND not expired → tripped
      - No rows → not tripped (default safe state)
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        row = (
            session.query(CircuitBreakerEvent)
            .order_by(_desc(CircuitBreakerEvent.event_at))
            .first()
        )
        if row is None:
            return BreakerState(tripped=False, reason=None, expires_at=None,
                                event_at=None, detail={})
        action = row.action
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        try:
            detail = json.loads(row.detail_json or "{}")
        except Exception:
            detail = {}
    if action != "tripped":
        return BreakerState(tripped=False, reason=row.reason, expires_at=expires,
                            event_at=row.event_at, detail=detail)
    if expires is not None and expires < now:
        return BreakerState(tripped=False, reason=row.reason, expires_at=expires,
                            event_at=row.event_at, detail=detail)
    return BreakerState(tripped=True, reason=row.reason, expires_at=expires,
                        event_at=row.event_at, detail=detail)


def is_tripped(engine, *, now: dt.datetime | None = None) -> bool:
    return state(engine, now=now).tripped


# ---------------------------------------------------------------------------
# Trip evaluators (pure-ish — caller passes the metric, we decide trip/clear)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TripDecision:
    should_trip: bool
    reason: str | None
    detail: dict


def evaluate_metrics(
    *,
    vix: float | None = None,
    vix_threshold: float = 35.0,
    daily_pnl_pct: float | None = None,
    dd_threshold_pct: float = -3.0,
    consecutive_losses: int = 0,
    consecutive_losses_threshold: int = 3,
    fast_stops_count: int = 0,
    fast_stops_threshold: int = 5,
    api_error_rate: float | None = None,
    api_error_rate_threshold: float = 0.5,
) -> TripDecision:
    """Evaluate operator-supplied metrics; return the FIRST trip condition
    that fires. Reason priority is operational severity (VIX > drawdown
    > losses > fast-stops > api errors).
    """
    if vix is not None and vix > vix_threshold:
        return TripDecision(True, REASON_VIX_SPIKE,
                             {"vix": vix, "threshold": vix_threshold})
    if daily_pnl_pct is not None and daily_pnl_pct < dd_threshold_pct:
        return TripDecision(True, REASON_DRAWDOWN,
                             {"daily_pnl_pct": daily_pnl_pct, "threshold": dd_threshold_pct})
    if consecutive_losses >= consecutive_losses_threshold:
        return TripDecision(True, REASON_CONSECUTIVE_LOSSES,
                             {"consecutive": consecutive_losses,
                              "threshold": consecutive_losses_threshold})
    if fast_stops_count >= fast_stops_threshold:
        return TripDecision(True, REASON_FAST_STOPS,
                             {"count": fast_stops_count,
                              "threshold": fast_stops_threshold})
    if api_error_rate is not None and api_error_rate >= api_error_rate_threshold:
        return TripDecision(True, REASON_API_ERROR_RATE,
                             {"rate": api_error_rate, "threshold": api_error_rate_threshold})
    return TripDecision(False, None, {})
