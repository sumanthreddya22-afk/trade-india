"""Historical Value-at-Risk and Expected Shortfall.

Pure-function risk metrics over a daily-return history. Used by:
- W2c risk gate: pre-trade ``portfolio_var_after`` check before order release
- Decision schema: ``risk_after.portfolio_var_after`` and
  ``risk_after.expected_shortfall_after`` populated for every order

Conservative by design: returns 0.0 for empty/thin samples (so the gate
doesn't false-positive on a fresh account), and the additive trade-VaR
combination is an upper bound (no correlation credit).
"""
from __future__ import annotations

from typing import Sequence


def historical_var(returns: Sequence[float], *, alpha: float = 0.05) -> float:
    """Historical Value-at-Risk at the 1−alpha confidence level.

    Returns the magnitude of the worst loss in the bottom ``alpha`` quantile
    of the return distribution, expressed as a non-negative fraction of
    equity.  E.g., ``alpha=0.05`` → 95% VaR.
    """
    if not returns:
        return 0.0
    sorted_r = sorted(returns)  # ascending: most-negative first
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0,1); got {alpha}")
    n = len(sorted_r)
    if n == 1:
        return max(0.0, -sorted_r[0])
    # Index of the alpha-quantile (0-indexed). Use floor so a tighter alpha
    # picks a more extreme loss.
    idx = int(alpha * n)
    if idx >= n:
        idx = n - 1
    quantile_return = sorted_r[idx]
    return max(0.0, -quantile_return)


def expected_shortfall(returns: Sequence[float], *, alpha: float = 0.025) -> float:
    """Expected Shortfall (CVaR) at the 1−alpha level.

    Average loss across the worst ``alpha`` fraction of the return
    distribution. Always ≥ ``historical_var`` for the same alpha (as the
    average of the tail is at least as bad as the tail boundary).
    """
    if not returns:
        return 0.0
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0,1); got {alpha}")
    sorted_r = sorted(returns)
    n = len(sorted_r)
    cutoff = max(1, int(alpha * n))
    tail = sorted_r[:cutoff]
    losses = [max(0.0, -r) for r in tail]
    if not losses:
        return 0.0
    return sum(losses) / len(losses)


def var_after_trade(
    *,
    current_var: float,
    trade_var_contribution: float,
) -> float:
    """Combine current portfolio VaR with a new trade's VaR contribution.

    Conservative additive upper bound — no correlation credit. This is the
    right default for a defensive gate: a real diversification-aware combiner
    would generally produce a smaller post-trade VaR, so anything that
    passes the additive gate is safe under the more accurate model too.
    """
    return float(current_var) + max(0.0, float(trade_var_contribution))
