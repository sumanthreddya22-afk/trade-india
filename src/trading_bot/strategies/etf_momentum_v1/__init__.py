"""ETF Momentum v1 — the seed thesis (Plan v4 §3, `docs/edge_thesis_v1.md`).

**Mechanism.** Cross-sectional momentum on diversified ETFs: rank a
small universe by 12-1 month total return (12-month return skipping
the most recent month, to avoid short-term reversal contamination),
hold the top N equally weighted, monthly rebalance.

**Why 12-1 month?** Long-horizon momentum has the strongest evidence
across asset classes (Asness-Moskowitz-Pedersen 2013) and the skip-the-
last-month version is the classic Jegadeesh-Titman form that excludes
the short-term-reversal effect. ETFs (not single stocks) avoid
survivorship bias and idiosyncratic blow-ups.

**Failure modes.** Underperforms in (a) sharp reversals where past
losers rally hard (post-COVID 2020, post-banking-crisis 2009), (b)
sustained low-vol regimes where momentum decays into noise. Kill
criteria live in `docs/edge_thesis_v1.md` (and in the registered
``strategy_version`` row).
"""
from __future__ import annotations

from trading_bot.strategies.etf_momentum_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, signal_fn,
)

__all__ = ["DEFAULT_PARAMS", "STRATEGY_ID", "UNIVERSE", "signal_fn"]
