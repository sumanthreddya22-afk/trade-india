# Phase 3 — Auto-Promote Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Lab process — a separate launchd-managed Python process that nightly runs Bayesian parameter search over the existing strategies, walk-forward backtests each variant against historical Polygon bars, ranks them by alpha-vs-SPY + Sortino + drawdown penalty, and atomically rewrites `paper_active.json` when the best variant clears all promotion gates.

**Architecture:** Third process under launchd alongside daemon and supervisor. Reads from `massive_grouped.db` (Polygon bars cached by Phase 1 Universe Curator) and `trade_journal.db` (placed-trade history); writes to `state.db` (`leaderboard`, `evolution_runs` tables) and atomically to `data/paper_active.json` when the auto-promote gate clears. Existing backtest harness (`src/trading_bot/backtest/simulator.py`, `metrics.py`) is the engine; Phase 3 adds the walk-forward wrapper, the optuna search loop, the SPY benchmark, and the promotion atomicity.

**Tech Stack:** Python 3.11, optuna 3.6+ (NEW dep), existing backtest infrastructure, SQLAlchemy 2.0 (state.db tables), Alembic (new migration).

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md) §7.6 (Tier 5 Lab roles) and §11 (Beat-SPY logic).

**Phase 3 explicitly EXCLUDES (deferred):**
- Strategy Coach + Hold-SPY Coordinator (Phase 4 — depends on Phase 3 leaderboard data)
- Strategy Architect + Code Reviewer (Phase 5 — Claude-generated new templates)
- Calibrator role (Phase 3.5 — needs at least 30 days of paper-trade history vs backtest predictions)
- New strategy templates beyond `MomentumStrategy` (Phase 5)

---

## File structure for Phase 3

### New files

```
src/trading_bot/
  lab.py                       # Lab process entrypoint (mirrors daemon.py shape)
  benchmark.py                 # SPY benchmark fetcher (Alpaca + yfinance fallback)
  walkforward.py               # Walk-forward backtest harness (6 folds default)
  fitness.py                   # Fitness function: alpha-vs-SPY + Sortino + DD penalty
  leaderboard.py               # Read/write state.db leaderboard rows
  promotion.py                 # Atomic write of paper_active.json + gate evaluation
  param_space.py               # Param search space declarations per strategy template
  roles/
    backtest_engineer.py       # Tier 5 role wrapping walkforward
    param_optimizer.py         # Tier 5 role with optuna integration
    promoter.py                # Tier 5 role with auto-promote logic

ops/launchd/
  com.bharath.trading.lab.plist

migrations/versions/
  002_leaderboard_and_evolution_runs.py   # autogen + tweak

tests/
  test_benchmark.py
  test_walkforward.py
  test_fitness.py
  test_leaderboard.py
  test_promotion.py
  test_param_space.py
  test_lab.py                  # lab entrypoint
  roles/
    test_backtest_engineer.py
    test_param_optimizer.py
    test_promoter.py
```

### Files modified

- `pyproject.toml` — add `optuna>=3.6.0`
- `src/trading_bot/state_db.py` — add `Leaderboard` and `EvolutionRun` ORM models
- `ops/install.sh` — load the lab plist alongside daemon + supervisor
- `ops/uninstall.sh` — unload the lab plist
- `data/paper_active.json` — daemon reloads on mtime change (already implemented in Phase 1)
- `src/trading_bot/strategy.py` — add `MomentumStrategy.from_params(dict)` classmethod for declarative construction (small)

### Files NOT modified

- All Phase 1 + Phase 2 daemon and supervisor code
- The backtest simulator and metrics modules (we wrap, not modify)

---

## Task 1: Add optuna dependency + Alembic migration

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/trading_bot/state_db.py` (add ORM models)
- Create: `migrations/versions/002_leaderboard_and_evolution_runs.py`

- [ ] **Step 1: Add optuna**

In `pyproject.toml` `[project]` `dependencies` array, add: `"optuna>=3.6.0"`. Run `uv lock`.

- [ ] **Step 2: Add ORM models**

Append to `src/trading_bot/state_db.py`:

```python
class Leaderboard(Base):
    __tablename__ = "leaderboard"
    id = Column(Integer, primary_key=True, autoincrement=True)
    template_name = Column(String(64), nullable=False, index=True)
    params_hash = Column(String(64), nullable=False, index=True)  # sha1 of canonical-json params
    params_json = Column(Text, nullable=False)
    alpha_vs_spy_x = Column(Float, nullable=False)               # 1.5 = 1.5x SPY return
    sortino = Column(Float, nullable=False)
    max_dd_pct = Column(Float, nullable=False)
    folds_passed = Column(Integer, nullable=False)
    folds_total = Column(Integer, nullable=False)
    fitness_score = Column(Float, nullable=False, index=True)    # composite, higher=better
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class EvolutionRun(Base):
    __tablename__ = "evolution_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    template_name = Column(String(64), nullable=False)
    n_trials = Column(Integer, nullable=False)
    best_fitness = Column(Float, nullable=True)
    best_params_hash = Column(String(64), nullable=True)
    auto_promoted = Column(Integer, nullable=False, default=0)   # 0/1 sqlite-friendly
    promotion_gate_pass = Column(Text, nullable=True)            # JSON of which gates passed
```

- [ ] **Step 3: Generate + verify migration**

```bash
uv run alembic -c migrations/alembic.ini revision --autogenerate -m "leaderboard and evolution_runs"
mv migrations/versions/*_leaderboard_and_evolution_runs.py migrations/versions/002_leaderboard_and_evolution_runs.py
uv run alembic -c migrations/alembic.ini upgrade head
sqlite3 data/state.db ".schema leaderboard"
sqlite3 data/state.db ".schema evolution_runs"
```

Expected: both tables present.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock src/trading_bot/state_db.py migrations/versions/002_leaderboard_and_evolution_runs.py
git commit -m "feat(plan-10): leaderboard + evolution_runs tables + optuna dependency"
```

---

## Task 2: SPY benchmark fetcher

**Files:**
- Create: `src/trading_bot/benchmark.py`
- Test: `tests/test_benchmark.py`

Returns SPY daily closes for a date range. Tries Alpaca first, falls back to yfinance (already in deps via `requests` based stooq fetch). Caches in a small SQLite table or JSON file under `data/`.

- [ ] **Step 1: Test**

```python
# tests/test_benchmark.py
import datetime as dt
from unittest.mock import patch, MagicMock
import pandas as pd
from trading_bot.benchmark import SpyBenchmark


def test_returns_dataframe_with_close_column(tmp_path):
    bench = SpyBenchmark(cache_path=tmp_path / "spy.parquet")
    fake_df = pd.DataFrame({
        "close": [100.0, 101.5, 99.8],
    }, index=pd.to_datetime(["2026-04-25", "2026-04-26", "2026-04-27"]))
    with patch.object(SpyBenchmark, "_fetch_alpaca", return_value=fake_df):
        df = bench.get(start=dt.date(2026, 4, 25), end=dt.date(2026, 4, 27))
    assert "close" in df.columns
    assert len(df) == 3


def test_falls_back_to_yfinance_on_alpaca_failure(tmp_path):
    bench = SpyBenchmark(cache_path=tmp_path / "spy.parquet")
    fake_df = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-04-25"]))
    with patch.object(SpyBenchmark, "_fetch_alpaca", side_effect=ConnectionError),\
         patch.object(SpyBenchmark, "_fetch_yfinance", return_value=fake_df):
        df = bench.get(start=dt.date(2026, 4, 25), end=dt.date(2026, 4, 25))
    assert len(df) == 1


def test_compute_period_return():
    df = pd.DataFrame({"close": [100.0, 110.0]}, index=pd.to_datetime(["2026-04-01", "2026-04-30"]))
    ret = SpyBenchmark.period_return(df)
    assert abs(ret - 0.10) < 1e-6
```

- [ ] **Step 2: Run, fail.**

- [ ] **Step 3: Implement**

```python
# src/trading_bot/benchmark.py
"""SPY benchmark prices. Used by fitness function for alpha calc.

Tries Alpaca daily bars first; falls back to stooq.com (free, no API key).
Caches the resulting close series at the given cache_path.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import requests


class SpyBenchmark:
    def __init__(self, *, cache_path: str | Path = "data/spy_benchmark.parquet"):
        self.cache_path = Path(cache_path)

    def get(self, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            df = self._fetch_alpaca(start=start, end=end)
        except Exception:
            df = self._fetch_yfinance(start=start, end=end)
        return df

    def _fetch_alpaca(self, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        from trading_bot.market_data import MarketDataClient
        from trading_bot.config import Settings
        client = MarketDataClient(Settings())
        bars = client.get_daily_bars("SPY", lookback_days=(end - start).days + 5)
        bars.index = pd.to_datetime(bars.index)
        return bars.loc[start.isoformat():end.isoformat(), ["close"]]

    def _fetch_yfinance(self, *, start: dt.date, end: dt.date) -> pd.DataFrame:
        # stooq fallback: free CSV at https://stooq.com/q/d/l/?s=spy.us&i=d
        url = f"https://stooq.com/q/d/l/?s=spy.us&i=d&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(resp.text))
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").rename(columns={"Close": "close"})[["close"]]
        return df

    @staticmethod
    def period_return(df: pd.DataFrame) -> float:
        if len(df) < 2:
            return 0.0
        return float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0)
```

- [ ] **Step 4: Run tests pass.**

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/benchmark.py tests/test_benchmark.py
git commit -m "feat(plan-10): SpyBenchmark fetcher with Alpaca + stooq fallback"
```

---

## Task 3: Walk-forward harness

**Files:**
- Create: `src/trading_bot/walkforward.py`
- Test: `tests/test_walkforward.py`

Wraps the existing `backtest/simulator.py`. Splits a year-range into 6 folds: 12-month train + 3-month test, walk forward by 3 months. Returns a list of `BacktestRunResult` (test windows only). No look-ahead — strategy params are fixed per fold; only the test window contributes to the metric.

- [ ] **Step 1: Test**

```python
# tests/test_walkforward.py
import datetime as dt
from unittest.mock import patch, MagicMock
import pytest
from trading_bot.walkforward import walk_forward_backtest, FoldDefinition


def test_walk_forward_returns_n_folds_results():
    """Six folds of (12mo train, 3mo test) walking quarterly forward."""
    start = dt.date(2024, 1, 1)
    end = dt.date(2026, 1, 1)
    folds = list(_default_folds(start=start, end=end, n_folds=6))
    assert len(folds) == 6
    # First fold: 2024-01..2024-12 train, 2025-01..2025-03 test
    assert folds[0].train_start == dt.date(2024, 1, 1)
    assert folds[0].test_end == dt.date(2025, 3, 31)


def _default_folds(start, end, n_folds):
    from trading_bot.walkforward import default_folds
    return default_folds(start=start, end=end, n_folds=n_folds)


def test_walk_forward_invokes_simulator_per_fold():
    with patch("trading_bot.walkforward._run_simulator") as mock_sim:
        mock_sim.return_value = MagicMock()  # BacktestRunResult stub
        results = walk_forward_backtest(
            template_name="momentum",
            params={"rsi_lower": 55, "rsi_upper": 70},
            start=dt.date(2024, 1, 1),
            end=dt.date(2026, 1, 1),
            n_folds=3,
        )
    assert len(results) == 3
    assert mock_sim.call_count == 3
```

- [ ] **Step 2: Run, fail.**

- [ ] **Step 3: Implement**

```python
# src/trading_bot/walkforward.py
"""Walk-forward backtest harness. Splits the date range into N folds,
each (train_window train, test_window test). Test windows do not overlap.

Returns one BacktestRunResult per fold (TEST window only — train data is used
only to inform position state at the start of each test window).

For Phase 3 the train window is informational; momentum strategy is fully
state-free so the test window result is what matters for fitness scoring.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from trading_bot.backtest.simulator import BacktestRunResult


@dataclass
class FoldDefinition:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def default_folds(*, start: dt.date, end: dt.date, n_folds: int = 6,
                   train_months: int = 12, test_months: int = 3) -> list[FoldDefinition]:
    """Returns N folds with 12mo train + 3mo test, walking forward by test_months."""
    folds: list[FoldDefinition] = []
    cursor = start
    for _ in range(n_folds):
        train_end = _add_months(cursor, train_months) - dt.timedelta(days=1)
        test_start = train_end + dt.timedelta(days=1)
        test_end = _add_months(test_start, test_months) - dt.timedelta(days=1)
        if test_end > end:
            break
        folds.append(FoldDefinition(
            train_start=cursor, train_end=train_end,
            test_start=test_start, test_end=test_end,
        ))
        cursor = _add_months(cursor, test_months)
    return folds


def _add_months(d: dt.date, months: int) -> dt.date:
    month_total = (d.year * 12) + (d.month - 1) + months
    new_year = month_total // 12
    new_month = (month_total % 12) + 1
    last_day = (dt.date(new_year, new_month, 28)).replace(day=28)
    # Cap day to month length
    next_month_first = (
        dt.date(new_year + 1, 1, 1)
        if new_month == 12
        else dt.date(new_year, new_month + 1, 1)
    )
    days_in_month = (next_month_first - dt.timedelta(days=1)).day
    return dt.date(new_year, new_month, min(d.day, days_in_month))


def _run_simulator(*, template_name: str, params: dict, fold: FoldDefinition) -> BacktestRunResult:
    """Hook point: invoke the existing simulator for one fold's test window.

    Phase 3 implementation: imports the existing harness, builds a strategy
    from params, runs simulator over fold.test_start → fold.test_end.
    """
    from trading_bot.backtest.simulator import run_simulation  # existing entry
    from trading_bot.strategy import MomentumStrategy  # extend with from_params later

    if template_name == "momentum":
        strategy = MomentumStrategy(**params)
    else:
        raise ValueError(f"Unknown template: {template_name}")

    return run_simulation(
        strategy=strategy,
        start=fold.test_start,
        end=fold.test_end,
    )


def walk_forward_backtest(
    *,
    template_name: str,
    params: dict[str, Any],
    start: dt.date,
    end: dt.date,
    n_folds: int = 6,
) -> list[BacktestRunResult]:
    folds = default_folds(start=start, end=end, n_folds=n_folds)
    results: list[BacktestRunResult] = []
    for fold in folds:
        result = _run_simulator(template_name=template_name, params=params, fold=fold)
        results.append(result)
    return results
```

NOTE: `run_simulation` is a thin wrapper that needs to exist in `backtest/simulator.py`. Inspect that file before writing tests; if no top-level `run_simulation` exists, either create it or adapt the imports here. The existing `BacktestRunResult` is the return type — verify by reading `backtest/simulator.py` first.

- [ ] **Step 4: Run tests pass.**

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/walkforward.py tests/test_walkforward.py
git commit -m "feat(plan-10): 6-fold walk-forward harness wraps existing simulator"
```

---

## Task 4: Fitness function

**Files:**
- Create: `src/trading_bot/fitness.py`
- Test: `tests/test_fitness.py`

Composes a single fitness score from `alpha_vs_spy_x`, `sortino`, and `max_dd_pct`. Higher is better. Promotion gate: `alpha_vs_spy_x >= 1.5 AND sortino >= 1.0 AND max_dd_pct <= 20.0`.

```python
fitness_score = alpha_vs_spy_x + 0.5 * sortino - 0.5 * max(0, max_dd_pct - 20) / 100
```

- [ ] **Step 1: Test**

```python
# tests/test_fitness.py
from trading_bot.fitness import compute_fitness, FitnessScore, promotion_gate_check


def test_compute_fitness_normal():
    score = compute_fitness(alpha_vs_spy_x=1.8, sortino=1.4, max_dd_pct=15.0)
    assert isinstance(score, FitnessScore)
    assert score.fitness_score > 0


def test_dd_penalty_kicks_in_above_20():
    s_under = compute_fitness(alpha_vs_spy_x=2.0, sortino=1.5, max_dd_pct=10.0).fitness_score
    s_over = compute_fitness(alpha_vs_spy_x=2.0, sortino=1.5, max_dd_pct=30.0).fitness_score
    assert s_over < s_under


def test_promotion_gate_pass():
    score = compute_fitness(alpha_vs_spy_x=1.6, sortino=1.1, max_dd_pct=18.0)
    assert promotion_gate_check(score) is True


def test_promotion_gate_fail_low_alpha():
    score = compute_fitness(alpha_vs_spy_x=1.4, sortino=2.0, max_dd_pct=10.0)
    assert promotion_gate_check(score) is False


def test_promotion_gate_fail_low_sortino():
    score = compute_fitness(alpha_vs_spy_x=2.0, sortino=0.5, max_dd_pct=10.0)
    assert promotion_gate_check(score) is False


def test_promotion_gate_fail_high_dd():
    score = compute_fitness(alpha_vs_spy_x=2.0, sortino=2.0, max_dd_pct=25.0)
    assert promotion_gate_check(score) is False
```

- [ ] **Step 2: Run, fail.**

- [ ] **Step 3: Implement**

```python
# src/trading_bot/fitness.py
"""Fitness scoring for backtested strategy variants."""
from __future__ import annotations
from dataclasses import dataclass


MIN_ALPHA_VS_SPY = 1.5
MIN_SORTINO = 1.0
MAX_DD_PCT = 20.0


@dataclass
class FitnessScore:
    alpha_vs_spy_x: float
    sortino: float
    max_dd_pct: float
    fitness_score: float


def compute_fitness(*, alpha_vs_spy_x: float, sortino: float, max_dd_pct: float) -> FitnessScore:
    dd_penalty = max(0.0, max_dd_pct - MAX_DD_PCT) / 100.0
    fitness = alpha_vs_spy_x + 0.5 * sortino - 0.5 * dd_penalty
    return FitnessScore(
        alpha_vs_spy_x=alpha_vs_spy_x, sortino=sortino,
        max_dd_pct=max_dd_pct, fitness_score=fitness,
    )


def promotion_gate_check(score: FitnessScore) -> bool:
    return (
        score.alpha_vs_spy_x >= MIN_ALPHA_VS_SPY
        and score.sortino >= MIN_SORTINO
        and score.max_dd_pct <= MAX_DD_PCT
    )
```

- [ ] **Step 4-5: Run + commit.**

```bash
git add src/trading_bot/fitness.py tests/test_fitness.py
git commit -m "feat(plan-10): fitness function + promotion gate (1.5x SPY, Sortino 1.0, DD 20%)"
```

---

## Task 5: Param space declarations

**Files:**
- Create: `src/trading_bot/param_space.py`
- Test: `tests/test_param_space.py`

Defines `param_space["momentum"]` as a dict mapping param name to (low, high, type/distribution) tuples. Optuna consumes this. Other templates added in Phase 5.

```python
# src/trading_bot/param_space.py
"""Param search spaces per strategy template. Phase 5's Strategy Architect
will populate this dict for new templates."""
from __future__ import annotations

# Tuple shape: (low, high, kind) where kind is "int" or "float"
PARAM_SPACE: dict[str, dict[str, tuple]] = {
    "momentum": {
        "rsi_lower": (50.0, 60.0, "float"),
        "rsi_upper": (65.0, 75.0, "float"),
        "ema_period": (15, 30, "int"),
        "stop_pct": (3.0, 7.0, "float"),
        "sentiment_floor": (-1.0, 0.0, "float"),
    },
}
```

Test: `test_momentum_space_complete` asserts the 5 expected keys; `test_unknown_template_returns_empty` asserts `PARAM_SPACE.get("unknown", {})` is empty.

```bash
git add src/trading_bot/param_space.py tests/test_param_space.py
git commit -m "feat(plan-10): momentum param search space"
```

---

## Task 6: Leaderboard read/write helpers

**Files:**
- Create: `src/trading_bot/leaderboard.py`
- Test: `tests/test_leaderboard.py`

Functions: `record_run(session, *, template, params, alpha, sortino, dd, folds_passed, folds_total) -> None`, `top_n(session, *, n) -> list[Leaderboard]`, `current_best(session) -> Leaderboard | None`. Tests use in-memory SQLite, verify ordering by `fitness_score DESC`.

Implementation pattern same as `state_hwm.py` (Phase 1 Task 5).

```bash
git add src/trading_bot/leaderboard.py tests/test_leaderboard.py
git commit -m "feat(plan-10): leaderboard read/write helpers (sorted by fitness_score)"
```

---

## Task 7: Promotion atomicity

**Files:**
- Create: `src/trading_bot/promotion.py`
- Test: `tests/test_promotion.py`

Functions: `should_promote(current_active_path, candidate) -> tuple[bool, dict]` (returns gate-pass dict for audit log), `promote_atomically(active_path, candidate) -> None` (write to `<path>.tmp`, fsync, rename — same pattern as heartbeat write). Tests use tmp_path fixtures; verify the `.tmp` file is gone after a successful promote, the active file contains the new params, and the gate check returns False when fitness regresses.

Critical: `should_promote` requires the candidate's fitness to **exceed the current active config's fitness by ≥ 10%** (delta gate from spec §7.6) — only meaningful improvements promote, prevents leaderboard noise.

```bash
git add src/trading_bot/promotion.py tests/test_promotion.py
git commit -m "feat(plan-10): atomic auto-promote with 10% improvement gate"
```

---

## Task 8: Backtest Engineer Role

**Files:**
- Create: `src/trading_bot/roles/backtest_engineer.py`
- Test: `tests/roles/test_backtest_engineer.py`

Tier 5 (lab). Wraps `walk_forward_backtest` + `compute_metrics`. `_do_work(ctx)` reads `ctx["template"]` and `ctx["params"]`, runs walk-forward, returns `{"folds": [...], "alpha_vs_spy_x": float, "sortino": float, "max_dd_pct": float, "folds_passed": int}`. Param Optimizer (Task 9) calls this per trial.

Charter:

```python
class BacktestEngineerRole(BaseRole):
    name = "backtest_engineer"
    tier = 5
    process = "lab"
    job_description = "Run 6-fold walk-forward backtest of a (template, params) variant. Returns fitness inputs."
    sla_seconds = 90
    upstream_roles: list[str] = []
    downstream_roles = ["param_optimizer", "promoter"]
```

Test mocks `walk_forward_backtest` to return three fake fold results; verifies role's outputs structure.

```bash
git add src/trading_bot/roles/backtest_engineer.py tests/roles/test_backtest_engineer.py
git commit -m "feat(plan-10): Backtest Engineer role wraps walk-forward harness"
```

---

## Task 9: Param Optimizer Role

**Files:**
- Create: `src/trading_bot/roles/param_optimizer.py`
- Test: `tests/roles/test_param_optimizer.py`

Tier 5. The optuna search loop. `_do_work(ctx)` reads `ctx["template"]` (default "momentum"), creates an optuna study with TPE sampler, runs N trials (default 100 — configurable, fewer for tests), each trial:
1. Samples params from `PARAM_SPACE[template]`
2. Invokes `BacktestEngineerRole.safe_run(ctx={"template": ..., "params": ...})`
3. Computes fitness via `compute_fitness(...)` from the role's outputs
4. Records the variant in `leaderboard` table
5. Returns the fitness_score as the optuna objective

After all trials: writes a row to `evolution_runs`; calls Promoter to evaluate the best variant.

Test mocks the BacktestEngineerRole to return canned fitness values, verifies optuna runs N trials and the leaderboard has N rows.

Charter:

```python
class ParamOptimizerRole(BaseRole):
    name = "param_optimizer"
    tier = 5
    process = "lab"
    job_description = "Bayesian search via optuna over template parameter space. Records each variant in leaderboard. Default 100 trials."
    sla_seconds = 4 * 60 * 60   # 4h budget
    upstream_roles = ["backtest_engineer"]
    downstream_roles = ["promoter"]
```

```bash
git add src/trading_bot/roles/param_optimizer.py tests/roles/test_param_optimizer.py
git commit -m "feat(plan-10): Param Optimizer role with optuna TPE search"
```

---

## Task 10: Promoter Role

**Files:**
- Create: `src/trading_bot/roles/promoter.py`
- Test: `tests/roles/test_promoter.py`

Tier 5. `_do_work(ctx)` reads the current `paper_active.json`, fetches the leaderboard's top-N, applies `should_promote` for each, and on first match: calls `promote_atomically`, writes an `evolution_runs` row, returns `{"promoted": True, "from": prev_version, "to": new_version, "fitness_delta": ...}`. If no candidate clears, returns `{"promoted": False, "best_fitness": ..., "current_fitness": ..., "reason": "..."}`.

Test fixtures: a tempdir paper_active.json + a leaderboard with one variant that beats current by 12%; verify the file is rewritten and the JSON is well-formed.

Charter:

```python
class PromoterRole(BaseRole):
    name = "promoter"
    tier = 5
    process = "lab"
    job_description = "Atomically rewrite paper_active.json when leaderboard top variant clears all gates by ≥ 10% delta vs current."
    sla_seconds = 30
    upstream_roles = ["param_optimizer"]
    downstream_roles: list[str] = []
```

```bash
git add src/trading_bot/roles/promoter.py tests/roles/test_promoter.py
git commit -m "feat(plan-10): Promoter role auto-rewrites paper_active on gate clear"
```

---

## Task 11: Lab process entrypoint

**Files:**
- Create: `src/trading_bot/lab.py`
- Test: `tests/test_lab.py`

Same shape as `daemon.py`: APScheduler in-process, signal handlers, structured logging, idempotent shutdown. Three jobs:

| Cron | Job | Role |
|---|---|---|
| 02:00 ET daily | param_search | ParamOptimizerRole.safe_run(ctx={"template": "momentum"}) |
| 02:45 ET daily | auto_promote | PromoterRole.safe_run(ctx={}) |
| 04:00 ET Mon-Fri (deferred to Phase 5) | template_propose | placeholder |

Phase 3 only wires param_search + auto_promote. The lab process reads the same `paper_active.json` on startup (mtime watch) — but never writes to it directly except via Promoter.

Add `bot lab` Click subcommand that delegates to `lab.main()` (mirrors Phase 2's `bot daemon`/`bot supervisor`).

Test: subprocess test similar to `test_integration_daemon.py` — boots lab for 5s, sends SIGTERM, asserts `lab_boot` event in logs.

```bash
git add src/trading_bot/lab.py tests/test_lab.py
git commit -m "feat(plan-10): lab process entrypoint with param_search + auto_promote jobs"
```

---

## Task 12: launchd plist for lab + install/uninstall update

**Files:**
- Create: `ops/launchd/com.bharath.trading.lab.plist`
- Modify: `ops/install.sh` (load lab plist alongside daemon + supervisor)
- Modify: `ops/uninstall.sh` (unload it)

Plist content mirrors `com.bharath.trading.daemon.paper.plist` but ProgramArguments is `python -m trading_bot.lab`. Same env vars + WorkingDirectory.

`plutil -lint` verification step.

```bash
git add ops/launchd/com.bharath.trading.lab.plist ops/install.sh ops/uninstall.sh
git commit -m "feat(plan-10): launchd plist + install/uninstall for lab process"
```

---

## Task 13: Phase 3 deployment dry run

**Files:** none (manual)

- [ ] **Step 1: Full suite passes**
  `uv run pytest tests/ 2>&1 | tail -3` — must show 303 + ~30 new = ~330+ passing.

- [ ] **Step 2: Lab module imports cleanly**
  `uv run python -c "from trading_bot import lab; from trading_bot.roles.backtest_engineer import BacktestEngineerRole; from trading_bot.roles.param_optimizer import ParamOptimizerRole; from trading_bot.roles.promoter import PromoterRole; print('imports ok')"`

- [ ] **Step 3: Apply migration**
  `uv run alembic -c migrations/alembic.ini upgrade head` — applies migration 002.

- [ ] **Step 4: Manual test of one optimizer trial**
  ```bash
  uv run python -c "
  from trading_bot.state_db import get_engine
  from trading_bot.roles.param_optimizer import ParamOptimizerRole
  engine = get_engine('data/state.db')
  role = ParamOptimizerRole(engine=engine)
  result = role.safe_run(ctx={'template': 'momentum', 'n_trials': 3})
  print(result.outputs)
  "
  ```
  Expected: completes in < 30s on cached bars, prints fitness values.

- [ ] **Step 5: Install lab plist + load**
  ```bash
  ops/install.sh
  launchctl list | grep com.bharath.trading
  ```
  Three processes alive: daemon, supervisor, lab.

- [ ] **Step 6: Verify lab waits idle until 02:00 ET**
  Lab process should be alive but mostly sleeping. Confirm with `ps aux | grep trading_bot.lab`.

- [ ] **Step 7: First overnight run**
  At 02:00 ET the next morning: param search runs, ~100 trials, leaderboard populates. At 02:45 ET: Promoter runs; if a variant clears the 10% delta gate, paper_active.json is rewritten and an "auto-promote notice" event lands in the lab's structured log. Daemon picks up the new config on next mtime-check.

---

## Acceptance criteria for Phase 3

The phase is shipped when:

1. `uv run pytest tests/` passes (Phase 2's 303 + ~30 new Phase 3 tests).
2. `state.db.leaderboard` and `state.db.evolution_runs` tables exist and accumulate rows from `param_optimizer` runs.
3. The first scheduled `param_search` (02:00 ET) completes within 4 hours and writes ≥50 rows to leaderboard.
4. The first scheduled `auto_promote` (02:45 ET) either rewrites `paper_active.json` (if a variant cleared the gate) or logs `no_promotion_no_candidate_above_gate`.
5. The daemon detects the rewritten `paper_active.json` (mtime change) and reloads its config without restart.
6. Three launchd-managed processes alive: daemon, supervisor, lab — all keep-alive on crash.
7. SPY benchmark fetcher returns valid daily closes (smoke-tested manually).
8. The Phase 3 final code review (run before deployment) finds no Critical issues.

---

## Notes on what's NOT in Phase 3

- **Strategy Coach** — Phase 4. Reads leaderboard's `paper_journal_30d_alpha_vs_spy_x` but Phase 3 only writes backtest fitness, not paper-journal alpha — Phase 4 adds the journal-alpha computation.
- **Hold-SPY Coordinator** — Phase 4. Activates when Strategy Coach signals fallback.
- **Strategy Architect (Claude template proposal)** — Phase 5. Phase 3 only searches over the existing `MomentumStrategy` template.
- **Code Reviewer (Claude AST/lookahead checker)** — Phase 5.
- **Calibrator** — Phase 3.5. Needs ≥30 days of paper-trade journal vs backtest predictions correlation.
- **30-day paper-trade gate** in Promoter — currently the promoter uses backtest fitness only. The bootstrap exception in spec §7.6 says the 30d paper-journal gate skips for the first 30 days; Phase 3 ships *only* the backtest gate, and Phase 3.5 adds the paper-journal gate once we have the data.
- **Real KPIs for Phase 2 roles** — Stock Scanner / Crypto Scanner / Universe Curator KPIs that depend on placed-trade data are not back-filled in this phase. They activate naturally as the daemon accumulates trades.
- **Tone Analyst** — Phase 5 (lab-side, depends on Strategy Architect).

When the 8 acceptance criteria hold, Phase 3 is shipped and the bot has an operational evolution loop. The leaderboard grows nightly; the paper config evolves toward higher fitness; you receive a notification email each time a promotion fires.
