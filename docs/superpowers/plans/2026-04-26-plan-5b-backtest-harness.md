# Plan 5b — Backtest Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay 2 years of historical Alpaca bars through the existing strategy + risk_manager + orchestrator code paths, producing per-strategy and per-regime trade-level metrics. Output `strategy/backtest_results.md` and `data/backtest_trades.db`.

**Architecture:** New package `src/trading_bot/backtest/`. Reuses `MomentumStrategy.evaluate`, `MeanReversionStrategy.evaluate`, `RiskManager.check`, `regime.detect_regime_from_bars`, and `market_data.compute_indicators` unchanged. New code is the historical bar cache, the day-by-day replay loop, and the metric aggregator.

**Tech Stack:** Python 3.11+, pydantic, pandas, sqlalchemy, pytest. No new deps.

**Reference spec:** [2026-04-26-plan-5b-backtest-harness-design.md](../specs/2026-04-26-plan-5b-backtest-harness-design.md)

**File map:**

- Create: `src/trading_bot/backtest/__init__.py`
- Create: `src/trading_bot/backtest/bar_store.py`
- Create: `src/trading_bot/backtest/simulator.py`
- Create: `src/trading_bot/backtest/metrics.py`
- Create: `src/trading_bot/backtest/reporter.py`
- Create: `tests/test_bar_store.py`
- Create: `tests/test_simulator.py`
- Create: `tests/test_metrics.py`
- Create: `tests/test_backtest_integration.py`
- Modify: `src/trading_bot/cli.py` — new `bot backtest` command
- Modify: `.gitignore` — add `data/backtest_*.db`

---

## Task 1 — Historical bar store (cache layer)

- [ ] Define `BacktestBar` dataclass: `(symbol, date, open, high, low, close, volume)`.
- [ ] Define `BarStore` class wrapping `sqlite:///data/backtest_bars.db`:
  - `__init__(db_path)` — creates the DB file + `bars` table with composite primary key `(symbol, date)`. Adds index on `(symbol, date)`.
  - `get(symbol, end_date, lookback_days)` → `pd.DataFrame` with the same shape as `MarketDataClient.get_daily_bars` returns (so the simulator can drop it straight into `compute_indicators`).
  - `warm(symbols, from_date, to_date, market: MarketDataClient, refresh: bool=False)` — for each symbol, fetches the full date range from Alpaca via `market.get_daily_bars`, upserts rows. Skips symbols whose `cached_at` is within 24h unless `refresh=True`.
  - `is_warm(symbol, from_date, to_date)` — quick check before the simulator starts.
- [ ] Tests in `tests/test_bar_store.py`:
  - Empty store returns empty DF.
  - Warm + get round-trip preserves OHLCV exactly.
  - Get respects `lookback_days` and `end_date` (no future leak).
  - Idempotent warm — running it twice doesn't duplicate rows.
  - `is_warm` returns False after >24h.
- [ ] Verify: `uv run python -m pytest tests/test_bar_store.py -q` passes.

---

## Task 2 — Backtest data model + persistence

- [ ] Define `BacktestTrade` dataclass mirroring `ClosedTrade` plus `run_id`, `regime_at_entry`, `exit_reason ("stop"/"tp"/"time")`, `equity_at_entry`, `daily_pnl_pct_at_entry`.
- [ ] Define `BacktestStore` (mirror `ClosedTradeStore`) — `data/backtest_trades.db`. Append-only by `(run_id, entry_order_id)`.
- [ ] Define `BacktestRun` summary dataclass: `run_id, generated_at, from_date, to_date, symbols, strategies_used, total_trades, errors`.
- [ ] Tests in `tests/test_simulator.py` (start of file):
  - Roundtrip: store + retrieve a single BacktestTrade.
  - Idempotent insert by `(run_id, entry_order_id)`.

---

## Task 3 — Simulator skeleton (no signal logic yet)

- [ ] Create `Backtester` class with constructor `(config, bar_store, settings, vix_series=None)`.
- [ ] Method `run(from_date, to_date, symbols, strategy_names) -> BacktestRunResult`:
  - Builds a `pd.DatetimeIndex` of trading days (Mon-Fri only; later refine with NYSE calendar if needed).
  - For each day: stub method `_step(date, ...)` returns `[]`. Just verifies the loop runs.
  - Returns a result with empty trade list.
- [ ] Test: `tests/test_simulator.py::test_simulator_runs_empty_loop` — runs from 2024-01-01 to 2024-01-10, returns 0 trades, no exceptions.

---

## Task 4 — Regime + indicator computation per step

- [ ] In `_step(date)`:
  1. Pull SPY bars up to `date` (via `bar_store.get`).
  2. Compute regime via `detect_regime_from_bars(spy_bars, vix=vix_at(date), vol_threshold_pct=cfg.regime.vol_threshold_pct)`.
  3. Pick strategy via `strategy_for_regime(regime)`.
- [ ] VIX historical: new helper `_load_vix_series(from_date, to_date)` that fetches FRED `VIXCLS` once, returns `dict[date, float]`. Cached for the run.
- [ ] Test: `_step` returns the right regime label across a synthetic up-then-crash bar sequence (use deterministic fixture).

---

## Task 5 — Simulated portfolio + open positions

- [ ] Define `_Position` internal struct: `(symbol, qty, entry_price, stop_price, tp_price, entry_date, regime, strategy_name, entry_order_id)`.
- [ ] Define `_PortfolioState` internal struct: `(equity, cash, positions: list[_Position], realized_pnl_today, daily_pnl_history: list[(date, pct)])`.
- [ ] Initial state: equity = `--starting-equity` (default $15,000), cash = equity, no positions.
- [ ] `_apply_entries(date, signals, portfolio)`: for each signal that passes risk_manager.check, open a position at *next-day open*. Decrement cash. Persistence: queue these for fill on `date+1`.
- [ ] `_apply_exits(date, portfolio, bar_store)`: for each open position, fetch the day's bar:
  - if `low <= stop`: close at stop, log `exit_reason="stop"`.
  - elif `high >= tp`: close at tp, log `exit_reason="tp"`.
  - elif `(date - entry_date).days >= max_hold_days` (default 60): close at close, log `exit_reason="time"`.
  - else: hold.
  - Realized P&L = `(exit - entry) * qty - fees`. Fees=0 for v1.
  - On close, append a `BacktestTrade` to the run's trade list.
- [ ] Update equity each day: `equity = cash + Σ(positions.market_value at today's close)`.
- [ ] Test: open a position, drive bars to hit stop, verify P&L = `(stop - entry) * qty`.
- [ ] Test: open a position, drive bars to hit tp, verify P&L positive and exit_reason="tp".
- [ ] Test: stop+tp on same bar → stop wins.
- [ ] Test: time-based exit fires after `max_hold_days`.

---

## Task 6 — Risk-state evolution (daily/weekly halt)

- [ ] In `_step(date)` after exits resolve:
  - Compute `daily_pnl_pct = realized_pnl_today / equity_at_open_of_day * 100`.
  - Roll into `weekly_pnl_pct` over a trailing 5-trading-day window.
  - If `daily_pnl_pct <= -daily_loss_limit_pct` or `weekly_pnl_pct <= -weekly_loss_limit_pct`: `halted=True` for the rest of that day; clears next day.
  - Track `consecutive_losing_days` (increment on negative day, reset on positive).
- [ ] When building the `RiskState` to pass into `RiskManager.check`, populate from the above.
- [ ] Test: simulate a -3% loss day, verify halt flag set, verify zero entries get accepted that day.

---

## Task 7 — Wire the strategy + risk loop

- [ ] In `_step(date)`:
  1. Iterate symbols.
  2. `bars = bar_store.get(symbol, end=date, lookback_days=60)`.
  3. If `len(bars) < MIN_BARS_FOR_INDICATORS` → skip.
  4. `ind = compute_indicators(bars)`.
  5. `signal = strategy.evaluate(symbol, ind, equity=portfolio.equity)`.
  6. If `signal.action == BUY`:
     - Build `OrderRequest` from signal.
     - Build `AccountSnapshot` mock from `portfolio`.
     - Try `RiskManager.check`; on `RiskRuleViolation` log+continue.
     - Otherwise, queue position to open at `date+1` open.
- [ ] Test: known-good fixture (SPY rising), verify ≥1 buy signal in expected window, verify position opened, verify exit recorded.

---

## Task 8 — Metric aggregator

- [ ] Create `metrics.py` with `compute_metrics(trades: list[BacktestTrade]) -> BacktestMetrics` returning:
  - per-strategy: `n, wins, losses, win_rate, gross_win, gross_loss, profit_factor, expectancy, sharpe_daily_ann, max_dd, avg_hold_days`.
  - per-(strategy, regime): same.
  - per-asset_class.
- [ ] Sharpe: from daily simulated equity series. `(mean_daily_return / std_daily_return) * sqrt(252)`. Returns None if <10 days.
- [ ] Max DD: from running equity peak. Reported as percent.
- [ ] Tests:
  - All-wins → profit_factor ≥ infinity flag (special value).
  - All-losses → profit_factor 0, win_rate 0.
  - Symmetric inputs → expectancy ≈ 0.
  - Sharpe matches by-hand calculation on canned series.

---

## Task 9 — Markdown reporter

- [ ] `reporter.write_markdown(result, metrics, path)` — overwrites `strategy/backtest_results.md`.
- [ ] Layout per spec: header with run-id, parameters; headline table; per-strategy×regime table; acceptance-gate row showing PASS/FAIL.
- [ ] Test: render against canned `BacktestRunResult` fixture, assert key strings present (e.g., "Profit factor", "Win rate", "Acceptance gate").

---

## Task 10 — CLI wiring

- [ ] Add `bot backtest` command to `cli.py`:
  - `--from YYYY-MM-DD` (default 2024-01-01)
  - `--to YYYY-MM-DD` (default today)
  - `--symbols SYM,SYM` (default the legacy 7 + AAPL/MSFT/NVDA/GOOGL/META/AMZN/TSLA)
  - `--strategies momentum,mean_reversion` (default both)
  - `--max-hold-days N` (default 60)
  - `--starting-equity N` (default 15000)
  - `--slippage-bps N` (default 0)
  - `--no-refresh` flag (skip cache warm)
- [ ] On run: warm cache → run simulator → compute metrics → write report → write trades DB.
- [ ] Echo summary to stdout and the path to the report.
- [ ] Test: `runner.invoke(["backtest", "--from", "2024-01-01", "--to", "2024-01-10", "--symbols", "SPY"])` exits 0 with mocked bar_store + alpaca.

---

## Task 11 — Integration test against real(ish) data

- [ ] `tests/test_backtest_integration.py`:
  - Build a 2-year synthetic SPY series (deterministic uptrend then crash then chop).
  - Pre-populate the `BarStore` with the synthetic bars.
  - Run `bot backtest --from --to --symbols SPY`.
  - Assert:
    - ≥1 trade closed.
    - At least one trade has `exit_reason="stop"` and one with `exit_reason="tp"` (or `"time"`).
    - Metrics report file written.
    - Trades DB contains rows with the right `run_id`.
- [ ] Run real backtest end-to-end: `uv run bot backtest --from 2024-01-01 --to 2026-04-26 --symbols SPY,QQQ`. Inspect output. Verify it matches expectations (no uncaught exceptions, ≥1 trade, metrics computed).

---

## Task 12 — Documentation + final polish

- [ ] Add a section to `docs/superpowers/specs/2026-04-26-revised-plan-sequence.md` noting Plan 5b is shipped + summarising the headline backtest metrics.
- [ ] Update `strategy/rules.md` with the first empirical-baseline metrics (per-strategy win/PF/Sharpe) — first time `rules.md` has data instead of placeholder text.
- [ ] Run full suite: `uv run python -m pytest -q` — all tests green.
- [ ] Code-review pass: superpowers:requesting-code-review.

---

## Verification gate

The trader's central acceptance criterion: **after the first real backtest run**, examine the headline output:

- If profit factor ≥ 1.0 AND Sharpe ≥ 0.5 in the dominant regime: the bot's core has empirical edge. Proceed to 5c (exit hardening), then 5d (data-tuned sizer), then onward.
- If profit factor < 1.0 OR Sharpe < 0.5: **do not silently ship 5c/5d.** Open the per-strategy×regime table, identify which slice fails (likely momentum-in-sideways or mean-reversion-in-trending). Adjust the entry rules in `strategy.py` (e.g. tighten the RSI band, add a regime gate) and re-run. Iterate until the acceptance gate passes or it's clear the rules are fundamentally wrong (in which case the next plan is "redesign strategy.py", not 5c).

This is the first time in the project we have a way to know — instead of guess — whether the bot is profitable. Treat the verdict seriously.

---

## Estimated size

Roughly twice the test surface of Plan 5a. Code volume: ~600-800 LOC across 5 new modules. Test volume: ~400-600 LOC. Estimated effort: 3-4 evenings of focused work, 1 of which is the warm-up + first meaningful run.
