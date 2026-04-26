# Revised Plan Sequence (post-trader-review)

**Date:** 2026-04-26
**Driven by:** [trading_risk_analysis.md](trading_risk_analysis.md)
**Status:** Strategic re-sequencing — supersedes the 5b–5f order in [2026-04-25-plan-5-adaptive-intelligence.md](2026-04-25-plan-5-adaptive-intelligence.md)

## TL;DR

The trader's central finding is correct: **we are adding intelligence on top of an unvalidated core.** Plan 5b (dynamic sizer) is well-designed but premature — its tuning knobs (`conviction_floor`, `target_atr_pct`, `mult_ceiling`) are educated guesses with zero empirical backing. Sizing better before knowing whether the underlying signal is profitable just amplifies an unknown.

**Therefore, before any new intelligence work, we ship validation infrastructure and exit hardening.** Specifically: backtest harness first, exit logic second, then revisit dynamic sizing with real data behind every parameter.

The originally-designed Plan 5b is **paused** (spec + plan committed and remain on disk), not abandoned. It re-enters the queue after the prerequisites land.

## Where I disagree (or sharpen) the trader's prioritisation

Mostly aligned. Three calibrations:

1. **"Don't proceed to Plan 5 feature work until 30+ closed trades."** Agreed in spirit — but Plan 5a (screener / watchlist expansion) is already shipped and is itself one of the trader's mitigations (Risk 7, "Crowded signals"). So the rule is: pause *new* feature work, but Plan 5a stays.

2. **Trailing stops, crypto post-fill verify, earnings exclusion, stop-leg verify** are all separate items in the trader's list (Risks 2, 4, 8). They share a code site (the order placement / post-fill monitor path) and should ship as **one combined "exit hardening" plan**, not four. Less context-switching, single test surface.

3. **VIX wiring + vol threshold lowering + watchlist cutover** are small enough to be **hotfixes**, not full plans. Three PRs, two days. Get them in before any larger work — they reduce the cost of running paper while the real plans land.

## New Sequence

```
Phase 0  Hotfixes (≈ 2 days)              ← 3 small PRs, no spec
Plan 5b  Backtest Harness                  ← validates everything else
Plan 5c  Exit Hardening (combined)
Plan 5d  Dynamic Position Sizer            ← was 5b; re-tuned with backtest data
Plan 5e  Halt + Intraday Monitor
Plan 5f  Evolution Loop Upgrades
Plan 5g  VIP Tweet Sentiment              ← was 5d
Plan 5h  Rich HTML Emails                 ← was 5e
Plan 5i  Nightly Research Scout           ← was 5f
```

Each item below sketches scope and the risk it retires. Detailed specs follow per-plan.

---

### Phase 0 — Hotfixes

Four small changes, ship as a single feature branch. No design doc. Each is its own commit.

**Status (2026-04-26):** All four hotfixes implemented. 0a, 0b, 0c are code changes in `cli.py` / `regime.py` / `config.py` / `config.yaml`. 0d is the scheduled-tasks MCP rewrite + new `eod-report` command. Tests added: 4 new in `test_regime.py` (VIX overrides + lowered threshold), 2 new in `test_cli.py` (`_load_active_universe` fallback + merge).

**0a — Cut the orchestrator over to `opportunities.md` for live scans.**
The `rank` command writes ranked candidates to `strategy/opportunities.md`. The orchestrator's `scan()` is wired to read it via `load_ranked_watchlist()`. Verify the **scheduled** entrypoint (cron / launchd / `bot full-run`) actually invokes `rank` before scanning, and that the legacy `strategy/watchlist.yaml` is no longer the live source. If it still is, that's the fix.
*Retires:* Risk 7 (crowded signals) — moves us off 7 hardcoded names onto the screener output that already works.

**0b — Lower `risk_off` realised-vol threshold from 30% → 22% annualised.**
Single change in [regime.py](src/trading_bot/regime.py). Add a config knob so it's tunable later from YAML.
*Retires part of:* Risk 3 (regime detector misclassifies) — the trader cited 25-28% vol in the March-April 2025 correction misclassifying as non-risk-off.

**0c — Wire VIX (FRED `VIXCLS`) directly into regime detection.**
The FRED feed already exists in [intelligence.py](src/trading_bot/intelligence.py). Add a VIX read into `detect_regime_from_bars` (rename to `detect_regime` since it'll take more than bars). Rules: `VIX > 28` forces `risk_off`; `VIX > 22` forces at least `sideways`. VIX missing/stale → fall back to current logic.
*Retires the rest of:* Risk 3.

**0d — Decouple the 16:30 EOD email from trade placement.**
The existing `scheduled-tasks` MCP routine `trading-bot-daily-full-run` (cron `30 16 * * 1-5`) was running `bot full-run`, which calls `orch.scan()` and queues orders 30 minutes after the close — a side effect that belongs to `intel-scan`, not the EOD report. Also, the routine's description claimed "comprehensive HTML" but `full-run` actually sends the basic `build_daily_report_html`, not the rich version.

*Fix:*
1. New CLI command `bot eod-report` in [cli.py](src/trading_bot/cli.py) — regime detect + intel gather + portfolio snapshot diff + SPY daily change + rich HTML email. Empty `ScanResult`. No `orch.scan`, no order placement.
2. Repoint `trading-bot-daily-full-run` to invoke `bot eod-report` instead of `bot full-run`. Description updated to match.

*Retires:* coupling between report emission and order placement; observability gap on what the rich report actually contains.

Effort: ~half a day each. **No new feature work proceeds until these are in.**

---

### Plan 5b (revised) — Backtest Harness

**Goal:** Replay 2 years of historical bars through the existing strategy + risk + sizer code, generating synthetic closed trades. Output: per-strategy and per-regime win rate, profit factor, Sharpe, max drawdown, hold-period distribution.

**Scope:**
- New module `backtest.py`. Driven by historical Alpaca bars + a deterministic clock.
- Replays `MomentumStrategy` and `MeanReversionStrategy` against SPY/QQQ/AAPL/MSFT/AMD plus a representative ranked-watchlist sample, across 2024-01-01 → present.
- Uses the **real** orchestrator + risk_manager code paths — bracket orders simulated by triggering whichever leg (stop or take-profit) hits first in subsequent bars.
- Writes results to `strategy/backtest_results.md` and a SQLite-backed structured table for the evolution loop.
- New CLI: `bot backtest --from 2024-01-01 --to 2026-04-26 --strategies momentum,mean_reversion`.

**Acceptance bar:** ≥ 30 trades per strategy per regime. If profit factor < 1.0 or Sharpe < 0.5 in the dominant regime, **the trader is right and we stop; the rules need rework before any further plan ships.**

**Retires:** Risk 1 (unvalidated params), and seeds Risk 5 (gives the evolution loop something to chew on).

**Estimated size:** medium. Roughly twice the test surface of Plan 5a.

---

### Plan 5c — Exit Hardening (combined)

**Goal:** Replace passive bracket-only exits with an actively-monitored exit layer.

**Scope:**
1. **Trailing stop logic** in code (not just spec). At unrealized P&L ≥ +3%, cancel-and-replace the stop leg at breakeven. At ≥ +5%, trail at 50% of unrealized gain above breakeven. Implemented as a periodic sweep over open positions in the existing `portfolio_monitor.py`.
2. **Crypto post-fill verification.** After `_place_crypto_with_stop()`, sleep 100ms, query Alpaca for the position + the stop order. If stop missing, immediate market-flatten. (Risk 4.)
3. **Stop-leg verification for stocks.** After every bracket placement, queue a 30s-deferred verification check; if the stop leg is `cancelled`/`rejected`/missing, replace it. (Risk 8.)
4. **Earnings-window exclusion.** Add an earnings-calendar adapter (Alpaca news feed already provides earnings-day flags via `intelligence.py`; if not, a static CSV from a free source updated weekly). Block entries within 3 trading days of earnings on stock symbols.

**Out of scope for 5c:** time-based "dead money" exit. Originally listed here, pulled out because its threshold (±2% / 5 days) is a strategy-tuning parameter, not a safety fix — it should be evaluated against backtest data in 5f (evolution loop) once 5b results exist.

**Retires:** Risk 2 (no trailing stops), Risk 4 (crypto non-atomic), Risk 8 (no intraday monitor / earnings blowups).

**Estimated size:** medium-large. Combines four risk items into one coherent post-fill-monitor module.

---

### Plan 5d — Dynamic Position Sizer (was 5b)

**Status:** Spec + implementation plan already written ([spec](2026-04-26-plan-5d-dynamic-risk-design.md), [plan](../plans/2026-04-26-plan-5d-dynamic-risk.md)). Both remain on disk, renamed from `5b` → `5d` to match the new sequence.

**Changes that backtest data will drive:**
- `conviction_floor`: currently 0.3 (guess). After backtest, set so it cuts the bottom-quintile of historical signals by P&L attribution.
- `target_atr_pct` per asset class: currently `{stock: 2%, crypto: 5%, option: 4%}` (heuristic). After backtest, set per-class to whatever ATR/price level historically produced the best P&L per unit of risk taken.
- `mult_ceiling`: currently 2.0. After backtest, set so the upper tail isn't dominated by single-position blowups.

**Retires:** Risk 9. Also makes Risk 5 (slow evolution) less acute because sizing already differentiates good from bad signals before the evolution loop tunes parameters.

---

### Plan 5e — Halt + Intraday Monitor

**Goal:** Replace the blunt -2%/-5% manual-reset halt with a graduated, intraday-aware mechanism.

**Scope:**
- **Soft halt** at -1.5% daily: stop *new* entries, but keep monitoring open positions and trailing their stops. No manual reset required — clears at the next session open.
- **Hard halt** at -2.0%: cancel all pending entry orders (existing stop legs on filled positions remain), email alert, manual reset.
- **Intraday equity polling**: extend `pnl_state.py` to read intraday portfolio history from Alpaca (the current `period="1W"`, `timeframe="1D"` only sees daily closes). Halt decisions use the most-recent intraday snapshot.

**Retires:** Risk 6 (blunt halt), part of Risk 8.

---

### Plan 5f — Evolution Loop Upgrades

**Goal:** Make the evolution loop adaptive enough to actually correct parameters.

**Scope:**
- **Regime-conditional metrics.** Instead of aggregating across all trades, compute win rate / profit factor per `(strategy, regime)` pair. Only propose changes within the regime that has enough data.
- **Tune indicator thresholds**, not just risk sizing. RSI window, MACD parameters, EMA window, hold-period limit. The current evolution only touches `per_trade_risk_pct` and `stop_pct`.
- **Faster feedback** — once Plan 5c trailing stops are in, trades close faster (median hold drops from weeks → days), which shrinks the feedback latency the trader flagged.

**Retires:** Risk 5.

---

### Plans 5g / 5h / 5i (re-numbered, unchanged scope)

Originally 5d / 5e / 5f. Pushed back because each adds intelligence/comms surface on top of the trading core, and the trader's argument applies: harden the core first.

- **5g** — VIP Tweet Sentiment (Truth Social RSS + GDELT; veto/boost policy)
- **5h** — Rich HTML Emails (Jinja2 + matplotlib charts)
- **5i** — Nightly Research Scout (web search → email proposal → user approves)

---

## Sequencing Logic Summary

| New plan | Retires risks | Why this position |
|---|---|---|
| Phase 0 | 3, 7 | Single-day fixes; lowers blast radius before any larger work |
| 5b backtest | 1, seeds 5 | Validates params; produces data needed to tune 5d |
| 5c exit hardening | 2, 4, 8 | Stop the bleed on individual trades; runs in parallel with backtest analysis |
| 5d sizer | 9 | Now data-driven, not guessed |
| 5e halt | 6, rest of 8 | Capital-preservation upgrade once we know how trades behave |
| 5f evolution | 5 | Now has 5b backtest history + 5c faster closures + 5d structured signals to learn from |
| 5g/h/i | — | Comms + intel layers, on top of a hardened core |

## What I'm asking for

Pick which of these to lock in:
- (a) The Phase 0 scope (3 hotfixes) — proceed to brainstorming each as a small change?
- (b) The new Plan 5b = backtest harness — proceed to brainstorming its design?
- (c) Reorder differently — push back on any sequence above?

The original Plan 5b spec/plan stay on disk and re-enter the queue as Plan 5d.
