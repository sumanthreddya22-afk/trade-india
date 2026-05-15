"""Single-entry risk gate. Plan v4 §6 — the L6 risk kernel.

Every order intent goes through ``evaluate``. The function composes:

  1. halt_router (active kill switches)
  2. account_caps  (daily DD, intraday floor)
  3. pdt           (entry-side only)
  4. asset_class_caps
  5. lane_status + per_lane_allocation
  6. per_strategy_loss
  7. per_symbol_cap (may reduce qty)
  8. per_order_cap

Strictest rule wins. If any check returns ``halt``, the rest are
skipped. If ``per_symbol_cap`` returns ``reduce``, the order continues
with the adjusted qty but ``per_order_cap`` is re-evaluated against
the adjusted qty.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

from trading_bot.ledger.order_master import OrderIntent
from trading_bot.risk import (
    account_caps, asset_class_caps, halt_router, kill_switches,
    lane_caps, live_capital, pdt, strategy_caps, symbol_order_caps,
)
from trading_bot.risk.limits import RiskLimits, parse_risk_policy
from trading_bot.risk.policy_loader import PolicyBundle
from trading_bot.risk.types import AccountState, Position, RiskDecision


def evaluate(
    *,
    conn: Optional[sqlite3.Connection],
    intent: OrderIntent,
    account: AccountState,
    positions: Sequence[Position],
    policy: PolicyBundle,
    lane: str,
    intent_price: float,
    stop_loss_price: Optional[float] = None,
    lane_session_pnl_pct: float = 0.0,
    strategy_30d_loss_pct: float = 0.0,
) -> RiskDecision:
    """Compose every Phase 2 risk check into one decision.

    ``conn`` is optional: when provided, the function consults the
    kill_switch_event table for active kills; when None, callers must
    pre-compute the active set (used by isolated unit tests).
    """
    limits: RiskLimits = parse_risk_policy(policy.risk_policy)

    # 1. Active kill switches
    active = (
        kill_switches.active_kills(conn) if conn is not None else set()
    )
    d = halt_router.decide(
        active_kill_set=active, intent_side=intent.side,
    )
    if d.verdict == "halt":
        return d

    # 2. Account-level checks
    d = account_caps.check_daily_drawdown(account, limits.account)
    if d.verdict == "halt":
        return d
    d = account_caps.check_intraday_pnl_floor(account, limits.account)
    if d.verdict == "halt":
        return d

    # 3. PDT entry-side gate
    d = pdt.check_pdt(intent_side=intent.side, account=account,
                      pdt_lock=policy.pdt_policy)
    if d.verdict == "halt":
        return d

    # 4. Asset-class caps
    intent_notional = intent.qty * intent_price
    d = asset_class_caps.check_asset_class_caps(
        intent_asset_class=intent.asset_class,
        intent_notional=intent_notional,
        intent_side=intent.side,
        account=account,
        positions=positions,
        limits=limits.asset_class,
    )
    if d.verdict == "halt":
        return d

    # 5. Lane status + per-lane allocation + per-lane daily loss
    d = lane_caps.check_lane_status(
        lane=lane, lane_caps_lock=policy.lane_caps,
        intent_side=intent.side,
    )
    if d.verdict == "halt":
        return d
    d = lane_caps.check_per_lane_allocation(
        lane=lane, intent_notional=intent_notional,
        intent_side=intent.side, account=account, positions=positions,
        limits=limits.lane,
    )
    if d.verdict == "halt":
        return d
    d = lane_caps.check_per_lane_daily_loss(
        lane=lane, lane_session_pnl_pct=lane_session_pnl_pct,
        limits=limits.lane,
    )
    if d.verdict == "halt":
        return d

    # 6. Per-strategy 30-day loss
    d = strategy_caps.check_per_strategy_loss(
        strategy_id=intent.strategy_id,
        strategy_30d_loss_pct=strategy_30d_loss_pct,
        limits=limits.strategy,
    )
    if d.verdict == "halt":
        return d

    # 7. Per-symbol cap (may reduce qty)
    d = symbol_order_caps.check_per_symbol_cap(
        intent_symbol=intent.symbol, intent_qty=intent.qty,
        intent_price=intent_price, intent_side=intent.side,
        account=account, positions=positions,
        limits=limits.symbol,
    )
    effective_qty = intent.qty
    reduce_reason: Optional[str] = None
    if d.verdict == "halt":
        return d
    if d.verdict == "reduce":
        effective_qty = d.adjusted_qty or 0.0
        reduce_reason = d.reason

    # 8. Per-order at-risk capital (uses the possibly-reduced qty)
    d = symbol_order_caps.check_per_order_cap(
        intent_qty=effective_qty,
        intent_price=intent_price,
        intent_side=intent.side,
        stop_loss_price=stop_loss_price,
        account=account, limits=limits.order,
    )
    if d.verdict == "halt":
        return d

    # 9. Live-capital cap (paper bypassed via lock flag = false).
    #    Kept last so the residual-risk bound is the final word: even
    #    if every other check accepts, the live-cap halts an order
    #    whose strategy isn't authorised for live capital.
    d = live_capital.check_live_capital(
        live_capital_lock=policy.live_capital,
        strategy_id=intent.strategy_id,
        intent_side=intent.side,
        intent_notional=effective_qty * intent_price,
        account=account,
        positions=positions,
    )
    if d.verdict == "halt":
        # Paper mode is the default state of the lock; treat the
        # `disabled` reason as "skip the live check" rather than
        # blocking every paper order on the live cap.
        if "live_cap:disabled" not in (d.reason or ""):
            return d

    if reduce_reason is not None:
        return RiskDecision.reduce(reduce_reason, adjusted_qty=effective_qty)
    return RiskDecision.accept()


__all__ = ["evaluate"]
