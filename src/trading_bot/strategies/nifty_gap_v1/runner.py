"""NIFTY Overnight Gap Fade v1 — research_only runner stub.

This strategy is **intraday** (entry at open, exit at close). The
existing daemon dispatch loop is EOD daily — it does not yet support
"submit at next open, cancel + sweep at next close" execution
mechanics. Until that ships, this runner returns ``StrategyDecision``
shells with no intents so the strategy passes registry validation and
shows up in the dashboard, but no orders are emitted.

The signal function (``signal.compute_gap_signal``) IS usable today:
  * Tier-1 backtests can replay it against historical OHLC bars and
    compute the realised return per signal day.
  * A future intraday dispatch loop can call it at 09:16 IST and
    submit an MIS (intraday) order with the appropriate side.

Lane status MUST remain ``research_only`` until both (a) Tier-1
validation passes and (b) intraday execution lands. Promotion to any
trading status before that is a kernel hard fail.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from trading_bot.strategies.nifty_gap_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE,
)

STRATEGY_VER = 1


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict = field(default_factory=dict)
    current_qty: dict = field(default_factory=dict)
    equity: float = 0.0
    intents: list = field(default_factory=list)
    universe: tuple[str, ...] = ()
    universe_payload: dict = field(default_factory=dict)


def should_rebalance_today(today: dt.date, last_date: Optional[dt.date]) -> bool:
    """Daily cadence — every NSE trading day produces a fresh signal."""
    return last_date is None or today > last_date


def evaluate_strategy(
    *,
    historical_db: Optional[Path] = None,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
) -> StrategyDecision:
    """Research_only stub: returns an empty decision.

    Real execution requires an intraday loop that doesn't exist yet
    in the daemon. Returning empty keeps the dispatcher happy and
    leaves the strategy visible in the registry / dashboard without
    submitting any orders.
    """
    return StrategyDecision(
        decision_date=decision_date or dt.date.today(),
        universe=UNIVERSE,
        universe_payload={
            "_status": "research_only",
            "_note": (
                "NIFTY_GAP_v1 is intraday; daemon dispatch is EOD. "
                "Signal is callable via "
                "trading_bot.strategies.nifty_gap_v1.compute_gap_signal "
                "for Tier-1 backtests."
            ),
        },
    )


__all__ = [
    "STRATEGY_ID", "STRATEGY_VER", "StrategyDecision",
    "evaluate_strategy", "should_rebalance_today",
]
