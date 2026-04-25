# Trading Bot — Intelligence & Evolution (Plan 3 of 5)

**Goal:** Add live regime detection, live P&L state, mean reversion strategy, fill reconciliation, and a weekly evolver that lets the bot learn from its own performance and update `strategy/rules.md` over time. Schedule daily routines so the bot runs 24/7.

**Architecture:** Pure additions to Plan 1+2 modules — no rewrites. Each piece is a focused module under `src/trading_bot/`. Scheduled execution via Claude Code's `scheduled-tasks` MCP (cloud, always-on).

**Tech additions:** `requests` (FRED API), no new heavy deps.

---

## Tasks (focused, executed directly)

### Task 1: Live regime detector
- File: `src/trading_bot/regime.py`
- Inputs: VIX (FRED API, free), SPY 50d/200d EMA (Alpaca bars)
- Output: `Regime` enum (`trending_up`, `trending_down`, `sideways`, `risk_off`)
- Fallback: if FRED is unreachable, use VIX proxy from Alpaca's QQQ vs SPY relative volatility
- Tests: 4 fixture-based tests for each regime

### Task 2: Live P&L state
- File: `src/trading_bot/pnl_state.py`
- Replaces orchestrator's stub `_build_risk_state()`
- Computes daily/weekly P&L from current equity vs journal entries vs Alpaca portfolio history
- Detects circuit-breaker conditions and returns `halted=True` when breached
- Tests: synthetic equity timeline, breached/non-breached scenarios

### Task 3: Mean reversion strategy
- File: `src/trading_bot/strategy.py` — extend with `MeanReversionStrategy` class
- Rule: RSI < 30 AND price within 1 stdev below 20-day mean AND no negative news (Plan 4 wire)
- Entry: limit at current price, stop at 2x stdev below mean
- Active in `sideways` and `risk_off` regimes only
- Strategy router selects momentum vs mean reversion based on regime

### Task 4: Reconciliation + closed-trade tracking
- File: `src/trading_bot/reconciliation.py`
- Reads Alpaca order history, matches against journal, marks fills
- Adds `closed_trades` SQLite table (entry, exit, realized P&L, hold time, strategy, regime)
- Updates positions vs journal mismatches → alert email if drift
- CLI: `bot reconcile`

### Task 5: Performance evaluator + evolver
- File: `src/trading_bot/evolution.py`
- `evaluate_performance()` → win rate, profit factor, Sharpe, max DD per strategy/regime
- `propose_rule_changes()` → if win rate < 40% over 20+ trades, suggests RSI threshold loosening; if win rate > 65%, suggests scaling up
- Appends proposals to `strategy/rules.md` evolution log (Claude-readable format)
- CLI: `bot evolve` (read-only review) + `bot evolve --apply` (writes rule changes)
- **Safety:** Never auto-applies if proposal would relax a hard risk limit. Always preserves halt rules.

### Task 6: `bot full-run` orchestrator
- Single command that does the daily flow: detect regime → reconcile → scan → email report → log
- This is what scheduled tasks invoke

### Task 7: Schedule via Claude scheduled-tasks MCP
- Daily 9:00 ET: `bot full-run` (regime + scan + report)
- Weekly Saturday 10:00 ET: `bot evolve` (review performance, propose changes)
- Hourly during crypto hours (every 1h): `bot scan --regime <current>` for crypto only

### Task 8: Manual E2E verification
- Run `bot evolve` on the trade journal — should produce a sensible (possibly empty) proposal
- Verify reconciliation correctly matches the AMD trade
- Confirm scheduled tasks visible in Claude

---

## Self-Evolution Loop (the heart of "learn and evolve")

```
Each trade → journaled with full context (regime, indicators, signal reason)
                       ↓
        Reconciliation matches fills → records realized P&L
                       ↓
            Closed-trade table accumulates over time
                       ↓
   Weekly evolver analyzes → proposes rule tweaks → appends to rules.md log
                       ↓
   User (or Claude in next run) reviews rules.md → applies if sensible
                       ↓
       Strategy parameters update → next week's trades use new rules
```

**Hard guardrails on evolution (never relaxed automatically):**
- Daily/weekly loss circuit breakers (2%/5%)
- Max position size (10%)
- Concentration cap (5%)
- Stop-loss mandatory on every trade
- Paper-only enforcement

**Soft parameters that CAN evolve:**
- RSI thresholds (within reasonable bounds: lower 50-60, upper 65-75)
- Per-trade risk percentage (within 0.25%-1.0%)
- Stop distance (3%-7%)
- Watchlist additions/removals
- Strategy activation per regime

---

## What Plan 4 Will Build (later)

- `trading-intelligence-mcp` MCP server (formal abstraction, exposes all data feeds + state to Claude tools)
- SEC EDGAR feed (insider trades, 13F)
- GDELT feed (global news sentiment)
- Sentiment overlay layer (boost/veto trades by news mood)
- Options strategies (covered calls, protective puts)
