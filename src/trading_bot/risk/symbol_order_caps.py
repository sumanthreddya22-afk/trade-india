"""Per-symbol and per-order caps + reduce-to-fit logic.

Plan v4 §6:
- Per-symbol gross ≤5%; on breach, **order qty reduced to fit**.
- Per-order at-risk capital ≤2%; on breach, **order rejected**.

The per-symbol check returns ``reduce`` (not ``halt``) when the order
would push the symbol over the cap — the kernel may submit a smaller
quantity. The per-order check returns ``halt`` because "at-risk
capital" is a function of the order's own qty + price, so reducing the
qty is the caller's responsibility (a smaller qty produces a different
at-risk capital).
"""
from __future__ import annotations

from typing import Sequence

from trading_bot.risk.limits import OrderLimits, SymbolLimits
from trading_bot.risk.types import AccountState, Position, RiskDecision


def check_per_symbol_cap(
    *,
    intent_symbol: str,
    intent_qty: float,
    intent_price: float,
    intent_side: str,
    account: AccountState,
    positions: Sequence[Position],
    limits: SymbolLimits,
) -> RiskDecision:
    """If the order would put this symbol over the 5% cap, reduce qty to
    the largest value that still fits. If even qty=0 fits (current gross
    already at cap) the order is halted.
    """
    side = (intent_side or "").lower()
    if side in ("sell_to_close", "buy_to_close"):
        return RiskDecision.accept("symbol_cap:exit_skip")
    # A plain ``"sell"`` is only an exit when we hold a long position
    # in this symbol. Without a prior long, ``"sell"`` is a sell-to-open
    # (short equity entry, or — for options — the wheel's short-put /
    # short-call entry). Those must go through the per-symbol cap.
    if side == "sell":
        held_long_qty = sum(
            p.qty for p in positions
            if p.symbol == intent_symbol and p.qty > 0
        )
        if held_long_qty > 0:
            return RiskDecision.accept("symbol_cap:exit_skip")

    if intent_price <= 0:
        return RiskDecision.accept("symbol_cap:no_price_skip")

    equity = max(account.equity, 1.0)
    current_gross = sum(
        abs(p.market_value) for p in positions
        if p.symbol == intent_symbol
    )
    requested_notional = intent_qty * intent_price
    cap_value = equity * (limits.per_symbol_gross_max_pct / 100.0)

    if current_gross >= cap_value:
        return RiskDecision.halt(
            f"symbol_cap:{intent_symbol} (current gross "
            f"{current_gross:.2f} already at cap {cap_value:.2f})"
        )
    headroom = cap_value - current_gross
    if requested_notional <= headroom:
        return RiskDecision.accept()
    # Reduce qty to fit. Floor at zero.
    adjusted_qty = max(0.0, headroom / intent_price)
    if adjusted_qty <= 0:
        return RiskDecision.halt(
            f"symbol_cap:{intent_symbol} (no headroom after reduction)"
        )
    return RiskDecision.reduce(
        f"symbol_cap:{intent_symbol} (reduced to fit {limits.per_symbol_gross_max_pct:.2f}% cap)",
        adjusted_qty=adjusted_qty,
    )


def check_per_order_cap(
    *,
    intent_qty: float,
    intent_price: float,
    intent_side: str,
    stop_loss_price: float | None,
    account: AccountState,
    limits: OrderLimits,
) -> RiskDecision:
    """At-risk capital = qty × |entry_price − stop_loss_price|.

    If no stop is provided, the worst-case "at-risk" is the full
    notional (we don't know where the stop will be). This check rejects
    any order whose at-risk capital exceeds the 2% cap.
    """
    side = (intent_side or "").lower()
    if side in ("sell_to_close", "buy_to_close"):
        return RiskDecision.accept("order_cap:exit_skip")

    equity = max(account.equity, 1.0)
    cap_value = equity * (limits.per_order_at_risk_max_pct / 100.0)

    if intent_price <= 0:
        return RiskDecision.accept("order_cap:no_price_skip")

    if stop_loss_price is None or stop_loss_price <= 0:
        at_risk = intent_qty * intent_price                # worst case
        reason_detail = "no stop provided; using full notional"
    else:
        at_risk = intent_qty * abs(intent_price - stop_loss_price)
        reason_detail = f"qty * |entry - stop| = {at_risk:.2f}"

    if at_risk > cap_value:
        return RiskDecision.halt(
            f"order_cap:per_order_at_risk "
            f"({at_risk:.2f} > {cap_value:.2f}); {reason_detail}"
        )
    return RiskDecision.accept()


__all__ = ["check_per_order_cap", "check_per_symbol_cap"]
