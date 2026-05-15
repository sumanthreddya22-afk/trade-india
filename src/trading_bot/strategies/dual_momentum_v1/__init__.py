"""Dual Momentum (SPY vs TLT) — Gary Antonacci's binary momentum.

**Mechanism.** Each rebalance, compare 90-day returns of SPY and TLT.
Hold whichever has the higher return at 100% weight. Equity proxy
(SPY) when stocks are working; bond proxy (TLT) when they aren't.
Cash isn't an option — the bond leg substitutes for cash with a
duration premium.

**Why this rather than ETF Momentum.** Single decision per rebalance,
no parameter grid. DSR doesn't suffer the multiple-testing penalty
that killed ETF Momentum's Tier-1. Antonacci's "Dual Momentum"
(2014) showed risk-adjusted outperformance vs both buy-and-hold SPY
and 60/40 portfolios over 1974-2013 — and the mechanism is robust
to the regime breakdowns that hurt single-asset momentum.

**Failure modes.** Whipsaw on regime transitions: SPY momentum dies
in March 2020 → algorithm rotates to TLT → April 2020 SPY rebounds
→ algorithm misses the V. The 90-day lookback dampens but doesn't
eliminate this.
"""
from __future__ import annotations

from trading_bot.strategies.dual_momentum_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, signal_fn,
)

__all__ = ["DEFAULT_PARAMS", "STRATEGY_ID", "UNIVERSE", "signal_fn"]
