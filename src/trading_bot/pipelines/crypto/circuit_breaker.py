"""Crypto circuit breaker (Phase 1F).

Independent from the stocks breaker — different trip conditions, different
lookback windows, different state. Per ADR 0001 and the crypto plan:

  Trip when ANY of:
    1. BTC 4h drawdown > 8%               (REASON_CRYPTO_BTC_CRASH)
    2. ETH or BTC funding rate >= 0.15%/8h (REASON_CRYPTO_FUNDING)
    3. USDT or USDC > 1.5% off peg         (REASON_CRYPTO_DEPEG)
    4. Exchange API error rate > 50%       (REASON_CRYPTO_API_ERROR)
    5. >$1B 24h liquidations               (REASON_CRYPTO_LIQ_CASCADE)

When tripped, the orchestrator's pre-strategy gate reads
``is_tripped()`` and emits ``Decision(skipped_circuit_breaker)``.

Critical safety carve-out: ``hold_debate.exit_now`` is NEVER blocked
by the circuit breaker. Cutting losses is always allowed even when
new entries are paused.

Cooldown: configurable per-trip; default 30 min for crypto (faster
recovery cadence than stocks because crypto markets are 24/7 and
recover faster from technical noise).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import CircuitBreakerEventCrypto

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trip reasons + severity
# ---------------------------------------------------------------------------


class TripReason(str, Enum):
    BTC_CRASH = "crypto_btc_crash"
    FUNDING_EXTREME = "crypto_funding_extreme"
    STABLECOIN_DEPEG = "crypto_stablecoin_depeg"
    EXCHANGE_API_ERROR = "crypto_exchange_api_error"
    LIQUIDATION_CASCADE = "crypto_liquidation_cascade"


class TripSeverity(str, Enum):
    WARNING = "warning"   # log + alert; allow new entries
    HARD = "hard"         # halt new entries; hold-exits still allowed


# ---------------------------------------------------------------------------
# Default thresholds (mirrors strategy/config.yaml `crypto_circuit_breakers:`)
# ---------------------------------------------------------------------------


@dataclass
class CryptoBreakerThresholds:
    btc_4h_drawdown_pct: float = -8.0
    funding_rate_threshold: float = 0.0015          # 0.15%/8h
    stablecoin_depeg_pct: float = 1.5               # absolute % off $1.00
    exchange_api_error_rate: float = 0.5            # 50%
    liquidations_24h_usd: float = 1_000_000_000     # $1B
    cooldown_minutes: int = 30


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
# Pure evaluator — no DB writes; testable in isolation
# ---------------------------------------------------------------------------


def evaluate_crypto_metrics(
    *,
    btc_4h_drawdown_pct: Optional[float] = None,
    eth_funding_rate: Optional[float] = None,
    btc_funding_rate: Optional[float] = None,
    usdt_peg_deviation_pct: Optional[float] = None,
    usdc_peg_deviation_pct: Optional[float] = None,
    exchange_api_error_rate: Optional[float] = None,
    liquidations_24h_usd: Optional[float] = None,
    thresholds: Optional[CryptoBreakerThresholds] = None,
) -> TripDecision:
    """Pure evaluator — given current crypto-tail-risk metrics, decide
    whether the crypto breaker should trip and which reason wins.

    Priority when multiple conditions trip simultaneously:
      LIQ_CASCADE > BTC_CRASH > STABLECOIN_DEPEG > FUNDING_EXTREME > API_ERROR
    (most-acute first; the others get logged into ``state`` for audit but
    don't change the primary reason.)
    """
    th = thresholds or CryptoBreakerThresholds()
    state: Dict[str, Any] = {}
    reasons: List[TripReason] = []

    if liquidations_24h_usd is not None:
        state["liquidations_24h_usd"] = liquidations_24h_usd
        if liquidations_24h_usd >= th.liquidations_24h_usd:
            reasons.append(TripReason.LIQUIDATION_CASCADE)

    if btc_4h_drawdown_pct is not None:
        state["btc_4h_drawdown_pct"] = btc_4h_drawdown_pct
        if btc_4h_drawdown_pct <= th.btc_4h_drawdown_pct:
            reasons.append(TripReason.BTC_CRASH)

    if usdt_peg_deviation_pct is not None:
        state["usdt_peg_deviation_pct"] = usdt_peg_deviation_pct
        if abs(usdt_peg_deviation_pct) >= th.stablecoin_depeg_pct:
            reasons.append(TripReason.STABLECOIN_DEPEG)
    if usdc_peg_deviation_pct is not None:
        state["usdc_peg_deviation_pct"] = usdc_peg_deviation_pct
        if abs(usdc_peg_deviation_pct) >= th.stablecoin_depeg_pct:
            reasons.append(TripReason.STABLECOIN_DEPEG)

    for rate, label in (
        (eth_funding_rate, "eth_funding_rate"),
        (btc_funding_rate, "btc_funding_rate"),
    ):
        if rate is None:
            continue
        state[label] = rate
        if abs(rate) >= th.funding_rate_threshold:
            reasons.append(TripReason.FUNDING_EXTREME)

    if exchange_api_error_rate is not None:
        state["exchange_api_error_rate"] = exchange_api_error_rate
        if exchange_api_error_rate >= th.exchange_api_error_rate:
            reasons.append(TripReason.EXCHANGE_API_ERROR)

    if not reasons:
        return TripDecision(should_trip=False, state=state)

    priority = (
        TripReason.LIQUIDATION_CASCADE,
        TripReason.BTC_CRASH,
        TripReason.STABLECOIN_DEPEG,
        TripReason.FUNDING_EXTREME,
        TripReason.EXCHANGE_API_ERROR,
    )
    primary = sorted(reasons, key=lambda r: priority.index(r))[0]
    state["all_reasons"] = sorted({r.value for r in reasons})

    # API error alone is a soft warning; everything else is hard halt.
    severity = (
        TripSeverity.WARNING
        if primary == TripReason.EXCHANGE_API_ERROR
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
    """Persist a trip event. Returns the new row id. Caller is responsible
    for surfacing this via the alerting channel."""
    if not decision.should_trip or decision.reason is None:
        raise ValueError("trip() called without a tripped decision")
    now = now or dt.datetime.now(dt.timezone.utc)
    cooldown = cooldown_minutes if cooldown_minutes is not None else CryptoBreakerThresholds().cooldown_minutes
    with Session(engine) as session:
        row = CircuitBreakerEventCrypto(
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
) -> Optional[CircuitBreakerEventCrypto]:
    """Return the active trip row if the crypto breaker is currently tripped,
    else None. Active = ``cleared_at`` IS NULL AND ``tripped_at + cooldown >= now``.

    The orchestrator's pre-strategy gate calls this; it should treat
    ``hold_debate.exit_now`` as exempt from this check (cut-losses
    always-allowed safety carve-out).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(CircuitBreakerEventCrypto)
            .filter(CircuitBreakerEventCrypto.cleared_at.is_(None))
            .order_by(CircuitBreakerEventCrypto.tripped_at.desc())
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
            session.query(CircuitBreakerEventCrypto)
            .filter(CircuitBreakerEventCrypto.cleared_at.is_(None))
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
            session.query(CircuitBreakerEventCrypto)
            .filter(CircuitBreakerEventCrypto.cleared_at.is_(None))
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
