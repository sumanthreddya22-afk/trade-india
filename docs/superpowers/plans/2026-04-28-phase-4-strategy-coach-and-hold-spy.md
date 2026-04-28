# Phase 4 — Strategy Coach + Hold-SPY Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up Strategy Coach (Role 10, daemon) and Hold-SPY Coordinator (Role 15, daemon) so the bot can autonomously detect "I'm losing to SPY" and fall back to a passive SPY hold. Reverses cleanly when conditions resume.

**Architecture:** Two new daemon-side roles + a `journal_alpha.py` analytics module that computes 30-day paper-trade alpha vs SPY. Coach evaluates the fallback flag once daily at 06:00 ET; the flag is read by Stock Scanner + Crypto Scanner before opening new positions, and by Hold-SPY Coordinator at 15:55 ET to drive the 5-day transition.

**Activation gate:** Coach returns `insufficient_data` when fewer than 30 days of trade journal exist. Until then, the flag stays at its bootstrap value (`fallback_active=False`). This means the system runs as Phase 3 today; Coach's behavior turns on naturally once enough trades close.

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md) §7.3 Role 10, §7.5 Role 15, §11 (full transition logic).

---

## File structure for Phase 4

### New files
```
src/trading_bot/
  journal_alpha.py               # 30d paper alpha vs SPY computation
  roles/strategy_coach.py        # Role 10 — flag flipper
  roles/hold_spy_coordinator.py  # Role 15 — 5-day transition mechanic

migrations/versions/
  005_fallback_flag.py

tests/
  test_journal_alpha.py
  roles/test_strategy_coach.py
  roles/test_hold_spy_coordinator.py
```

### Files modified
- `src/trading_bot/state_db.py` — add `FallbackFlag` ORM model
- `src/trading_bot/daemon.py` — wire both roles into APScheduler
- `src/trading_bot/scheduler_jobs.py` — add the 06:00 ET coach + 15:55 ET coordinator jobs
- `src/trading_bot/roles/stock_scanner.py` — early-return when fallback_active
- `src/trading_bot/roles/crypto_scanner.py` — early-return when fallback_active
- `src/trading_bot/roles/reporter.py` — surface fallback status in daily digest

---

## Task 1 — FallbackFlag ORM + migration

**Files:** `state_db.py`, `migrations/versions/005_fallback_flag.py`

Schema:
```python
class FallbackFlag(Base):
    __tablename__ = "fallback_flags"
    id = Column(Integer, primary_key=True, autoincrement=True)
    fallback_active = Column(Integer, nullable=False)   # 0/1, sqlite-friendly
    set_at = Column(DateTime(timezone=True), nullable=False, index=True)
    set_by = Column(String(64), nullable=False)         # "strategy_coach" | "manual" | "bootstrap"
    reason = Column(Text, nullable=True)                # corr or alpha values
```

The "current" flag is the row with the latest `set_at`. Rows are append-only — full audit trail.

- [ ] Add ORM model.
- [ ] Generate migration 005.
- [ ] Apply, commit.

---

## Task 2 — `journal_alpha.py` math module

**Files:** `src/trading_bot/journal_alpha.py`, `tests/test_journal_alpha.py`

`compute_journal_alpha_vs_spy(trade_journal_db, spy_benchmark, *, lookback_days=30)` returns a dict:
```
{
  "n_trades": int,
  "strategy_return_pct": float,
  "spy_return_pct": float,
  "alpha_multiplier": float,         # strat / spy, clamped at INF sentinel
  "insufficient_data": bool,         # True when n_trades < 5 closed trades
}
```

- Reads closed trades from `trade_journal.db` for the lookback window.
- Strategy return = sum of realized P&L / starting equity at lookback start.
- SPY return = SpyBenchmark.period_return() over the same date span.
- Alpha multiplier = strat_return / spy_return; clamps to ±100 sentinel for near-zero SPY.

- [ ] Tests cover: 30 winners, 30 losers, insufficient n, SPY flat (±epsilon).
- [ ] Implementation reuses SpyBenchmark from Phase 3.
- [ ] Commit.

---

## Task 3 — StrategyCoachRole

**Files:** `src/trading_bot/roles/strategy_coach.py`, `tests/roles/test_strategy_coach.py`

Charter:
```python
class StrategyCoachRole(BaseRole):
    name = "strategy_coach"
    tier = 2
    process = "daemon"
    job_description = "Once-daily evaluation of 30d paper alpha vs SPY; flips fallback_active flag with hysteresis."
    sla_seconds = 30
    upstream_roles: list[str] = []
    downstream_roles = ["stock_scanner", "crypto_scanner", "hold_spy_coordinator"]
```

`_do_work(ctx)` flow:
1. Read latest `FallbackFlag` row → current state.
2. Compute `journal_alpha = compute_journal_alpha_vs_spy(...)` for last 30 days.
3. If `insufficient_data`: write a no-op CalibrationRun-style audit entry, return `{"flag_change": False, "reason": "insufficient_data"}`.
4. Apply hysteresis state machine (spec §11):
   - Currently OFF (active strategy): if `alpha_multiplier < 1.5` → flip ON. Reason: `"alpha < 1.5x SPY"`.
   - Currently ON (fallback): if `alpha_multiplier > 1.65 AND has been > 1.5 for 5 consecutive trading days` → flip OFF. (Track "consecutive days" by querying the most recent 5 daily journal_alpha rows — needs a small history table, OR walk back over the trade journal applying the daily computation.) For simplicity, use: walk back the last 5 daily windows (each a 30-day rolling alpha as of that day), check all > 1.5 AND today > 1.65.
5. On flip: append a new `FallbackFlag` row with `set_by="strategy_coach"`, reason = the metric values.
6. Return outputs dict.

- [ ] Tests: state-machine transitions (4 cases — stay-off, flip-on, stay-on, flip-off-with-hysteresis).
- [ ] Implementation.
- [ ] Commit.

---

## Task 4 — Stock + Crypto Scanner consult flag

**Files:** `roles/stock_scanner.py`, `roles/crypto_scanner.py`

Add a helper (`is_fallback_active(engine) -> bool`) reading the latest FallbackFlag row. Scanners call it at the start of `_do_work`; if active, return `{"skipped": True, "reason": "fallback_active"}` immediately. Existing positions still managed by Order Steward / Portfolio Monitor (no change to those roles).

- [ ] Helper in `state_db.py` or a new `state_fallback.py`.
- [ ] Both scanners call it.
- [ ] Tests: scanner short-circuits when flag is set.
- [ ] Commit.

---

## Task 5 — HoldSpyCoordinatorRole — exit phase

**Files:** `src/trading_bot/roles/hold_spy_coordinator.py`, `tests/roles/test_hold_spy_coordinator.py`

Charter:
```python
class HoldSpyCoordinatorRole(BaseRole):
    name = "hold_spy_coordinator"
    tier = 4
    process = "daemon"
    job_description = "On fallback_active=True, liquidates 1/5 of active-strategy positions per trading day at 15:55 ET; buys SPY proportionally."
    sla_seconds = 120
    upstream_roles = ["strategy_coach"]
    downstream_roles = ["trade_executor"]
```

State machine via a new `transition_state` table tracking `(fallback_id, day_index, phase: 'exit'|'reverse')`:
- Day 1 of fallback: snapshot all positions; mark them as "active-strategy" (anything that isn't SPY).
- Day 2-6 each at 15:55 ET: compute 1/5 of remaining active-strategy positions (round to whole shares), submit market orders to Trade Executor for sells.
- Same daily window: read freed equity from Alpaca, place a SPY BUY for the freed amount. Risk Officer gates this like any other order.
- Day 6 onward (still in fallback): no-op; just confirms 100% in SPY.

When flag flips OFF (resume): symmetric — sell SPY 1/5 per day, allow Stock Scanner to repopulate naturally.

- [ ] Test: 5-day exit unfolds (sell 1/5 each day, buy SPY).
- [ ] Test: 5-day reverse unfolds (sell SPY 1/5 each day).
- [ ] Test: idempotent if invoked twice on same day.
- [ ] Implementation.
- [ ] Commit.

---

## Task 6 — Wire daemon scheduler

**Files:** `src/trading_bot/daemon.py`, `src/trading_bot/scheduler_jobs.py`

Two new APScheduler jobs:
- `strategy_coach`: 06:00 ET daily, M-F (calls `StrategyCoachRole.safe_run`).
- `hold_spy_coordinator`: 15:55 ET daily, M-F (calls `HoldSpyCoordinatorRole.safe_run`).

- [ ] Add to `_load_runners` + `register_jobs`.
- [ ] Update test_integration_daemon.py if it counts jobs.
- [ ] Commit.

---

## Task 7 — Reporter surfaces fallback status

**Files:** `src/trading_bot/roles/reporter.py`

Add a "Strategy Mode" line near the top of the daily digest: "ACTIVE: momentum_v3" or "FALLBACK: hold_spy (since 2026-04-30)". Read from latest FallbackFlag row.

- [ ] Implement.
- [ ] Test the rendered HTML mentions either state.
- [ ] Commit.

---

## Task 8 — Phase 4 deployment dry run

- [ ] Full pytest passes — must show 371 + ~25 new ≈ 396 passing.
- [ ] Daemon boots cleanly with both new jobs registered.
- [ ] Manual smoke test of StrategyCoach: with empty trade_journal, returns `insufficient_data` cleanly.
- [ ] Bootstrap a `fallback_active=0` row in production state.db so the flag has a known starting value (otherwise scanners that read the latest row see None and conservatively early-return).
- [ ] No install change — daemon plist unchanged.

---

## Acceptance criteria

1. `state.db.fallback_flags` exists; bootstrap row inserted on first daemon boot if empty.
2. StrategyCoach runs daily at 06:00 ET, returning `insufficient_data` cleanly until 30 days of trades exist.
3. When `fallback_active=True`, both scanners short-circuit; HoldSpyCoordinator drives 1/5/day transition; Reporter shows the state.
4. Hysteresis works: bot doesn't flip OFF until alpha > 1.65× sustained 5 consecutive days.
5. `uv run pytest tests/` passes.
