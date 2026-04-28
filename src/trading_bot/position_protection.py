"""Open-position auto-protect — decides whether to place a protective stop or
market-flatten an unprotected open position, then carries out the action.

Triggered from cli.py:verify_stops every :20 / :50 of every hour.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal


def _decide(
    *, current_price: float, ema_20: float, stop_pct: Decimal,
) -> tuple[Literal["protect", "flatten"], float]:
    """Compute strategy-aligned protective stop and decide the action.

    Mirrors MomentumStrategy.evaluate's stop math:
        stop = max(ema_20, last_close * (1 - stop_pct))

    Returns ('protect', stop_level) when stop < current_price (position is
    above its protective floor), or ('flatten', stop_level) when the floor
    has already been crossed.
    """
    pct_stop = current_price * (1.0 - float(stop_pct))
    stop = max(ema_20, pct_stop)
    decision: Literal["protect", "flatten"] = (
        "protect" if stop < current_price else "flatten"
    )
    return decision, stop
