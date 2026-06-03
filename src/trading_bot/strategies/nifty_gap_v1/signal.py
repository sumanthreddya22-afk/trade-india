"""NIFTY overnight gap fade — pure signal function.

The signal is deterministic and side-effect free. It takes the prior
close + today's open and returns one of three actions plus a target
direction. The runner is responsible for translating actions into
broker intents; the backtester replays the signal against historical
bars with the three-lens cost model.

Cost-of-trade gate: ``gap_threshold_pct`` defaults to 0.50% so the
round-trip cost (STT 0.025% × 2 + stamp 0.003% + exchange 0.00325% × 2
+ SEBI 0.0001% × 2 + GST 18% on (exchange+SEBI), all on intraday MIS
delivery) of ~0.06-0.08% leaves room for a meaningful fade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

STRATEGY_ID = "NIFTY_GAP_v1"

# Universe is a single ticker. Kept as a tuple for API parity with
# other strategies; the dispatch loop iterates regardless of length.
UNIVERSE: tuple[str, ...] = ("NIFTYBEES",)

ActionT = Literal["fade_up", "fade_down", "flat"]


@dataclass(frozen=True)
class GapSignal:
    """One day's signal output."""

    action: ActionT
    gap_pct: float
    """Today's open minus yesterday's close, divided by yesterday's
    close, in percent. Positive = gap up. Always populated, even when
    the action is ``flat`` (useful for monitoring distributions)."""


# Hyperparameters. The mutation engine (Phase 6) walks the threshold
# grid across {0.30, 0.40, 0.50, 0.60, 0.75, 1.00} during search;
# default is the mid-point that comfortably clears round-trip cost.
DEFAULT_PARAMS = {
    "gap_threshold_pct": 0.50,
    "_gap_threshold_pct_grid": (0.30, 0.40, 0.50, 0.60, 0.75, 1.00),
}


def compute_gap_signal(
    *,
    prior_close: float,
    today_open: float,
    gap_threshold_pct: float = DEFAULT_PARAMS["gap_threshold_pct"],
) -> GapSignal:
    """Return today's fade signal.

    ``fade_up`` → sell at open, cover at close (gap was up; we expect
    fade down).
    ``fade_down`` → buy at open, sell at close (gap was down; we
    expect bounce up).
    ``flat`` → no action; the gap is too small to overcome round-trip
    cost.
    """
    if prior_close <= 0:
        # Defensive: bad data → no signal.
        return GapSignal(action="flat", gap_pct=0.0)
    gap_pct = (today_open - prior_close) / prior_close * 100.0
    if gap_pct >= gap_threshold_pct:
        return GapSignal(action="fade_up", gap_pct=gap_pct)
    if gap_pct <= -gap_threshold_pct:
        return GapSignal(action="fade_down", gap_pct=gap_pct)
    return GapSignal(action="flat", gap_pct=gap_pct)


__all__ = [
    "ActionT", "DEFAULT_PARAMS", "GapSignal", "STRATEGY_ID", "UNIVERSE",
    "compute_gap_signal",
]
