# Edge thesis v2 — Mean Reversion

> **DRAFT — operator must review and edit before promotion.** This file is a
> realistic starting draft for the second lane, not a validated thesis. Until
> it is signed off and the strategy passes Tier-1 validation, the lane stays
> in `research_only` and nothing trades.

## Hypothesis

Short-horizon (3–10 trading day) prices of large-cap US equities
overshoot relative to a slow trend after concentrated retail or news-driven
flow. Buying when the z-score of close vs the 50-day rolling mean is more
than 2 standard deviations *below* the mean (and selling the mirror case)
captures the mean-reverting component of the overshoot.

## Mechanism (causal story)

- **Why does this edge exist?** Forced selling from passive flows, ETF
  rebalancing, and retail panic creates short-lived liquidity demand that
  market-makers absorb at a discount. The discount unwinds as discretionary
  capital arrives over the following days.
- **Who's on the other side?** Liquidity providers want the spread; they're
  paid through the mean reversion. We compete with them.
- **Why does retail / passive flow give an edge to us specifically?** It
  doesn't, in steady state. The edge is in the *gap* between when forced
  flow hits and when discretionary capital responds. That gap is shrinking
  over time as more retail goes algorithmic — so this thesis must be
  re-validated at least quarterly.

## Universe

- Top 100 by market cap of the S&P 500, daily bar liquidity > $50M average
  20-day dollar volume.
- Exclude single-name stocks under earnings blackout (3 days pre, 1 day
  post).
- No leveraged ETFs.

## Signal definition (precise)

```
mu     = 50-day rolling mean of daily close
sigma  = 50-day rolling stdev of daily close
z      = (close - mu) / sigma

LONG entry  : z < -2.0 AND z prev > -2.0  (downward crossing)
LONG exit   : z >= 0.0 OR holding period >= 10 bars
SHORT entry : z >  2.0 AND z prev <  2.0  (upward crossing)
SHORT exit  : z <= 0.0 OR holding period >= 10 bars

Sizing      : 1% of equity per signal, capped at per_symbol cap.
```

## Expected regimes

- **Works in:** quiet-vol regimes (VIX < 25), no Fed surprises, normal
  earnings cycles.
- **Breaks in:** trending regimes (post-Fed-pivot, post-pandemic rebound),
  individual-name regime changes (acquisition rumors, fraud disclosures).
- **Historical cohorts:** worked 2010–2018; broke spectacularly in
  Q1 2020 (COVID), recovered late 2020; degraded 2021–2023 (everything
  trending), partial recovery 2024–2025.

## Kill criteria

The strategy must be retired (lane → `halted`) if any of:

- Sharpe ratio over a 6-month rolling window falls below 0.3
- Max drawdown exceeds 8% of allocated capital
- Win rate falls below 45%
- Correlation to ETF_MOMENTUM_v1 (seed thesis) sustained above 0.7 for
  30 trading days
- Any single-name realised slippage exceeds 3× model for 5 consecutive
  fills (drift_monitor will catch this automatically)

## Cost assumptions

- Slippage (pessimistic lens): +5 bps per side (matches stocks lock)
- SEC §31 + FINRA TAF: per cost_model.lock (sells only)
- No borrow cost yet (assume long-only Tier-1; shorts deferred to
  `short_policy.lock` Phase)
- Pessimistic-lens additive overhead: +5 bps per round-trip on top of
  cost model

## Falsification plan

- **Tier-1 backtest:** 2010-01-01 to 2025-12-31 daily bars, walk-forward
  with 5 folds × 252 trading days, 30% locked holdout.
- **Tier-2 paper:** 60 calendar days + 25+ trades min, run alongside the
  seed thesis.
- **Tier-3 live:** Phase 9 ramp checklist.
- **Acceptance:**
  - Tier-1: DSR ≥ 0.50 AND PBO ≤ 0.50 (per validation_policy.lock)
  - Tier-2: same gates + zero `drift_monitor` breaches
  - Tier-3: human-signed promotion_packet

## Operator sign-off

- Author: _____ (operator: this is a draft, you must own it)
- Date:   _____
- Git commit: _____

---

**Until this file is signed and the search_space_v2.json is hashed in
`policy/HASHES`, this lane stays in `research_only`. The daemon will not
allow this strategy to emit orders.**
