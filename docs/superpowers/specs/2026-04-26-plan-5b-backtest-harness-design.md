# Plan 5b — Backtest Harness

**Status:** Design
**Date:** 2026-04-26
**Parent plan:** [2026-04-26-revised-plan-sequence.md](2026-04-26-revised-plan-sequence.md)
**Sequence note:** This replaces the original Plan 5b (dynamic sizing), which was paused and renumbered to 5d. Backtest is the gate for everything downstream — 5c exit rules, 5d sizing knobs, and 5f evolution all need backtest-derived metrics to be data-driven instead of guesses.

## TL;DR

Replay 2+ years of historical Alpaca bars through the **real** strategy + risk_manager + orchestrator code paths. Generate synthetic closed trades, compute per-strategy and per-regime metrics (win rate, profit factor, Sharpe, max DD, hold-period distribution). Output a markdown report and a SQLite table that the evolution loop can later consume.

If, after running, profit factor < 1.0 or Sharpe < 0.5 in the dominant regime, **the trader's central worry is empirically confirmed** and we should rework the rules before any further plan ships.

## Why

The trader's risk analysis flagged this as **Risk #1, severity Critical**: "Strategy parameters are empirically unvalidated." The bot has zero closed trades. Every threshold (`RSI 55-70`, `5% stop`, `10% take-profit`, `0.5% per-trade risk`, `$5 price floor`, `$10M ADV`, etc.) is a guess. Continuing to ship features on top of unvalidated rules amplifies an unknown.

Concrete examples of decisions that need backtest data before they're meaningful:
- Plan 5d's `conviction_floor`, `target_atr_pct`, `mult_ceiling` — currently educated guesses.
- Plan 5c's "trail to breakeven at +3%, trail at 50% of unrealized above breakeven at +5%" — values pulled from the spec without empirical grounding.
- Plan 5f's regime-conditional metrics need historical regime labels.
- The recently-shipped Phase 0a top-25-stocks cap, $10 price floor, $10M ADV — all guesses too.

## Non-goals

- This is **not** a full Monte-Carlo / forward-test framework. It's a pure historical replay.
- It does **not** model slippage, transaction costs, or market impact in v1. Acceptable for a long/short-horizon (days) signal validator. We add slippage modelling later if we move toward higher-frequency strategies.
- It does **not** simulate borrow / margin costs. Long-only, no shorting. Same as live bot.
- It does **not** simulate corporate actions (splits, dividends). Alpaca adjusts bars for splits already; dividend reinvestment is small enough to ignore at this signal horizon.
- It does **not** trigger any order, email, or external side effect. Pure-function except for the bar fetch and the result write.

## High-level design

### Pipeline

```
                ┌───────────────────────────────────────────────┐
                │  HistoricalBarStore (SQLite cache)            │
                │     ↓                                          │
                │  warm cache from Alpaca for symbol×date range │
                └───────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Backtester                                                         │
│                                                                     │
│  for each trading day in [from, to]:                                │
│      regime = regime.detect_regime_from_bars(spy_bars[..t])        │
│      strategy = strategy.strategy_for_regime(regime)               │
│      state = simulated RiskState (running daily/weekly P&L)        │
│      account, positions = simulated portfolio                      │
│                                                                     │
│      for each watchlist symbol:                                    │
│          bars = bar_store.get(symbol, end=t, lookback=60)          │
│          ind = compute_indicators(bars)                            │
│          signal = strategy.evaluate(symbol, ind, equity)           │
│          if signal.action == BUY:                                  │
│              order = build OrderRequest                            │
│              try: risk_manager.check(...)                          │
│              except RiskRuleViolation: log + skip                  │
│              else: open simulated position with bracket            │
│                                                                     │
│      for each open position:                                       │
│          next_bar = bar_store.get(symbol, t+1)                     │
│          if next_bar.low <= stop_price: close at stop              │
│          elif next_bar.high >= take_profit: close at TP            │
│          else: hold                                                │
│          (priority: stop wins on conflict — conservative)          │
│                                                                     │
│      record P&L delta, advance simulated clock                     │
│                                                                     │
│  return BacktestResult                                             │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
              ┌──────────────────────────────────┐
              │  metrics.py                       │
              │    win_rate, profit_factor,      │
              │    Sharpe, max_dd, hold dist     │
              │    sliced by:                     │
              │      strategy × regime           │
              │      asset_class                 │
              └──────────────────────────────────┘
                                ↓
              ┌──────────────────────────────────┐
              │  strategy/backtest_results.md   │
              │  data/backtest_trades.db        │
              └──────────────────────────────────┘
```

### Core modules (new)

```
src/trading_bot/backtest/
  __init__.py
  bar_store.py        — SQLite-backed historical bar cache; warms from Alpaca
  simulator.py        — the day-by-day replay loop (Backtester class)
  metrics.py          — per-strategy/per-regime aggregation
  reporter.py         — markdown report + DB writer
  __main__.py         — none (CLI added to trading_bot.cli instead)
```

CLI: `bot backtest --from YYYY-MM-DD --to YYYY-MM-DD [--symbols SYM,SYM] [--strategies momentum,mean_reversion]`.

### Reuse over rebuild

- **Use `strategy.MomentumStrategy.evaluate()` and `MeanReversionStrategy.evaluate()` unchanged.** They're already pure functions of (Indicators, equity).
- **Use `risk_manager.RiskManager.check()` unchanged.** It's already a pure function of (order, account, positions, state, regime).
- **Use `regime.detect_regime_from_bars()` unchanged.** Same pure function.
- **Use `market_data.compute_indicators()` unchanged.**

The new code is *only* the simulator's day-by-day clock, the bar cache, and the metric aggregation. No copy-pasting strategy logic — that's the whole point: validate the same code that runs in prod.

### Bar caching

Pulling 60-day-lookback bars × 25 symbols × 500 trading days = ~750k API calls per run is unworkable. Instead:

- One-time warm pass: `bar_store.warm(symbols, from_date, to_date)` fetches all bars up front via Alpaca's bulk historical endpoint (which supports `start`/`end` for full ranges in one call per symbol).
- Cache them in `data/backtest_bars.db` keyed by `(symbol, date)`.
- The simulator queries the cache with O(log n) lookups, not API calls.
- Cache is reusable across runs — first warm is slow (~2-5 min for 25 symbols × 2 years), subsequent runs are fast.
- Invalidation: TTL on the cache row's `cached_at` (default 24h). Backtests can run with `--no-refresh` to use cache regardless of age.

### Bracket-order simulation

Today's bot places bracket orders: entry + stop + take-profit. The simulator mirrors this conservatively:

- **Open**: at next-day `open` price (we computed signal off today's close, can't fill at today's close).
- **Stop**: position closes at `stop_price` if subsequent bar's `low <= stop_price`.
- **Take-profit**: position closes at `take_profit_price` if subsequent bar's `high >= take_profit_price`.
- **Conflict resolution**: if both legs hit on the same bar, **stop wins**. This is the conservative-bias choice — real-world execution depends on intra-bar ordering which we don't have, so we model the worse outcome.
- **Time-based fallback**: if neither leg hits within `--max-hold-days N` (default 60), close at that day's close. Captures dead-money trades. Configurable knob, candidate for tuning later.

### Regime over time

- Regime is recomputed each backtest day from SPY bars up to that day.
- The same `regime.detect_regime_from_bars` is called — including the new VIX override path.
- VIX historical data: FRED's `VIXCLS` series, fetched once at the start of the run, indexed by date. If VIX missing for a date, fall back to bars-only logic (matches live behavior).
- Regime labels are stored on each synthetic trade so the metrics layer can slice by regime.

### Risk-state evolution

`RiskManager.check` requires a `RiskState` (daily/weekly P&L, halt flag). In the backtest:

- Daily P&L is the sum of realized P&L from trades closed that simulated day.
- Weekly P&L rolls forward over the prior 5 trading days.
- `halted` triggers when daily ≤ -2% or weekly ≤ -5% (matches config). Once halted, the simulator skips all entries for the remainder of that day; halt clears at next day's start (this models the spec'd auto-clear in 5e and matches what live should do).
- `consecutive_losing_days` increments on a negative-P&L day, resets on positive.

This is the **same logic that 5e (halt + intraday) will implement live.** The backtest is also a forcing function for getting the halt logic right.

### Output

**`strategy/backtest_results.md`** (overwrite per run):

```
# Backtest Results

Generated: 2026-04-26T22:00:00Z
Range: 2024-01-01 → 2026-04-26  (582 trading days)
Symbols: SPY, QQQ, AAPL, MSFT, AMD, NVDA, ... (25)
Strategies: momentum, mean_reversion

## Headline

| Metric | momentum | mean_reversion | combined |
|---|---|---|---|
| Trades | 142 | 38 | 180 |
| Win rate | 58% | 47% | 55% |
| Profit factor | 1.42 | 0.91 | 1.28 |
| Sharpe (daily, ann.) | 0.84 | 0.21 | 0.71 |
| Max DD | -8.3% | -4.1% | -9.7% |
| Avg hold | 6.2d | 3.4d | 5.6d |

## Per-strategy × regime

| Strategy | Regime | Trades | Win % | PF | Sharpe |
|---|---|---|---|---|---|
| momentum | trending_up | 98 | 64% | 1.71 | 1.12 |
| momentum | sideways | 30 | 47% | 0.93 | 0.18 |
| momentum | risk_off | 14 | 36% | 0.62 | -0.34 |
| mean_reversion | trending_up | 4 | 25% | 0.41 | -0.88 |
| ... | ... | ... | ... | ... | ... |

## Acceptance gate

- Trade count per (strategy, dominant regime) ≥ 30:  YES / NO
- Profit factor ≥ 1.0 in dominant regime:  YES / NO
- Sharpe ≥ 0.5 in dominant regime:  YES / NO

If any NO → review parameters before shipping further plans.
```

**`data/backtest_trades.db`** — SQLAlchemy-managed SQLite table:

| col | type |
|---|---|
| id | int PK |
| run_id | str (UUID, lets us compare runs) |
| symbol | str |
| asset_class | str |
| strategy | str |
| regime_at_entry | str |
| entry_date | date |
| exit_date | date |
| hold_days | int |
| qty | Decimal |
| entry_price | Decimal |
| exit_price | Decimal |
| stop_price | Decimal |
| take_profit_price | Decimal |
| exit_reason | str ("stop" / "tp" / "time") |
| realized_pnl | Decimal |
| pnl_pct | float |
| equity_at_entry | Decimal |
| daily_pnl_pct_at_entry | float |
| reason | str (strategy reason at entry) |

Plan 5f (evolution loop upgrades) can later read this table directly — the evolution proposals already work off `ClosedTrade`-shaped data, so the backtest output and the live `closed_trades.db` end up structurally similar.

## Acceptance criteria

1. `bot backtest --from 2024-01-01 --to 2026-04-26` runs to completion without uncaught exceptions.
2. The cache warm-up writes ≥10 rows per symbol per quarter to `data/backtest_bars.db`.
3. ≥30 simulated trades per strategy per dominant regime in the headline output.
4. Computed metrics match by-hand spot checks on a known synthetic input (test fixture).
5. Re-running with the same parameters produces identical results (deterministic).
6. Re-running with cached bars takes <30s for a 2-year × 25-symbol run.
7. Trader's gate: profit factor and Sharpe in the dominant regime determine whether we proceed to 5c. We **do not silently ignore a fail** — the report flags it and the human decides.

## Risks during build

- **Bar cache size.** 25 symbols × ~600 days × 1 row each = 15k rows. Trivial. If we expand to crypto with intraday bars later, switch to Parquet partitions.
- **Crypto in backtest.** Crypto trades 24/7; the daily-bar simulator works on calendar days. We accept that crypto backtests use the date-only granularity (close-to-close), same as live. Acceptable for v1.
- **VIX historical gaps.** FRED occasionally returns "." for non-trading days. The pure-function regime detector already handles this fall-through to bars-only.
- **Earnings dates.** Pre-Plan 5c, no earnings exclusion. The backtest will include earnings-window entries, which will look noisier than live-with-5c eventually will. That's accurate to today's bot behavior.
- **Look-ahead bias.** Easy to introduce by accident. Test the simulator on a tiny canned dataset where the answer is hand-computable. Do this before trusting any large-scale numbers.

## Open questions for the brainstorming round

1. **Symbol set for v1.** Use the legacy 7 watchlist names + the top 25 from a recent rank? Or just SPY/QQQ + a few liquid singles? More symbols = more trades per regime but slower run.
   - *Recommendation:* SPY, QQQ, AAPL, MSFT, NVDA, AMD, GOOGL, META, AMZN, TSLA, plus BTC/USD, ETH/USD. 12 symbols, broad coverage, fast.
2. **Historical depth.** 2 years (2024-01 → present) gets one major regime change (the 2024 rally + chops); 4 years gets through the 2022 bear. Diminishing returns past 4 years for current macro relevance.
   - *Recommendation:* default 2 years; flag for `--from 2022-01-01` if more drawdown coverage wanted.
3. **Slippage knob.** Add a flat `--slippage-bps N` parameter (default 0)? Cheap to add and lets us stress-test sensitivity.
   - *Recommendation:* yes, default 0, exposed for sensitivity analysis.
4. **What does "dominant regime" mean for the gate?** Most-frequent regime in the backtest period, weighted by trading days?
   - *Recommendation:* weighted by trading days.
