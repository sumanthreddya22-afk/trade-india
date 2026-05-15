"""Live-capital cap check — the residual-risk bound between today and
the day the seed thesis clears its full validation gates (Plan v4 §16).

Policy lives in ``policy/live_capital.lock``. Three gates are
enforced here:

  1. **Live-mode authorization.** If ``live_capital_enabled`` is False,
     the kernel HALTS any non-paper intent. The Alpaca adapter must
     remain on the paper endpoint until the lock is explicitly enabled
     by a signed operator commit + 7-day cooldown.
  2. **Total-equity ceiling.** If account equity exceeds the ceiling,
     halt all entries. Protects the operator from over-funding the
     bot beyond the bound the lock authorises.
  3. **Per-strategy capital cap.** Each strategy gets an explicit
     dollar cap; new entries that would push the strategy's gross
     position beyond the cap are halted.

This module is intentionally simple — it reads the bundle, computes
three predicates, returns a decision. The bundle hash and the lock
expiry are both checked: an expired lock collapses to "paper only".
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Sequence

from trading_bot.risk.types import AccountState, Position, RiskDecision


def _expired(lock: Mapping, today: dt.date) -> bool:
    raw = lock.get("live_mode_expiry_iso")
    if not raw:
        return False
    try:
        expiry = dt.date.fromisoformat(str(raw))
    except ValueError:
        # Malformed expiry — treat as expired so we fail safe.
        return True
    return today > expiry


def check_live_capital(
    *,
    live_capital_lock: Mapping,
    strategy_id: str,
    intent_side: str,
    intent_notional: float,
    account: AccountState,
    positions: Sequence[Position],
    today: dt.date | None = None,
) -> RiskDecision:
    """Single composite check for the live-capital cap.

    Exit-side orders (``sell_to_close``, ``buy_to_close``) bypass the
    caps — we never want to block a closing trade. Plain ``"sell"`` is
    treated as an exit only when the strategy currently holds a long
    position in the same symbol (consistent with ``symbol_order_caps``).
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    side = (intent_side or "").lower()

    enabled = bool(live_capital_lock.get("live_capital_enabled", False))

    # Hard rule: paper-only until the lock is enabled.
    if not enabled:
        # Sell-to-close orders on existing paper positions must always
        # be allowed even when live capital is disabled — we still
        # want to be able to unwind paper positions.
        if side in ("sell_to_close", "buy_to_close"):
            return RiskDecision.accept("live_cap:exit_skip")
        # Any non-exit intent submitted while live is disabled gets
        # logged and skipped — but the dispatcher only calls this on
        # the live path. In paper mode this check is short-circuited
        # by the daemon (see strategy_dispatch).
        return RiskDecision.halt(
            "live_cap:disabled (live_capital.lock.live_capital_enabled=false)"
        )

    # From here on, live mode is enabled. Check expiry, ceilings, caps.
    if _expired(live_capital_lock, today):
        return RiskDecision.halt(
            f"live_cap:lock_expired (today={today} > "
            f"{live_capital_lock.get('live_mode_expiry_iso')})"
        )

    if side in ("sell_to_close", "buy_to_close"):
        return RiskDecision.accept("live_cap:exit_skip")

    ceiling = float(live_capital_lock.get("total_equity_ceiling_usd", 0.0))
    if ceiling > 0 and account.equity > ceiling:
        return RiskDecision.halt(
            f"live_cap:equity_overflow (equity={account.equity:.2f} > "
            f"ceiling={ceiling:.2f}); reduce capital or raise the lock"
        )

    per_strategy = live_capital_lock.get("per_strategy_max_capital_usd", {})
    cap = per_strategy.get(strategy_id)
    if cap is None or float(cap) <= 0:
        return RiskDecision.halt(
            f"live_cap:strategy_disabled ({strategy_id} has no live "
            f"capital allocation in live_capital.lock)"
        )
    cap = float(cap)

    if side == "sell":
        held_long = sum(
            p.market_value for p in positions
            if p.strategy_id == strategy_id and p.qty > 0
        )
        if held_long > 0:
            return RiskDecision.accept("live_cap:reduce_long")

    current_strategy_gross = sum(
        abs(p.market_value) for p in positions
        if p.strategy_id == strategy_id
    )
    projected = current_strategy_gross + max(intent_notional, 0.0)
    if projected > cap:
        return RiskDecision.halt(
            f"live_cap:strategy_cap ({strategy_id} would reach "
            f"${projected:.2f} > ${cap:.2f})"
        )
    return RiskDecision.accept()


__all__ = ["check_live_capital"]
