"""Per-lane allocation + daily loss + lane status transitions.

Plan v4 §6: per-lane allocation ≤40%; per-lane daily loss ≤0.5%.
Plan v4 §7: lane status ∈ {research_only, shadow, tiny_paper,
scaled_paper, live, reduce_only, observe_only, halted}. A lane emits
orders only when its status is ``tiny_paper | scaled_paper | live``.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from trading_bot.risk.limits import LaneLimits
from trading_bot.risk.types import AccountState, Position, RiskDecision

# Lane states that may emit new orders.
ACTIVE_LANE_STATES = frozenset({"tiny_paper", "scaled_paper", "live"})

# Lane states that permit only exits.
EXIT_ONLY_LANE_STATES = frozenset({"reduce_only", "observe_only"})


def check_lane_status(
    *,
    lane: str,
    lane_caps_lock: Mapping,
    intent_side: str,
) -> RiskDecision:
    """Lookup the lane's current status from ``lane_caps.lock``. Reject
    new entries unless status is active; exits always pass.
    """
    lanes = lane_caps_lock.get("lanes", {})
    if lane not in lanes:
        return RiskDecision.halt(f"lane_status:unknown_lane:{lane}")
    status = lanes[lane].get("status", "halted")
    side = (intent_side or "").lower()
    is_exit = side in ("sell_to_close", "buy_to_close")
    if is_exit:
        return RiskDecision.accept()
    if status in ACTIVE_LANE_STATES:
        return RiskDecision.accept()
    if status in EXIT_ONLY_LANE_STATES:
        return RiskDecision.halt(f"lane_status:{lane}:{status}:entries_blocked")
    return RiskDecision.halt(f"lane_status:{lane}:{status}:no_orders")


def check_per_lane_allocation(
    *,
    lane: str,
    intent_notional: float,
    intent_side: str,
    account: AccountState,
    positions: Sequence[Position],
    limits: LaneLimits,
) -> RiskDecision:
    """Reject any entry that would push a lane over 40% of equity. Exits
    pass through. Reduce-to-fit is not applied here (the lane cap is
    aggregate; the kernel uses ``symbol_caps`` for per-name reduction).
    """
    side = (intent_side or "").lower()
    is_exit = side in ("sell_to_close", "buy_to_close")
    if is_exit:
        return RiskDecision.accept()
    equity = max(account.equity, 1.0)
    current_gross = sum(
        abs(p.market_value) for p in positions if p.lane == lane
    )
    proj = current_gross + intent_notional
    pct = proj / equity * 100.0
    if pct > limits.per_lane_allocation_max_pct:
        return RiskDecision.halt(
            f"lane_cap:{lane}:allocation "
            f"({pct:.2f}% > {limits.per_lane_allocation_max_pct:.2f}%)"
        )
    return RiskDecision.accept()


def check_per_lane_daily_loss(
    *,
    lane: str,
    lane_session_pnl_pct: float,
    limits: LaneLimits,
) -> RiskDecision:
    """Halt the lane for the session when realised + unrealised lane PnL
    drops below the -0.5% threshold.
    """
    if lane_session_pnl_pct <= -limits.per_lane_daily_loss_max_pct:
        return RiskDecision.halt(
            f"lane_cap:{lane}:daily_loss "
            f"({lane_session_pnl_pct:.2f}% <= "
            f"-{limits.per_lane_daily_loss_max_pct:.2f}%)"
        )
    return RiskDecision.accept()


def demote_on_breach(
    *,
    lane: str,
    breach_reason: str,
    lane_caps_lock_mutable: dict,
) -> Optional[str]:
    """Transition a lane from active to ``observe_only`` after a breach.

    Returns the new status or None if the lane was already non-active.
    This function mutates the *in-memory* lane lock — the persistent
    lock file is not modified (operator must re-issue a dated lock to
    restore the lane per Plan §7 transitions).
    """
    lanes = lane_caps_lock_mutable.get("lanes", {})
    if lane not in lanes:
        return None
    current = lanes[lane].get("status")
    if current not in ACTIVE_LANE_STATES:
        return None
    lanes[lane]["status"] = "observe_only"
    lanes[lane]["_demoted_at_runtime"] = True
    lanes[lane]["_demoted_reason"] = breach_reason
    return "observe_only"


__all__ = [
    "ACTIVE_LANE_STATES",
    "EXIT_ONLY_LANE_STATES",
    "check_lane_status",
    "check_per_lane_allocation",
    "check_per_lane_daily_loss",
    "demote_on_breach",
]
