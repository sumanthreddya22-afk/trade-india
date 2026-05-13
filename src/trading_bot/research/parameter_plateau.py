"""Parameter-plateau coverage.

Plan v4 §13 Tier-1: "parameter plateau ≥ 25 % of swept range". Given a
mapping of parameter value → metric across a 1-D sweep, find the
largest contiguous region where the metric stays within ``tolerance``
of the maximum. Return its width as a fraction of the sweep.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PlateauResult:
    max_metric: float
    plateau_fraction: float
    """Largest contiguous window where metric ≥ max - tolerance, as a
    fraction of the full sweep length."""
    plateau_range: tuple[float, float]
    """The (low, high) parameter values bounding the plateau."""


def plateau_coverage(
    sweep: Mapping[float, float],
    *,
    tolerance: float = 0.05,
) -> PlateauResult:
    """``sweep`` is a dict ``{parameter_value: metric}``. Parameter values
    are sorted ascending; the plateau is the longest contiguous run of
    parameter values whose metric is within ``tolerance`` of the max.
    """
    if len(sweep) < 2:
        return PlateauResult(
            max_metric=float("nan"),
            plateau_fraction=0.0,
            plateau_range=(0.0, 0.0),
        )
    items = sorted(sweep.items())
    params = [k for k, _ in items]
    metrics = [v for _, v in items]
    max_m = max(metrics)
    floor = max_m - tolerance

    sweep_width = params[-1] - params[0]
    if sweep_width <= 0:
        return PlateauResult(
            max_metric=max_m, plateau_fraction=0.0,
            plateau_range=(params[0], params[0]),
        )

    best_lo, best_hi = params[0], params[0]
    best_width = 0.0
    run_start: int | None = None
    for i, m in enumerate(metrics):
        if m >= floor:
            if run_start is None:
                run_start = i
            lo, hi = params[run_start], params[i]
            width = hi - lo
            if width >= best_width:
                best_width, best_lo, best_hi = width, lo, hi
        else:
            run_start = None
    return PlateauResult(
        max_metric=max_m,
        plateau_fraction=best_width / sweep_width,
        plateau_range=(best_lo, best_hi),
    )


__all__ = ["PlateauResult", "plateau_coverage"]
