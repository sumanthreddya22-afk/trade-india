"""Calibration math: drift detection between backtest predictions and paper realized P&L.

Spearman rank correlation, computed in pure numpy (no scipy dep). Returns
the correlation and a severity label per spec §7.6 Role 21.
"""
from __future__ import annotations

import numpy as np

INSUFFICIENT_DATA_THRESHOLD = 10  # below this n we don't compute corr at all


def _rank(values: list[float]) -> np.ndarray:
    """Average-tie ranks, like scipy.stats.rankdata default."""
    arr = np.asarray(values, dtype=float)
    order = arr.argsort()
    ranks = np.empty_like(arr)
    ranks[order] = np.arange(1, len(arr) + 1, dtype=float)
    # Resolve ties: average their ranks.
    sorted_arr = arr[order]
    i = 0
    while i < len(sorted_arr):
        j = i
        while j + 1 < len(sorted_arr) and sorted_arr[j + 1] == sorted_arr[i]:
            j += 1
        if j > i:
            avg = (i + j + 2) / 2.0  # ranks are 1-based
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1
    return ranks


def compute_drift_score(
    predicted_pnls: list[float], realized_pnls: list[float]
) -> tuple[float | None, str]:
    """Return (spearman_corr, severity).

    Severity policy (spec §7.6 Role 21):
      corr > 0.5    → "ok"
      0.3..0.5      → "warning"
      corr < 0.3    → "high"  (Calibrator halts Promoter for 7d)
      n < 10        → "insufficient_data"  (corr is None)
    """
    if len(predicted_pnls) != len(realized_pnls):
        raise ValueError("predicted and realized must be same length")
    n = len(predicted_pnls)
    if n < INSUFFICIENT_DATA_THRESHOLD:
        return None, "insufficient_data"

    rp = _rank(predicted_pnls)
    rr = _rank(realized_pnls)
    # Pearson on ranks == Spearman.
    corr = float(np.corrcoef(rp, rr)[0, 1])
    if np.isnan(corr):
        return 0.0, "high"

    if corr > 0.5:
        sev = "ok"
    elif corr >= 0.3:
        sev = "warning"
    else:
        sev = "high"
    return corr, sev
