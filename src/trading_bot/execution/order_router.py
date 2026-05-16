"""Order router — risk-gated, idempotent, freshness-checked submission.

Plan v4 §3 + §5 + §6: the only path from a kernel intent to a broker
order. Every step writes to the ledger so the entire decision is
auditable from on-disk artifacts.

The broker call is injected (``broker_submit`` callback). For Phase 3
unit tests we pass a fake; the kernel daemon (Phase 5) will pass the
hardened Alpaca client.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

# WS5a — retry policy for transient broker failures.
RETRY_BACKOFFS_S: tuple[float, ...] = (0.5, 1.0, 2.0)

# Classify broker errors: transient (retry) vs permanent (cancel).
_PERMANENT_KEYWORDS = (
    "rejected",
    "insufficient_buying_power",
    "insufficient buying power",
    "market_closed",
    "market closed",
    "invalid_symbol",
    "invalid symbol",
    "wash_trade",
    "wash trade",
    "shadow_mode",
    "pdt",
    "day_trading_buying_power_exceeded",
)
_TRANSIENT_KEYWORDS = (
    "timeout",
    "timed out",
    "connection reset",
    "connectionreseterror",
    "rate limit",
    "rate_limit",
    "throttle",
    "503",
    "502",
    "504",
    "500",
    "5xx",
    "service unavailable",
    "gateway",
)


def _is_transient(err: str) -> bool:
    """Classify a broker error string. Default: treat as permanent (no
    retry) to avoid hammering the broker with bad orders."""
    if not err:
        return False
    e = err.lower()
    if any(k in e for k in _PERMANENT_KEYWORDS):
        return False
    return any(k in e for k in _TRANSIENT_KEYWORDS)


def _submit_with_retry(
    broker_submit: "BrokerSubmitT",
    submit_payload: dict,
    *,
    backoffs: tuple[float, ...] = RETRY_BACKOFFS_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Wrap broker_submit with exponential backoff for transient errors.

    Returns the final broker result dict (whether eventual success or
    permanent failure). Attempts are bounded by ``len(backoffs)+1``.
    """
    attempt = 0
    last: dict = {"ok": False, "broker_order_id": None, "error": "no_attempt"}
    while True:
        attempt += 1
        try:
            result = broker_submit(**submit_payload)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "broker_order_id": None, "error": str(e)}
        last = result
        if result.get("ok"):
            if attempt > 1:
                log.info("order_router: succeeded on attempt %d", attempt)
            return result
        err = str(result.get("error", ""))
        if not _is_transient(err) or attempt > len(backoffs):
            return result
        sleep_for = backoffs[attempt - 1]
        log.warning(
            "order_router: transient broker error (attempt %d/%d): %s; "
            "retrying in %.1fs",
            attempt, len(backoffs) + 1, err, sleep_for,
        )
        sleep(sleep_for)

from trading_bot.ingest.watermarks import check_lane_freshness
from trading_bot.ledger import (
    OrderIntent, append_state_event, check_idempotent, insert_order_master,
    write_decision,
)
from trading_bot.risk import precheck
from trading_bot.risk.policy_loader import PolicyBundle
from trading_bot.risk.types import AccountState, Position, RiskDecision

BrokerSubmitT = Callable[..., dict]
"""Signature: ``(client_order_id, symbol, qty, side, ...)`` -> dict with
at least ``ok: bool`` and ``broker_order_id: str | None`` keys."""


@dataclass(frozen=True)
class SubmissionResult:
    submitted: bool
    risk_verdict: str          # accept | reduce | halt
    reason: str
    order_uid: Optional[str] = None
    broker_order_id: Optional[str] = None
    effective_qty: Optional[float] = None
    risk_reason: Optional[str] = None


def submit_order(
    *,
    conn: sqlite3.Connection,
    intent: OrderIntent,
    account: AccountState,
    positions: Sequence[Position],
    policy: PolicyBundle,
    lane: str,
    quote_lane: str,             # which lane's watermark to check ("equity"|"crypto"|"option")
    intent_price: float,
    broker_submit: BrokerSubmitT,
    stop_loss_price: Optional[float] = None,
    lane_session_pnl_pct: float = 0.0,
    strategy_30d_loss_pct: float = 0.0,
    feature_snapshot_id: str = "phase3-unwired",
    code_hash: str = "",
    config_hash: str = "",
    now: Optional[dt.datetime] = None,
) -> SubmissionResult:
    """Single-entry submission path. Every step writes to the ledger."""
    now = now or dt.datetime.now(dt.timezone.utc)

    # 1) Risk precheck (also handles active kills via halt_router)
    decision: RiskDecision = precheck.evaluate(
        conn=conn, intent=intent, account=account, positions=positions,
        policy=policy, lane=lane, intent_price=intent_price,
        stop_loss_price=stop_loss_price,
        lane_session_pnl_pct=lane_session_pnl_pct,
        strategy_30d_loss_pct=strategy_30d_loss_pct,
    )
    if decision.verdict == "halt":
        _log_decision(
            conn=conn, intent=intent, policy=policy,
            feature_snapshot_id=feature_snapshot_id,
            code_hash=code_hash, config_hash=config_hash,
            risk_decision="halt", risk_reason=decision.reason,
            emitted_client_order_id=None, now=now,
        )
        return SubmissionResult(
            submitted=False, risk_verdict="halt", reason=decision.reason,
            risk_reason=decision.reason,
        )

    effective_qty = (decision.adjusted_qty
                     if decision.verdict == "reduce" else intent.qty)

    # 2) Freshness check
    fresh = check_lane_freshness(
        conn, lane=quote_lane,
        data_freshness_lock=policy.data_freshness, now=now,
    )
    if fresh.verdict == "halt":
        _log_decision(
            conn=conn, intent=intent, policy=policy,
            feature_snapshot_id=feature_snapshot_id,
            code_hash=code_hash, config_hash=config_hash,
            risk_decision="halt", risk_reason=fresh.reason,
            emitted_client_order_id=None, now=now,
        )
        return SubmissionResult(
            submitted=False, risk_verdict="halt", reason=fresh.reason,
            risk_reason=fresh.reason,
        )

    # 3) Idempotency check (uses Phase 1 helper)
    status, existing_uid = check_idempotent(conn, intent.client_order_id)
    if status == "active":
        _log_decision(
            conn=conn, intent=intent, policy=policy,
            feature_snapshot_id=feature_snapshot_id,
            code_hash=code_hash, config_hash=config_hash,
            risk_decision="halt",
            risk_reason=f"idempotent:active:{existing_uid}",
            emitted_client_order_id=None, now=now,
        )
        return SubmissionResult(
            submitted=False, risk_verdict="halt",
            reason="idempotent:active",
            risk_reason=f"existing order_uid={existing_uid}",
        )

    # 4) Insert order_master + initial intent state event
    order_uid = insert_order_master(conn, intent, now=now)
    append_state_event(conn, order_uid=order_uid, to_state="intent", now=now)

    # 5) Broker submit
    submit_payload = dict(
        client_order_id=intent.client_order_id,
        symbol=intent.symbol, asset_class=intent.asset_class,
        side=intent.side, qty=effective_qty,
        limit_price=intent.limit_price, tif=intent.tif,
    )
    result = _submit_with_retry(broker_submit, submit_payload)

    if not result.get("ok"):
        # Cancel chain: intent -> cancelled
        append_state_event(
            conn, order_uid=order_uid, to_state="cancelled",
            reason=f"broker_submit_failed: {result.get('error', 'unknown')}",
            now=now,
        )
        _log_decision(
            conn=conn, intent=intent, policy=policy,
            feature_snapshot_id=feature_snapshot_id,
            code_hash=code_hash, config_hash=config_hash,
            risk_decision=decision.verdict, risk_reason=decision.reason,
            emitted_client_order_id=intent.client_order_id, now=now,
        )
        return SubmissionResult(
            submitted=False, risk_verdict=decision.verdict,
            reason="broker_submit_failed", order_uid=order_uid,
            effective_qty=effective_qty,
            risk_reason=result.get("error"),
        )

    broker_order_id = result.get("broker_order_id")
    append_state_event(
        conn, order_uid=order_uid, to_state="submitted",
        broker_order_id=broker_order_id, now=now,
    )
    _log_decision(
        conn=conn, intent=intent, policy=policy,
        feature_snapshot_id=feature_snapshot_id,
        code_hash=code_hash, config_hash=config_hash,
        risk_decision=decision.verdict, risk_reason=decision.reason,
        emitted_client_order_id=intent.client_order_id, now=now,
    )
    return SubmissionResult(
        submitted=True, risk_verdict=decision.verdict,
        reason=decision.reason, order_uid=order_uid,
        broker_order_id=broker_order_id,
        effective_qty=effective_qty,
    )


def _log_decision(
    *,
    conn: sqlite3.Connection,
    intent: OrderIntent,
    policy: PolicyBundle,
    feature_snapshot_id: str,
    code_hash: str,
    config_hash: str,
    risk_decision: str,
    risk_reason: Optional[str],
    emitted_client_order_id: Optional[str],
    now: dt.datetime,
) -> None:
    write_decision(
        conn,
        strategy_id=intent.strategy_id,
        strategy_ver=intent.strategy_ver,
        code_hash=code_hash or "phase3-unwired",
        config_hash=config_hash or "phase3-unwired",
        policy_hash=policy.combined_hash,
        feature_snapshot_id=feature_snapshot_id,
        intent={
            "client_order_id": intent.client_order_id,
            "symbol": intent.symbol, "side": intent.side,
            "qty": intent.qty, "limit_price": intent.limit_price,
            "tif": intent.tif, "origin": intent.origin,
        },
        risk_decision=risk_decision,
        risk_reason=risk_reason,
        emitted_client_order_id=emitted_client_order_id,
        now=now,
    )


__all__ = ["BrokerSubmitT", "SubmissionResult", "submit_order"]
