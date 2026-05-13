"""PDT (Pattern Day Trader) check.

Plan v4 §6 PDT note (verbatim):

  v2's "block the 4th day trade" was unsafe: a stop-loss or kill-switch
  exit might be the 4th day-trade, and forcing the bot to hold to avoid
  PDT trumps the protection it is supposed to provide. v3 inverts the
  rule: at entry time the risk kernel asks "if I had to exit this
  position later today via a stop or emergency, would that be the 4th
  day-trade in the rolling 5-business-day window?" If yes, the entry is
  blocked or routed to a non-day-tradable strategy. Once a position
  exists, ANY exit is permitted regardless of the day-trade count —
  PDT is treated as a softly-penalty restriction the operator accepts
  in exchange for honoring stops.

This module implements the entry-side check. The day_trade_count is
read from the Alpaca account endpoint (the bot does NOT maintain a
parallel count); the caller passes it via ``account.day_trade_count``.
"""
from __future__ import annotations

from typing import Mapping

from trading_bot.risk.types import AccountState, RiskDecision

DAY_TRADE_ROUND_TRIP_RISK = 1
"""Each entry has a worst-case round-trip cost of 1 day-trade
(buy + same-day sell). The check asks: would committing this round-trip
push the rolling counter past the threshold?"""


def check_pdt(
    *,
    intent_side: str,
    account: AccountState,
    pdt_lock: Mapping,
) -> RiskDecision:
    """Entry-only PDT gate.

    Exits ALWAYS pass — Plan v4 explicit rule. The check fires only when
    the account is below the equity boundary AND the requested entry's
    worst-case day-trade addition would tip the rolling counter over.
    """
    if pdt_lock.get("exit_policy", {}).get("exits_always_allowed", True):
        side = (intent_side or "").lower()
        is_exit = side in ("sell_to_close", "buy_to_close")
        if is_exit:
            return RiskDecision.accept("pdt:exit_always_allowed")

    equity_boundary = float(pdt_lock.get("equity_boundary_usd", 25000))
    threshold = int(pdt_lock.get("day_trade_threshold", 3))

    # If equity is above the boundary, PDT does not apply.
    if account.equity >= equity_boundary:
        return RiskDecision.accept("pdt:above_equity_boundary")

    projected_count = account.day_trade_count + DAY_TRADE_ROUND_TRIP_RISK
    if projected_count > threshold:
        return RiskDecision.halt(
            f"pdt_entry_block "
            f"(day_trade_count={account.day_trade_count}, worst-case "
            f"+{DAY_TRADE_ROUND_TRIP_RISK} would exceed threshold={threshold} "
            f"at equity ${account.equity:.0f} < boundary ${equity_boundary:.0f})"
        )
    return RiskDecision.accept()


__all__ = ["check_pdt"]
