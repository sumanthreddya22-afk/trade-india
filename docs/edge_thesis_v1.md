---
thesis_id: edge_thesis_v1
version: 1
authored_at: 2026-05-13
status: research_only
authored_by: solo-operator
sources:
  - Moskowitz, Ooi, Pedersen (2012), "Time Series Momentum"
  - Hurst, Ooi, Pedersen (2017), "A Century of Evidence on Trend-Following"
---

# Edge Thesis v1 — Cross-Asset Time-Series Momentum on Liquid ETFs

## Hypothesis (falsifiable, one sentence)

Cross-asset time-series momentum on liquid ETFs persists at monthly-to-quarterly
horizons after costs, because it is a behavioural premium (slow institutional
reallocation and trend-confirming flow) and not a risk premium; it is harvestable
by a small account because the holding period is long enough that Alpaca's modelled
execution costs and the PDT rule do not bind, and the universe is shallow enough to
size correctly.

## Universe

Ten ETFs, all options-eligible, tight spreads, no leveraged / inverse, no single-stock
idiosyncratic risk:

| Symbol | Asset class | Why |
|---|---|---|
| SPY | US equity | broad-market beta benchmark |
| QQQ | US equity | growth / large-cap tech |
| IWM | US equity | small-cap |
| EFA | Intl equity | developed ex-US |
| EEM | Intl equity | emerging markets |
| TLT | Govt bonds | long duration |
| IEF | Govt bonds | intermediate duration |
| GLD | Commodity | gold |
| DBC | Commodity | broad commodities basket |
| VNQ | Real estate | US REITs |

## Signal

12-month total return minus 1-month skip; **sign-only** (long if positive, flat if
negative; long-only at seed). Dividend- and split-adjusted series mandatory.
Adjustment basis: Alpaca corporate-actions ingest, cross-checked nightly against a
second source — mismatch halts the lane until reconciled. Rebalance monthly on the
first US trading day after the 16:00 ET close.

## Position sizing

Equal-risk via 20-day realized vol; cap **15 % gross per name**, **60 % gross
total**. Long-only at seed. Short sleeve gated by a separate short-sleeve
readiness checklist (borrow availability, locate API, hard-to-borrow rates) under
its own signed lock; phase numbering is not the gate.

## Cost lens

Midpoint-relative, parameterized in `policy/cost_model.lock` (lands Phase 3). All
backtests report three lenses — raw mid-to-mid, broker-paper, pessimistic — but
**only the pessimistic lens is the gate** for the validation policy thresholds in
Section 4. See Plan v4 §9.

## Kill criteria (any one trips → strategy halted; new validation lock required to resume)

1. 24-month rolling net Sharpe < 0 at any monthly checkpoint.
2. Deflated Sharpe Ratio < 0.30 on the latest walk-forward fold.
3. Asset-class concentration breach: any single asset class
   (US equity, intl equity, govt bonds, commodity, real estate) > 60 % gross at any
   rebalance over backtest history.

## Expected behaviour

| Regime | Expected |
|---|---|
| Strong trends (Q1 2017, 2019–2020 rebound, 2023) | positive expectancy |
| Choppy / mean-reverting (2015, 2018 Q4) | drawdowns |
| Sharp reversals | flat-to-down |

Not tuned to historical drawdowns. The thesis is intentionally not LLM-discovered;
its priors come from peer-reviewed factor research (Moskowitz/Ooi/Pedersen,
Hurst/Ooi/Pedersen).

## Multi-hypothesis policy

The Research Factory (Plan v4 §8) may explore parameter / feature / universe
variants of this thesis in sandbox **in parallel**. The production strategy
registry holds **exactly one active thesis** at a time. A second thesis is added
only after the first either passes paper-to-live promotion **or** is killed.

## Promotion gate

This thesis must pass each of the three validation tiers in Plan v4 §4 before
any capital is committed:

1. **Research candidate** (sandbox → shadow): DSR ≥ 0.50, PBO ≤ 0.50, ≥ 5 walk-forward folds.
2. **Paper candidate** (shadow → tiny/scaled paper): DSR ≥ 0.70, PBO ≤ 0.35, ≥ 6 folds, ≥ 504 OOS days, ≥ 50 trades-per-regime.
3. **Live candidate** (paper → live): DSR ≥ 0.85, PBO ≤ 0.25, ≥ 365 days paper observation, ≥ 12 monthly rebalance events, max paper drawdown ≤ 6 %, excess over SPY total return ≥ 1.5 % annualised net of costs, operator sign-off.

## How this document is enforced

- The `strategy_registry` row (Phase 4) for any strategy implementing this thesis
  must reference `thesis_id: edge_thesis_v1`.
- This file is included in `policy/HASHES`; the L0 governance loader (Phase 2)
  refuses to start the kernel on hash mismatch.
- Changes to this file create a new versioned file (`edge_thesis_v2.md`), they
  do not mutate this one. The bot can never silently change its own thesis.
