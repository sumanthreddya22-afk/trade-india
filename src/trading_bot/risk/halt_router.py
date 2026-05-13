"""Halt routing — translates active kills into a precheck halt verdict.

Plan v4 §6: any one of the eight kill switches firing halts all new
entries; existing positions can only be reduced.

``decide`` is the pure function the precheck calls before any other
gate. If any kill is active, the verdict is ``halt`` (with the active
detector list as the reason). Exit-side intents are passed through
because the plan explicitly preserves the right to manage existing
positions during a halt.
"""
from __future__ import annotations

from typing import Iterable

from trading_bot.risk.types import RiskDecision


def decide(
    *,
    active_kill_set: Iterable[str],
    intent_side: str,
) -> RiskDecision:
    """Single decision function used by ``risk.precheck.evaluate``."""
    actives = sorted(set(active_kill_set))
    side = (intent_side or "").lower()
    is_exit = side in ("sell_to_close", "buy_to_close")
    if not actives:
        return RiskDecision.accept()
    if is_exit:
        return RiskDecision.accept(
            f"halt_router:exit_passthrough (active={','.join(actives)})"
        )
    return RiskDecision.halt(
        f"halt_router:active_kills:{','.join(actives)}"
    )


__all__ = ["decide"]
