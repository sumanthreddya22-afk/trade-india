# Phase 3.5 — Calibrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Calibrator lab role (Role 21 from spec §7.6). Daily compares the active config's most recent backtest fold (predicted per-trade P&L) against actual paper-trade outcomes; writes a Spearman rank correlation drift score to `state.db`; halts the Promoter for 7 days when correlation falls below 0.3 — guards against the case where the backtest model has decoupled from real conditions.

**Architecture:** New role under the lab process. Reads from `trade_journal.db` (paper trade outcomes — schema already exists from Phase 1) and `state.db.leaderboard` (predicted per-trade P&L from the active config's most recent walk-forward fold). Writes to a new `calibration_runs` table. On HIGH-severity drift (corr < 0.3), writes a `promoter_halt_until` row to `state.db` that the Promoter checks before each run.

**Activation gate:** Calibrator returns `insufficient_data` when fewer than 10 trades have closed in the rolling 30-trade window. This means it's a no-op until paper trades accumulate — expected and intended.

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md) §7.6 Role 21.

---

## File structure for Phase 3.5

### New files
```
src/trading_bot/
  calibration.py                 # Spearman corr + drift policy
  roles/calibrator.py            # Tier 5 role wrapping calibration

migrations/versions/
  003_calibration_runs.py        # autogen + tweak

tests/
  test_calibration.py
  roles/test_calibrator.py
```

### Files modified
- `src/trading_bot/state_db.py` — add `CalibrationRun` + `PromoterHalt` ORM models
- `src/trading_bot/lab.py` — schedule a `calibrate` job at 05:00 ET daily
- `src/trading_bot/roles/promoter.py` — check `promoter_halt_until` before promoting

---

## Task 1 — ORM + migration

**Files:** `state_db.py`, `migrations/versions/003_calibration_runs.py`

- [ ] **Step 1: Add ORM models**

```python
class CalibrationRun(Base):
    __tablename__ = "calibration_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    template_name = Column(String(64), nullable=False)
    n_trades = Column(Integer, nullable=False)
    spearman_corr = Column(Float, nullable=True)   # null when n < 10
    severity = Column(String(16), nullable=False)  # ok | warning | high | insufficient_data


class PromoterHalt(Base):
    __tablename__ = "promoter_halts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    halted_until = Column(DateTime(timezone=True), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    set_by = Column(String(64), nullable=False)    # always "calibrator" for Phase 3.5
    set_at = Column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Generate + apply migration**

```bash
uv run alembic -c migrations/alembic.ini revision --autogenerate -m "calibration_runs and promoter_halts"
mv migrations/versions/*_calibration_runs_and_promoter_halts.py migrations/versions/003_calibration_runs.py
uv run alembic -c migrations/alembic.ini upgrade head
```

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(plan-3.5): calibration_runs + promoter_halts tables"
```

---

## Task 2 — Calibration math (`calibration.py`)

**Files:** `src/trading_bot/calibration.py`, `tests/test_calibration.py`

`compute_drift_score(predicted_pnls, realized_pnls) -> tuple[float | None, str]` — returns (corr, severity). Returns (None, "insufficient_data") when n < 10.

Severity policy (spec §7.6):
- corr > 0.5 → "ok"
- 0.3 ≤ corr ≤ 0.5 → "warning"
- corr < 0.3 → "high"
- n < 10 → "insufficient_data"

Spearman rank corr via `scipy.stats.spearmanr` if scipy is available, else hand-rolled rank correlation (avoid extra dep if numpy is enough — `numpy.corrcoef` on rank arrays is equivalent).

- [ ] Test: 4 cases — perfect monotonic, perfect inverse, no correlation, insufficient_data.
- [ ] Implement using rank-based correlation in pure numpy (no scipy dep).
- [ ] Commit.

---

## Task 3 — Calibrator role (`roles/calibrator.py`)

**Files:** `src/trading_bot/roles/calibrator.py`, `tests/roles/test_calibrator.py`

Tier 5 lab role. `_do_work(ctx)`:
1. Reads the active config from `paper_active.json` to get `template_name`.
2. Reads the latest leaderboard row for that template — its fold-by-fold per-trade predicted P&L lives in `params_json` (it doesn't currently — see Task 5).
3. Reads paper trades from `trade_journal.db` for the last 30 trades or 30 days, whichever is fewer.
4. Calls `compute_drift_score(predicted, realized)`.
5. Writes a `CalibrationRun` row.
6. If severity == "high": writes a `PromoterHalt` row with `halted_until = now + 7d`, reason = the corr value.
7. Returns `{"corr": float | None, "severity": str, "n_trades": int}`.

Charter:
```python
class CalibratorRole(BaseRole):
    name = "calibrator"
    tier = 5
    process = "lab"
    job_description = "Daily Spearman corr of backtest predicted vs paper realized P&L. Halts Promoter on severe drift."
    sla_seconds = 60
    upstream_roles = ["param_optimizer"]
    downstream_roles = ["promoter", "reporter"]
```

- [ ] Tests with mocked trade_journal + leaderboard (fixtures).
- [ ] Implement.
- [ ] Commit.

---

## Task 4 — Promoter respects halt window

**Files:** `src/trading_bot/roles/promoter.py`, `tests/roles/test_promoter.py`

Modify `PromoterRole._do_work` to check `PromoterHalt.halted_until > now()` at the start. If halted: return `{"promoted": False, "reason": "halted_by_calibrator", "halted_until": ...}`.

- [ ] Add a test case to `test_promoter.py`: leaderboard has a winner, but a PromoterHalt row exists → no promotion.
- [ ] Implement the gate.
- [ ] Commit.

---

## Task 5 — Backfill predicted P&L from BacktestEngineerRole

**Files:** `src/trading_bot/roles/backtest_engineer.py`, `src/trading_bot/leaderboard.py`

For Phase 3.5 to have anything to compare against, the BacktestEngineer must record per-trade predicted P&L from the most recent fold. Currently it only records aggregate metrics. Extend:

- The role's outputs gain `"per_trade_predictions": [{"date": ..., "symbol": ..., "predicted_pnl": float}, ...]` from the last (most recent test-window) fold's `result.trades`.
- `leaderboard.record_run(...)` accepts an optional `per_trade_predictions` arg, stored as a JSON column `per_trade_predictions_json` on the Leaderboard row.

This is a small additive schema change — needs migration 004 (additive column, no constraints).

- [ ] Migration 004 adds the column.
- [ ] Update record_run + BacktestEngineerRole.
- [ ] Calibrator reads from this column when computing predictions.
- [ ] Commit.

---

## Task 6 — Wire into lab.py

**Files:** `src/trading_bot/lab.py`, `tests/test_lab.py`

Add a third APScheduler job:
```python
# 05:00 ET daily — calibrate
sched.add_job(
    runners["calibrate"],
    trigger=CronTrigger(hour=5, minute=0, timezone="America/New_York"),
    id="calibrate",
)
```

Add a `calibrate` runner in `_build_runners`. Update test_lab.py's job-count assertion.

- [ ] Implement.
- [ ] Update test.
- [ ] Commit.

---

## Task 7 — Reporter surfaces drift in daily digest

**Files:** `src/trading_bot/roles/reporter.py`

Append a "Calibrator Drift" section to the daily digest email body, showing the most recent `CalibrationRun` corr + severity. Read-only, no policy.

- [ ] Add the section.
- [ ] Test that the rendered HTML mentions "Calibration" when a row exists.
- [ ] Commit.

---

## Task 8 — Phase 3.5 deployment dry run

- [ ] Full pytest passes — must show 359 + ~12 new ≈ 371 passing.
- [ ] Lab boots (subprocess test passes).
- [ ] Manual smoke test of one calibrator trial: ensure `insufficient_data` is returned cleanly with the live (empty) trade_journal.
- [ ] Plist already loads the lab process — no install change.

---

## Acceptance criteria

1. `state.db.calibration_runs` accumulates rows (one per day).
2. Calibrator returns `insufficient_data` cleanly until ≥ 10 closed trades exist in the rolling window.
3. `state.db.promoter_halts` is written when severity == "high"; Promoter respects the halted_until window.
4. Daily digest email includes the latest drift score.
5. `uv run pytest tests/` passes.
