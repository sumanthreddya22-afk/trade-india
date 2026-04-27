# Autonomous Self-Evolving Trading System — Design

**Date:** 2026-04-27
**Status:** Spec, awaiting user review → implementation plan
**Supersedes:** Plan 5d (dynamic risk), Plan 7 (composite signals) — both fold into this design.

---

## 1. Context

The bot existed but did not behave like an autonomous system. On 2026-04-27 the owner could not get timely emails, did not see trades placed during market hours, and had no confidence the system was even running. The root cause was operational, not strategic: the 10 scheduled tasks live in Claude's session directory and only fire when an interactive session is alive. There is no daemon, no watchdog, no health monitoring, and most CLI commands only email when a trade is placed — silent on every other failure mode.

The owner's ambition is full autonomy: the system must run 24/7 without human supervision, evolve its own strategies, and keep itself alive through failures. The owner accepts one and only one manual touchpoint: a deliberate `bot promote` command to copy a paper-validated config into a live-trading account. Everything else must be hands-off.

This spec replaces the current operational and evolution architecture with a three-process system organized into 26 named roles, each with a charter, a KPI, and a fail-safe behavior contract.

## 2. Goals and non-goals

### Goals
1. **Fully autonomous operation.** Zero interactive prompts during normal operation. The system decides, acts, and self-recovers without human input.
2. **Always-on liveness.** When the host is online, the bot is online. Crashes auto-restart. Hangs auto-restart. Network blips auto-recover.
3. **Self-evolution.** Strategy parameters and (with safety gates) strategy code are mutated, backtested, and promoted automatically based on walk-forward performance.
4. **Capital preservation through clear constraints.** Max position 10%, daily loss 3%, max drawdown 20% before automatic pause.
5. **Beat SPY by 2× target / 1.5× floor.** When no active variant clears 1.5× SPY rolling-12mo alpha, the system holds SPY rather than bleeding on dead strategies.
6. **Honest reporting.** Per-trade fills, daily digest at 18:00 ET, weekly review Mon 08:00 ET, plus immediate critical alerts. No spam.
7. **Explicit role accountability.** Each routine has a written charter, a measurable KPI, and a report card.

### Non-goals
- Live trading from day one. Paper-only until owner runs `bot promote`.
- ML / RL / black-box models. All strategy logic is interpretable Python rules. The only LLM use is weekly strategy template proposals + their review, both gated by sandbox + walk-forward.
- Microsecond latency. We trade on 15-minute bars and slower. APScheduler-in-process is sufficient.
- Multi-user / multi-tenant. Single owner, single Mac.
- Replacing the existing dashboard. Dashboard is extended with new endpoints, not rewritten.

## 3. Locked decisions (the contract)

| Decision | Choice |
|---|---|
| Runtime host | Mac kept on 24/7 with launchd daemon + watchdog |
| Autonomy mode | Paper now, manual `bot promote` to live with frozen config snapshot |
| Evolution scope | Parameter search (Tier-A) + Claude-proposed new templates (Tier-B) with sandboxed walk-forward gate |
| Reporting profile | Minimalist + every-trade fill email |
| Success metric | Rolling 12-month alpha vs SPY: target 2×, floor 1.5× |
| Risk caps | Max position 10%, daily loss 3%, max drawdown 20% before pause |
| Capital | $100k Alpaca paper account |
| Fallback when no alpha | Hold SPY (or cash) until a variant re-clears 1.5× |

## 4. High-level architecture

Three physically separate Python processes, each managed by its own launchd plist. They communicate only through files and a shared SQLite database. None imports another's internals.

```
┌─────────────────────────────────────────────────────────────────┐
│ SUPERVISOR PROCESS (launchd: com.bharath.trading.supervisor)    │
│ - heartbeat watch, drawdown breach, account reconcile           │
│ - schedule audit, resource budget, kill switch                  │
└─────────┬───────────────────────────────────────────────────────┘
          │ pause.flag, alerts (email)
          ▼
┌─────────────────────────────────────────────────────────────────┐
│ EXECUTION DAEMON (launchd: com.bharath.trading.daemon.paper)    │
│ - reads paper_active.json (frozen until lab promotes)           │
│ - APScheduler runs scans, orders, monitors, emails              │
│ - never modifies itself; never imports lab code                 │
└─────────┬───────────────────────────────────────────────────────┘
          │ writes trade_journal.db, last_scan.json, heartbeat.json
          ▼
┌─────────────────────────────────────────────────────────────────┐
│ EVOLUTION LAB (launchd: com.bharath.trading.lab)                │
│ - reads journal + market data; never reads daemon RAM           │
│ - param search nightly; template propose weekly                 │
│ - writes paper_active.json atomically (NEVER live_active.json)  │
└─────────────────────────────────────────────────────────────────┘
```

**Why physical separation:** A bug in evolution code (especially Claude-generated strategy code) cannot crash the daemon or corrupt live trading. A daemon crash cannot corrupt the leaderboard. The supervisor's view of reality is independent of both — it queries Alpaca directly and reads files; it does not trust either daemon's in-memory state.

Two trading-account configs:
- `paper_active.json` — paper daemon's frozen config, rewritten only by Lab's auto-promote.
- `live_active.json` — live daemon's frozen config, rewritten only by `bot promote` CLI. Does not exist until first promotion.

When live trading is enabled, a fourth process is started: a second instance of the daemon pointed at `live_active.json` and live Alpaca credentials. The lab continues to write only to paper.

## 5. External data sources

| # | Source | Use | Auth | Cost |
|---|---|---|---|---|
| 1 | Alpaca Trading API (paper) | account, positions, place/cancel orders, fills | API key+secret in `.env` | free |
| 2 | Alpaca Market Data (IEX) | live daily/minute OHLCV for US stocks | same | free |
| 3 | Polygon.io | grouped daily bars (1 call → 10k tickers), per-symbol multi-year history, news+sentiment, splits/dividends | API key in `.env` | $29-99/mo |
| 4 | FRED | VIX, treasury yields, macro signals | free API key | free |
| 5 | GDELT 2.0 GKG | per-symbol global news tone (lab-side) | none | free |
| 6 | SEC EDGAR | 8-K filings (hard-blocker), 10-Q/K, insider transactions | none | free |
| 7 | Truth Social RSS | VIP tweet stream (alert-only) | none | free |
| 8 | Yahoo / stooq | SPY benchmark prices for alpha calc (fallback) | none | free |
| 9 | Anthropic API | Strategy Architect (template proposal) + Code Reviewer (validation), weekly | API key in `.env` | ~$5-20/mo |

## 6. Internal data stores

All under `data/`. Three SQLite databases run in WAL mode for concurrent reads.

| File | Tables / format | Writers | Readers |
|---|---|---|---|
| `state.db` | `leaderboard`, `evolution_runs`, `heartbeats`, `config_history`, `equity_high_water_mark`, `regime_history`, `role_kpis`, `role_runs` | Daemon, Lab, Supervisor | all three |
| `trade_journal.db` | `orders`, `fills`, `errors`, `decisions`, `vetoes` | Daemon | Daemon, Lab, Reports |
| `massive_grouped.db` | grouped daily OHLCV cache (Polygon) | Universe Curator | Daemon, Lab |
| `news_sentiment.db` | per-symbol Polygon news+sentiment cache (3-day TTL) | Sentiment Analyst | Daemon |
| `gdelt_cache.db` | daily GDELT tone scores per ticker | Tone Analyst (lab) | Lab template B |
| `edgar_cache.db` | 8-K events with timestamps, last 30 days | Insider Tracker | Daemon |
| `paper_active.json` | active config the paper daemon trades from | Promoter (lab) | Paper daemon (read-only) |
| `live_active.json` | active config the live daemon trades from | `bot promote` CLI | Live daemon (read-only) |
| `candidate_config.json` | next-best leaderboard entry (diagnostics) | Lab | inspection only |
| `last_scan.json` | last scan summary | Daemon | Dashboard, Supervisor |
| `heartbeat.json` | `{ts, pid, version, last_action}` written every 60s | Daemon | Supervisor |
| `pause.flag` | sentinel — if file exists, daemon must not place orders | Supervisor or human | Daemon |
| `runs/<YYYY-MM-DD>/<role>/*.json` | per-run structured logs | Daemon, Lab | Reports, audit |
| `data/backups/<YYYY-MM-DD>/*.db` | nightly SQLite backups, 30d retention | Resource Guardian | manual recovery |

`paper_active.json` and `live_active.json` are written atomically: write to tmp, fsync, rename. Daemons read on a watch-and-reload pattern.

## 7. The 26-role taxonomy

Every routine is a `Role` — a Python module implementing a fixed Protocol with a Role Charter docstring. The charter is loaded as context when LLM-side roles reason about the system.

### 7.1 Role Protocol

```python
class Role(Protocol):
    name: str                                     # snake_case identifier
    tier: int                                     # 1-6
    process: Literal["daemon", "lab", "supervisor"]
    job_description: str                          # one paragraph
    sla_seconds: int                              # max wall-clock per run
    upstream_roles: list[str]                     # whose output we consume
    downstream_roles: list[str]                   # who consumes ours

    def run(self, ctx: Context) -> RoleResult: ...
    def report_card(self, lookback_days: int) -> ReportCard: ...
    def health_check(self) -> Health: ...
```

`RoleResult` carries: status, typed outputs, structured errors, latency, metrics.
`ReportCard` carries: KPI value, delta vs prior period, one-sentence prose summary.
`Health` is `OK | DEGRADED | BLOCKED | FAIL`.

### 7.2 Tier 1 — Data acquisition (the intel team)

#### Role 1 — Universe Curator (daemon)
**Mission:** Maintain the list of tradable stocks and their cached daily bars.
**Inputs:** Polygon grouped daily bars endpoint.
**Outputs:** `massive_grouped.db`, `stage1_top100.json`.
**Cadence:** `massive-refresh` 06:30 ET Mon–Fri; `rank` 07:30 ET Mon–Fri.
**KPI:** Top-25 capture rate of next-day winners (weekly rolling).
**Failure behavior:** If Polygon unreachable, use last cached grouped bars (max 24h stale). If > 24h stale, mark Stock Scanner upstream-blocked and alert.

#### Role 2 — Sentiment Analyst (daemon)
**Mission:** Refresh per-symbol news+sentiment for the active universe.
**Inputs:** Polygon news API (`/v2/reference/news`).
**Outputs:** `news_sentiment.db` (3-day TTL).
**Cadence:** 5 min before each Stock Scanner run.
**KPI:** % of names blocked by sentiment_floor that underperformed 5 days later (target ≥ 60%).
**Failure behavior:** If API unreachable, Stock Scanner uses last cached value or skips sentiment gate (logged).

#### Role 3 — Insider Tracker (daemon)
**Mission:** Detect 8-K filings on watchlist names and emit hard-blocks within 24h of filing.
**Inputs:** SEC EDGAR full-text feed.
**Outputs:** `edgar_cache.db`, block flags written to `state.db`.
**Cadence:** 04:30 ET daily.
**KPI:** Block activations + 5d return of blocked names (block "should" precede increased volatility).
**Failure behavior:** If EDGAR unreachable, Stock Scanner falls back to "no 8-K block" (logged).

#### Role 4 — Earnings Watcher (daemon)
**Mission:** Track upcoming earnings dates for held + watched names; emit pre-earnings adjustment signals per policy.
**Inputs:** Polygon earnings calendar (yfinance fallback).
**Outputs:** earnings tags in `state.db`, adjustment orders to Trade Executor.
**Cadence:** 06:00 ET daily.
**Policy (locked):**
- Earnings T-1 day on held position: trim to 50% by EOD T-1.
- Earnings T-0 (today before/after market): full exit by EOD T-1 if held < 7 days; hold through if held ≥ 7 days.
- Never *open* a new position with earnings in next 3 trading days.
**KPI:** Pre-earnings exit slippage vs hold-through P&L (rolling 90d).

#### Role 5 — Macro Sensor (daemon)
**Mission:** Classify market regime from VIX, yield curve slope, S&P breadth.
**Inputs:** FRED, Yahoo (SPY breadth).
**Outputs:** `regime` field in `state.db` (one of: `trending_up`, `trending_down`, `sideways`, `volatile_bear`, `volatile_bull`).
**Cadence:** 07:00 ET daily; ad-hoc on VIX spike (> 2σ intraday move).
**KPI:** Regime call accuracy (next 5d realized vol matches predicted regime).

#### Role 6 — VIP Listener (daemon)
**Mission:** Poll Truth Social RSS, flag HIGH-severity posts. Alert-only, never trades.
**Inputs:** Truth Social RSS feed.
**Outputs:** Alerts in `runs/<date>/vip.log`.
**Cadence:** Every 30 min during US market hours.
**KPI:** False-alarm rate; coverage of regulatory/policy posts.

#### Role 7 — Tone Analyst (lab)
**Mission:** Refresh GDELT global tone scores per ticker. Lab-side only, feeds template B + lab backtests.
**Inputs:** GDELT 2.0 GKG.
**Outputs:** `gdelt_cache.db`.
**Cadence:** 04:00 ET daily.
**KPI:** Tone-vs-Polygon-sentiment correlation (calibration check).

### 7.3 Tier 2 — Decision making (the alpha team)

#### Role 8 — Stock Scanner (daemon)
**Mission:** Run intel-scan: evaluate stage-2 watchlist, emit BUY/HOLD/SKIP per name. Never places orders.
**Inputs:** bars (Alpaca), sentiment (Sentiment Analyst), regime (Macro Sensor), 8-K blocks (Insider Tracker), earnings tags (Earnings Watcher).
**Outputs:** `decisions` rows in `trade_journal.db`, `last_scan.json`.
**Cadence:** Every 60 min, 09:30–16:00 ET, Mon–Fri.
**KPI:** BUY win rate; false-signal rate (rolling 30d).
**Constraints:** Max 3 new BUY decisions per scan, prioritized by conviction score. Excess logged but not traded.

#### Role 9 — Crypto Scanner (daemon)
**Mission:** Same as Stock Scanner but for crypto pairs. Sentiment floor not applied.
**Inputs:** Alpaca crypto bars, regime.
**Outputs:** `decisions` rows.
**Cadence:** Every 15 min, 24/7.
**KPI:** Same.

#### Role 10 — Strategy Coach (daemon)
**Mission:** Monitor active config's rolling 30d alpha vs SPY; trigger `hold_spy` fallback when below 1.5×; trigger resume when conditions clear with hysteresis.
**Inputs:** `trade_journal.db`, SPY benchmark prices.
**Outputs:** `fallback_active` flag in `state.db`.
**Cadence:** 06:00 ET daily; on every Stock Scanner cycle (consults flag).
**Policy (locked):**
- Enter fallback: 30d alpha < 1.5× SPY.
- Resume from fallback: 30d alpha > 1.65× SPY AND has been > 1.5× for 5 consecutive trading days.
**KPI:** Time-in-fallback; alpha-on-resume.

### 7.4 Tier 3 — Risk and execution (the floor team)

#### Role 11 — Risk Officer (daemon)
**Mission:** Gate every proposed order against capital-preservation rules. Size down rather than veto when possible.
**Inputs:** Proposed orders, live equity (Alpaca), open positions, risk_caps from active_config, `equity_high_water_mark`.
**Outputs:** Gated orders, veto log in `state.db`.
**Hard constraints:** Never accept if max_position_pct breached; never accept if daily realized loss > daily_loss_pct cap; if pause.flag exists, veto all; if drawdown > max_drawdown_pct, veto all and notify Account Sentinel.
**KPI:** Retrospective veto correctness — % of vetoed trades that would have lost money (target ≥ 60%).
**Failure behavior:** If Alpaca account API unreachable, veto all (fail-closed). Email after 3 failed cycles.

#### Role 12 — Trade Executor (daemon)
**Mission:** Place orders via Alpaca. Handle wash-trade, syntax errors, and other broker-side failures with deterministic recovery.
**Inputs:** Gated orders.
**Outputs:** Alpaca order IDs in `trade_journal.db`, per-trade fill emails.
**Cadence:** On-demand from Risk Officer output.
**Recovery (locked):**
- On `wash trade detected (40310000)`: cancel any conflicting open order, retry once as a single bracket OCO (entry + stop). If still rejected, blacklist symbol for 24h, email.
- On 401 (creds rejected): mark blocked-on-credentials, email, auto-resume on next 200.
- On 429 (rate limited): exponential backoff, max 5 retries.
**KPI:** Slippage vs expected; rejection rate; retry success rate.

#### Role 13 — Order Steward (daemon)
**Mission:** Post-order lifecycle: verify fill, attach stops, manage trailing stops (when configured), cancel stale unfilled limits.
**Inputs:** Open orders, positions.
**Outputs:** Updated orders, attached stops.
**Cadence:** Every 30 min during market hours; immediately after Trade Executor places an order.
**Policy:** Cancel any unfilled limit order older than 60 min. Verify every position has stop attached; re-attach if missing.
**KPI:** Stop-attached rate (target 100%); stale-order cancellation rate.

### 7.5 Tier 4 — Position care (the stewardship team)

#### Role 14 — Portfolio Monitor (daemon)
**Mission:** Snapshot diff every 30 min during market hours. Alert on stop-hits, big intraday moves, unusual fills.
**Inputs:** Alpaca positions/orders.
**Outputs:** Event records in `state.db`; per-event alerts routed via Reporter.
**Cadence:** Every 30 min during market hours.
**KPI:** Alert lead time on stop-hits.

#### Role 15 — Hold-SPY Coordinator (daemon)
**Mission:** When `fallback_active` flag is set: liquidate active positions over 5 trading days (1/5 each day, market-on-close), accumulate SPY proportionally with freed equity. Reverse on resume.
**Inputs:** `fallback_active` flag, current positions.
**Outputs:** SPY orders to Trade Executor (subject to Risk Officer gating).
**Cadence:** Daily at 15:55 ET when fallback active.
**KPI:** Active-to-SPY transition smoothness; reversal slippage.

### 7.6 Tier 5 — Evolution (the lab team)

#### Role 16 — Backtest Engineer (lab)
**Mission:** Run walk-forward backtests: 6 folds, 12mo train / 3mo test, no peeking.
**Inputs:** Strategy code + historical bars (`massive_grouped.db`).
**Outputs:** Backtest results in `state.db` keyed by `(strategy_name, params_hash, fold_id)`.
**Cadence:** Sub-routine of Param Optimizer and Strategy Architect.
**KPI:** Coverage of edge cases; deterministic re-runs (same inputs → same outputs).
**Constraints:** Each backtest run in a subprocess with `resource.setrlimit` (CPU=30s, RSS=512MB).

#### Role 17 — Param Optimizer (lab)
**Mission:** Bayesian search across param space for each active template; nightly.
**Inputs:** Leaderboard, template registry, `massive_grouped.db`.
**Outputs:** New leaderboard entries in `state.db`.
**Cadence:** 02:00 ET daily.
**KPI:** Δ alpha vs SPY achieved per week.
**Tool:** `optuna` with TPE sampler, 200 trials per template per night.

#### Role 18 — Strategy Architect (lab, LLM)
**Mission:** Saturday weekly: ask Claude to propose 1–3 new strategy templates as Python modules. See full prompt in §13.
**Inputs:** Leaderboard, recent failures, regime stats, role charters as context.
**Outputs:** Code in `src/trading_bot/strategies/_pending/`.
**Cadence:** 06:00 ET Saturday.
**KPI:** Survival rate of proposals through review; alpha contribution from accepted templates.

#### Role 19 — Code Reviewer (lab, LLM)
**Mission:** Review every proposal: AST allowlist, lookahead bias, test coverage, sandbox runtime check. See full prompt in §13.
**Inputs:** Proposed code from Strategy Architect.
**Outputs:** Accepted → `_evolved/`; rejected → archive with rationale.
**Cadence:** Immediately after Strategy Architect.
**KPI:** False-acceptance rate (something unsound that snuck through, detected later).

#### Role 20 — Promoter (lab)
**Mission:** Atomically rewrite `paper_active.json` when leaderboard's best variant clears all gates.
**Inputs:** Leaderboard, current paper config.
**Outputs:** New `paper_active.json` (atomic write).
**Cadence:** Immediately after Param Optimizer.
**Promotion gates (all must pass):**
- 12mo backtest alpha ≥ 1.5× SPY.
- ≥ 6 of 6 walk-forward folds passing.
- Sortino ≥ 1.0.
- Max DD in backtest ≤ 20%.
- Beats current paper config's fitness by ≥ 10% on out-of-sample folds.
- 30d paper-trade journal alpha ≥ 1.5× SPY (proof in real conditions).
**Bootstrap exception:** During the first 30 days after deployment (or after the very first promoted config), the 30d paper-journal gate is skipped — backtest gates alone are sufficient. After 30 days of paper-trade history exist, the journal gate becomes active.
**KPI:** Promotion cadence; post-promotion 30d realized alpha.

#### Role 21 — Calibrator (lab)
**Mission:** Daily compare yesterday's backtest expectations to actual paper trade outcomes. Alert on drift.
**Inputs:** Trade journal, backtest predictions.
**Outputs:** Drift score in `state.db`, alerts via Reporter.
**Cadence:** 05:00 ET daily.
**Drift metric:** Spearman rank correlation between predicted per-trade P&L (from the active config's most recent backtest fold covering the same regime) and realized per-trade P&L for trades made under that config. Computed over rolling 30 trades (or 30 days, whichever comes first).
**Drift policy:**
- Correlation > 0.5: healthy, no action.
- Correlation 0.3–0.5: warning, included in daily digest.
- Correlation < 0.3: HIGH-severity alert, halt Promoter for 7 days while investigation occurs (the backtest model is no longer predictive — promotion based on it is unsafe).
**KPI:** Paper-vs-backtest correlation (target ≥ 0.7 rolling 30 trades).

### 7.7 Tier 6 — Supervision (the guardian team)

#### Role 22 — Watchdog (supervisor)
**Mission:** Detect daemon stall via heartbeat staleness; auto-restart via `launchctl kickstart`.
**Inputs:** `heartbeat.json` mtime.
**Outputs:** Restart actions, alerts.
**Cadence:** Every 60 s.
**Policy:** If `heartbeat.json` mtime stale > 5 min, attempt one `launchctl kickstart`. If still stale 5 min after restart attempt, escalate to HIGH severity email.
**KPI:** Time-to-detect; restart success rate.

#### Role 23 — Account Sentinel (supervisor)
**Mission:** Reconcile Alpaca account vs trade journal; detect drawdown breach; write `pause.flag` if breached.
**Inputs:** Alpaca account API (independent fetch), `trade_journal.db`, `equity_high_water_mark`.
**Outputs:** `pause.flag` if drawdown > 20% from high-water mark; alerts on reconciliation drift > 1%.
**Cadence:** Every 60 s.
**KPI:** Drift caught; false-pause rate.

#### Role 24 — Schedule Auditor (supervisor)
**Mission:** Verify every routine ran today on its expected cadence. Alert on misses.
**Inputs:** `role_runs` table in `state.db`.
**Outputs:** Daily roll-up at 17:00 ET; immediate alerts on missed scan windows.
**Cadence:** Every 5 min during market hours; daily roll-up at 17:00 ET.
**KPI:** Missed-run detection rate.

#### Role 25 — Resource Guardian (supervisor)
**Mission:** Track Anthropic + Polygon API budgets, disk space, DB size, network connectivity. Alert + halt cost-affecting routines on breach.
**Inputs:** API call counters, `df`, `ping`, DB file sizes.
**Outputs:** Budget halts (e.g. halt Strategy Architect at 100% Anthropic budget); alerts.
**Cadence:** Every 5 min.
**Locked budgets:**
- Anthropic monthly cap: $20 (warn at 80%, halt at 100%).
- Polygon: respect rate-limit; flat-rate plan, no overage.
- Disk: warn at < 10 GB free.
- Daily SQLite backup at 06:00 ET to `data/backups/<date>/`.
- Sync to `~/Dropbox/trading-bot-backups/` (or iCloud Drive).
- 30-day retention.
**KPI:** Budget breaches caught; disk-full pre-warnings.

#### Role 26 — Reporter (daemon)
**Mission:** Compose and send daily digest at 18:00 ET + weekly review Mon 08:00 ET. Per-trade fills routed by Trade Executor through this role for templating.
**Inputs:** `state.db`, `trade_journal.db`, role report cards.
**Outputs:** Emails (SMTP via Gmail).
**Cadence:** 18:00 ET Mon–Fri (digest); 08:00 ET Mon (weekly).
**KPI:** Delivered-on-time rate.

## 8. Master schedule

### Continuous (every 60 s)
- Daemon: heartbeat write
- Supervisor: Watchdog, Account Sentinel

### High-frequency (during market hours, 09:30–16:00 ET, Mon–Fri unless noted)
- Crypto Scanner: every 15 min, **24/7**
- Portfolio Monitor: every 30 min
- Order Steward (verify-stops): every 30 min
- VIP Listener: every 30 min
- Stock Scanner: every 60 min
- Sentiment Analyst (news-warm): 5 min before each Stock Scan
- Schedule Auditor: every 5 min
- Resource Guardian: every 5 min

### Pre-market and open (Mon–Fri)
| ET | Routine |
|---|---|
| 06:00 | Earnings Watcher (refresh, tag held) |
| 06:30 | Universe Curator (massive-refresh) |
| 07:00 | Macro Sensor (regime classification) |
| 07:30 | Universe Curator (rank, top 25) |
| 08:55 | Sentiment Analyst (pre-open warm) |
| 09:25 | Sentiment Analyst (final warm) |
| 09:30 | Stock Scanner first cycle |

### End-of-day (Mon–Fri)
| ET | Routine |
|---|---|
| 16:00 | Portfolio Monitor post-close, Account Sentinel deep-check |
| 17:00 | Schedule Auditor daily roll-up |
| 18:00 | Reporter — daily digest email |

### Overnight (Mon–Fri 02:00–06:00 ET)
| ET | Routine | Process |
|---|---|---|
| 02:00 | Param Optimizer | Lab |
| 02:45 | Promoter (auto-promote check) | Lab |
| 04:00 | Tone Analyst (GDELT-warm) | Lab |
| 04:30 | Insider Tracker (EDGAR-warm) | Daemon |
| 05:00 | Calibrator | Lab |
| 06:00 | Strategy Coach (alpha vs SPY recompute, fallback flip) | Daemon |
| 06:00 | Resource Guardian — daily SQLite backup | Supervisor |

### Weekly
| When | Routine |
|---|---|
| Mon 08:00 ET | Reporter — weekly review email |
| Sat 06:00 ET | Strategy Architect (Claude proposes templates) |
| Sat 07:00 ET | Code Reviewer (Claude validates) |
| Sat 08:00 ET → Sun 23:00 | Param Optimizer runs new templates through 6-fold backtest |
| Sun 02:00 ET | DB VACUUM (Resource Guardian) |
| Sun 03:00 ET | Log rotation, archive runs > 90 d |

### Event-driven (no schedule; fires on condition)
| Trigger | Routine | Action |
|---|---|---|
| Heartbeat stale > 5 min | Watchdog | `launchctl kickstart`; email |
| Drawdown > 20% | Account Sentinel | Write `pause.flag`; email |
| Account ↔ journal drift > 1% | Account Sentinel | Email |
| 8-K filed on held position | Insider Tracker | Notify Stock Scanner to re-evaluate next cycle |
| Earnings ≤ T-1 on held | Earnings Watcher | Trigger pre-earnings adjustment |
| VIP HIGH severity post | VIP Listener | Email (no trade action) |
| Anthropic budget > 80% / 100% | Resource Guardian | Email warning / halt template-propose |
| Polygon rate limit hit | Resource Guardian | Throttle calling routines |
| Disk free < 10 GB | Resource Guardian | Email warning |
| Alpaca 401 | Trade Executor | Mark blocked-on-credentials, email; auto-resume on next 200 |
| Network down > 5 min | Resource Guardian | Pause active routines, email; auto-resume on connectivity |
| Regime change detected | Macro Sensor | Notify Scanners |
| Active config 30d alpha < 1.5× SPY | Strategy Coach | Trigger `hold_spy` fallback |
| Active config 30d alpha > 1.65× SPY for 5 consecutive days | Strategy Coach | Reverse fallback; Hold-SPY Coordinator unwinds SPY |

## 9. Config schema

`paper_active.json` and `live_active.json` share the same schema.

```json
{
  "version": "2026-04-27-v17",
  "git_sha": "abc1234",
  "promoted_at": "2026-04-27T02:00:00Z",
  "promoted_by": "auto-promote" | "manual-promote-paper-to-live",
  "active_template": "momentum_v3",
  "template_path": "trading_bot.strategies._evolved.momentum_v3",
  "params": {
    "rsi_low": 56, "rsi_high": 71, "ema_period": 22,
    "stop_pct": 4.5, "sentiment_floor": -0.4
  },
  "fitness_at_promotion": {
    "rolling_12mo_alpha_vs_spy_x": 2.1,
    "sortino": 1.7,
    "walk_forward_folds_passed": 6,
    "max_dd_in_backtest_pct": 17.4,
    "paper_journal_30d_alpha_vs_spy_x": 1.8
  },
  "risk_caps": {
    "max_position_pct": 10,
    "daily_loss_pct": 3,
    "max_drawdown_pct": 20
  },
  "universe": {
    "stocks_filter": "stage1_top100",
    "crypto_pairs": ["BTC/USD", "ETH/USD", "SOL/USD"]
  },
  "fallback_when_no_alpha": "hold_spy"
}
```

`paper_active.json` is replaced atomically: write to `paper_active.json.tmp`, fsync, rename. Daemon detects mtime change and reloads on next loop iteration.

`live_active.json` is created/replaced only by the `bot promote` CLI. The lab process is launched with filesystem ACL restricting write access to live_active.json (chmod 444 + chown).

## 10. Email contract (the inbox)

| Email | When | From | Volume |
|---|---|---|---|
| Per-trade fill | within 60s of fill | Daemon | 1–10/day |
| Daily digest | 18:00 ET Mon–Fri | Daemon | 1/day |
| Weekly review | 08:00 ET Mon | Daemon | 1/week |
| Auto-promote notice | when lab promotes paper | Lab | 0–3/week |
| Promotion-eligible (paper→live) | when paper config clears live-gate | Lab | rare |
| Heartbeat lost | within 5 min of stall | Supervisor | rare |
| Drawdown breach paused | within 60s | Supervisor | rare |
| Account corruption alert | within 60s | Supervisor | very rare |
| Critical (template propose failure, repeated rejections, lab crash) | as needed | Supervisor or Lab | rare |
| VIP HIGH severity | within 60s | Daemon | rare |

Per-trade fill emails include: symbol, qty, fill price, slippage vs expected, strategy name, leaderboard rank, conviction, stop price, current account equity.

Daily digest includes: today's trades, today's P&L, regime, active config version + 30d alpha vs SPY, lab activity overnight, role report cards, errors today, tomorrow's first scheduled event.

Weekly review includes: rolling 7d/MTD stats vs SPY, leaderboard top-5 with deltas, new templates added/rejected, drawdown chart, calibration drift, suggested attention items.

## 11. Beat-SPY-or-hold-SPY logic

On every Stock Scanner cycle, the daemon reads `Strategy Coach`'s `fallback_active` flag:

```
if not fallback_active:
    proceed with active strategy (momentum_v3 or whatever paper_active.json names)
else:
    Stock Scanner and Crypto Scanner stop opening new active-strategy positions.
    Existing positions continue to be managed by Order Steward + Portfolio Monitor
    (stops attached, trailing stops updated, stop-hits journaled) — they are simply
    not replaced after exit.

    Hold-SPY Coordinator manages the transition:
        - On day 1 of fallback: snapshot all active-strategy positions, plan 5-day exit
        - Each subsequent trading day at 15:55 ET: liquidate 1/5 of remaining active
          positions via Trade Executor
        - Each subsequent trading day at 15:55 ET: place SPY BUY orders proportional
          to freed equity (subject to Risk Officer gating like any other order)
        - On reverse: liquidate SPY 1/5 per day, resume active scanners
```

Strategy Coach evaluates flag flip daily at 06:00 ET:
- Flip to fallback: 30d alpha vs SPY < 1.5×.
- Flip from fallback: 30d alpha > 1.65× SPY AND > 1.5× for 5 consecutive trading days.

The 1.65× / 5-day hysteresis prevents whipsaw flapping in/out of fallback.

## 12. Autonomy enforcement

All roles inherit a fail-safe contract:

```python
class BaseRole(Role):
    def safe_run(self, ctx) -> RoleResult:
        try:
            return self.run(ctx)
        except RetryableError as e:
            return self._retry_with_backoff(e)
        except CredentialError as e:
            self._mark_blocked_on_creds()
            self._email_user_async(e)
            return RoleResult.blocked_on_creds()
        except FatalConfigError as e:
            self._write_pause_flag()
            self._email_user_async(e, severity=HIGH)
            return RoleResult.halted()
        except Exception as e:
            self._email_user_async(e, severity=HIGH)
            return RoleResult.error(e)
```

**Hard rule: no role uses `input()`, `getpass`, or any interactive prompt.** No role waits on user input. No role asks "are you sure." All decisions are policy-driven from active_config.json or hard-coded in the role.

The only manual touchpoint in the entire system is the `bot promote` CLI command, which is a deliberate, conscious decision the owner makes when paper has earned trust. Everything else runs hands-off.

## 13. LLM prompts

### 13.1 Strategy Architect (Saturday 06:00 ET, Anthropic API)

```
SYSTEM PROMPT (Strategy Architect)
==================================

You are the Strategy Architect of an autonomous trading system. Your weekly
job is to propose 1–3 new strategy templates as Python modules conforming to
the BaseStrategy Protocol.

YOUR INPUTS (all attached below):
1. The current leaderboard (top 10 strategy variants and their fitness).
2. The 3 worst-performing live trades of the past 30 days, with full context
   (entry signal, regime, what went wrong).
3. Regime distribution of the last 90 days (% time in trending_up, sideways,
   volatile_bear, etc.).
4. The Role Charters of every routine in the system, so you understand
   constraints and contracts.
5. The historical bar data summary (no peeking — only metadata about coverage).

YOUR OUTPUT FORMAT:
For each proposed strategy, emit a JSON object containing:
{
  "name": "snake_case_name_v1",
  "rationale": "1-2 paragraphs: WHAT regime/inefficiency this exploits, WHY
                you believe it can clear 1.5x SPY in walk-forward",
  "expected_regime": "trending_up | sideways | volatile_bear | mean_reverting",
  "code": "<full Python module text>",
  "tests": "<full pytest module text>",
  "params_to_search": { "param_name": [low, high, type] }
}

HARD CONSTRAINTS ON YOUR PROPOSED CODE:
- Must implement BaseStrategy Protocol (signature provided below).
- Imports allowed: pandas, numpy, ta, math, datetime, dataclasses, typing.
- Imports prohibited: os, sys, subprocess, requests, urllib, eval, exec.
- No I/O of any kind. No file reads. No network calls.
- All math must use only the bars and indicators passed in via the context object.
- Must run a 5-year backtest in under 30 seconds wall-clock on a single CPU core.
- Must include at least 3 unit tests covering: signal generation correctness,
  no-lookahead-bias property, edge case behavior at universe boundaries.
- Must NOT use future bars. Indicators must use only data ≤ current bar.
- Must declare its expected regime; will be selected only when regime matches.

QUALITY BAR (you should aim for, not always meet):
- Backtest 12mo rolling alpha vs SPY ≥ 1.8× across 6 walk-forward folds.
- Sortino ≥ 1.0.
- Max drawdown in backtest ≤ 25%.

WHAT YOU SHOULD AVOID:
- Refining momentum_v3 with slightly different RSI bands. The Param Optimizer
  does this nightly. Your job is novelty, not tuning.
- Trading rules that mostly look like the existing leaderboard top-3 with
  cosmetic tweaks. Propose genuinely different signal hypotheses.
- Anything that requires data we don't have (alternative data, options chains,
  level-2 order book — we have only daily/intraday bars + sentiment + EDGAR + GDELT).
- Strategies whose rationale is "this should work" without naming a specific
  market inefficiency or behavioral bias being exploited.

EXAMPLES OF GOOD NOVELTY:
- "Sector-relative mean reversion": when a stock's 5d return is in the bottom
  decile of its sector AND its sector is in the top quintile of all sectors,
  buy and exit when ranking equalizes. Exploits sector rotation lag.
- "Post-8K-positive drift": after EDGAR records a positive 8-K, buy in the
  24-72h window with volume confirmation. Exploits slow institutional digestion.
- "Sentiment-momentum convergence": when Polygon sentiment crosses +0.3 AND
  20d momentum > 0 AND VIX < 20, enter with tight stop. Exploits agreement
  between narrative and price.

EXAMPLES OF BAD PROPOSALS (will be rejected):
- "RSI < 25 buy" — too generic, already searched by Param Optimizer.
- "Train an ML model on..." — no ML beyond rule-based logic.
- "Buy if news mentions 'breakout'" — ad-hoc text matching, not robust.

You MUST emit valid JSON. Output nothing else. The Code Reviewer will check
your work; reject yourself early if you can't justify the rationale.
```

### 13.2 Code Reviewer (immediately after Strategy Architect)

```
SYSTEM PROMPT (Code Reviewer)
=============================

You are the Code Reviewer of an autonomous trading system. Your job is to
accept or reject each proposed strategy from the Strategy Architect.

You are paranoid by design. Your job is to find reasons to reject. The
Strategy Architect is incentivized to ship; you are incentivized to refuse
unsound code. Bias toward rejection when in doubt.

YOUR CHECKS (each must pass):

1. AST CHECK — Walk the Python AST. Reject if you find:
   - Any import outside the allowed list (pandas, numpy, ta, math, datetime,
     dataclasses, typing).
   - Calls to: eval, exec, open, compile, __import__, getattr-on-string,
     globals(), locals(), vars().
   - Use of os.*, sys.*, subprocess.*, network libs, file I/O.

2. LOOKAHEAD BIAS CHECK — Read the code. Reject if you find:
   - Any indicator computed using `df.iloc[-1]` while iterating over historical
     bars before that bar's timestamp.
   - Use of `shift(-N)` (pulls future values).
   - Indicator window that includes the current bar's close in computing the
     decision for the current bar (subtle off-by-one).
   - Use of statistics computed over the entire dataset (e.g. mean of all bars)
     to score historical bars (data leakage from future).

3. UNIT TEST CHECK — The submitted tests must:
   - Run in under 5 seconds total.
   - Include at least one no-lookahead test (assert that swapping bar N+1 with
     a wildly different value doesn't change the decision at bar N).
   - Pass.

4. CONTRACT CHECK — Verify:
   - Module exposes a class implementing BaseStrategy Protocol.
   - All declared params_to_search appear as constructor parameters.
   - Returns Decision objects with required fields.

5. RATIONALE CHECK — Read the proposal's rationale. Reject if:
   - Rationale doesn't name a specific market inefficiency or behavioral bias.
   - Rationale is "this should work" or "this is a momentum strategy" without
     specificity.
   - Rationale repeats existing leaderboard strategies with cosmetic tweaks.

6. NOVELTY CHECK — Reject if the strategy's signal logic, not just parameters,
   is substantially identical to any of the current leaderboard top-5
   (provided in attachment).

OUTPUT FORMAT (JSON only):
{
  "verdict": "accept" | "reject",
  "checks_passed": ["ast", "lookahead", "tests", "contract", "rationale", "novelty"],
  "checks_failed": [{ "check": "<name>", "reason": "<specific code line + reason>" }],
  "summary": "<one paragraph for the audit log>"
}

If verdict is "reject", checks_failed must be non-empty and reasons must be
specific (cite line numbers). If verdict is "accept", checks_failed must be empty.

Bias toward rejection. The cost of accepting bad code is real money lost.
The cost of rejecting good code is one week of delay. They are not symmetric.
```

Both prompts live in `prompts/strategy_architect.txt` and `prompts/code_reviewer.txt`, version-controlled. Lab loads them at runtime.

## 14. Operational policies

### 14.1 Cold-start backfill
On daemon restart, no retroactive job firing. APScheduler initializes with `coalesce=True, misfire_grace_time=0`. Missed scans are logged but never executed retroactively.

### 14.2 Migrations
Use Alembic. Daemon runs `alembic upgrade head` on startup before scheduling jobs. Migration files live in `migrations/versions/`. Existing positions in `trade_journal.db` are preserved; new tables added empty. First-boot establishes `equity_high_water_mark = current Alpaca equity`.

### 14.3 Backups
- Resource Guardian runs daily at 06:00 ET.
- SQLite `.backup` API to `data/backups/<YYYY-MM-DD>/state.db`, `trade_journal.db`.
- Sync to `~/Dropbox/trading-bot-backups/` (or iCloud Drive, owner choice; default Dropbox).
- 30-day retention, oldest pruned automatically.

### 14.4 Cost ceilings
- Anthropic monthly cap: $20 (warn at 80%, halt Strategy Architect at 100%).
- Polygon: respect rate limit (5 calls/min on basic plan); flat-rate, no overage.
- Alpaca: free for paper.
- Halts are temporary; ceiling resets on the 1st of each calendar month.

### 14.5 Logging
- Structured JSON logs to `runs/<YYYY-MM-DD>/<role>/<HH:MM:SS>.json`.
- One file per role-run.
- Stdout also captured by launchd to `/var/log/com.bharath.trading.*.log`.
- Rotated nightly; archived after 90 d to `runs/_archive/<YYYY-MM>.tar.gz`.

### 14.6 Multi-environment separation
- `~/trading-bot/paper/` — paper daemon, paper config, paper state.db, paper credentials.
- `~/trading-bot/live/` — live daemon, live config, live state.db, live credentials. Created only on first `bot promote`.
- Each has its own launchd plist and `.env`.
- Lab runs in `~/trading-bot/paper/` and only writes paper.
- Filesystem ACL: lab process has no write permission on `~/trading-bot/live/`.

### 14.7 Order rate limiting
Cap at 3 new BUY positions per scan, prioritized by conviction score. Excess logged but not traded. Reconsidered next scan.

### 14.8 Hold-SPY hysteresis
Resume only when 30d alpha is above 1.65× SPY AND has been > 1.5× for 5 consecutive trading days.

### 14.9 Wash-trade fallback
On `40310000`: cancel conflicting open order, retry once as bracket OCO. If still rejected, blacklist symbol for 24 h, email. Symbol re-enters universe automatically next day.

### 14.10 Earnings policy
- T-1 on held: trim to 50% by EOD.
- T-0 on held < 7 days: full exit by EOD T-1.
- T-0 on held ≥ 7 days: hold through.
- Never open new position with earnings in next 3 trading days.

## 15. Phased implementation

| Phase | Duration | Deliverable |
|---|---|---|
| 1 — Operational hardening | 1 week | launchd daemon + supervisor + watchdog + heartbeat + drawdown pause + JSON logging + email infrastructure overhaul. Existing strategies keep running, but now reliably. |
| 2 — Role pattern + KPIs | 1 week | Role Protocol, charter docstrings on all 26 routines, ReportCard generation, role_runs table, daily digest gains report-card section. |
| 3 — Param Optimizer + Promoter | 1 week | Bayesian search via optuna, walk-forward backtest, leaderboard, auto-promote into paper config, beat-SPY gate. |
| 4 — Strategy Coach + Hold-SPY | 0.5 week | 30d alpha vs SPY tracking, fallback flag, Hold-SPY Coordinator's 5-day exit + reverse logic. |
| 5 — Strategy Architect + Code Reviewer | 1.5 weeks | Anthropic-driven template proposal + AST/sandbox review + auto-merge into rotation. Calibrator. |
| 6 — `bot promote` CLI + live daemon | 0.5 week | Manual paper→live promotion with frozen snapshot + ACL enforcement. Second daemon instance for live. |

Total: ~5.5 weeks. Phase 1 alone fixes today's failure mode. Phases 3–6 build the autonomous-evolution capability incrementally.

## 16. Test strategy

### 16.1 Per-role tests
Each role gets `tests/roles/test_<name>.py` with:
- **Contract test:** does it implement `Role` Protocol? Are charter fields populated?
- **Behavior test:** given specific inputs, does it produce specified outputs?
- **KPI calculation test:** given a history of runs, does `report_card` produce expected KPI values?
- **Failure test:** does it fail-safe correctly (no exceptions escape, correct `RoleResult.error` on all error paths)?

### 16.2 Integration tests
- Daemon startup test: full cold-start with fixture state, verify all roles register, scheduler boots, heartbeat fires.
- Lab cycle test: mock Anthropic API, verify Strategy Architect → Code Reviewer → Param Optimizer → Promoter chain works end-to-end on a fixture leaderboard.
- Supervisor recovery test: kill daemon process, verify watchdog restarts within 5 min.

### 16.3 Walk-forward backtest tests
The Backtest Engineer itself is tested: given a known strategy with known historical performance, does it reproduce known fitness values? (Determinism check.)

### 16.4 Sandbox tests
Test that AST allowlist actually rejects prohibited imports. Test that resource limits actually fire on a deliberately slow strategy.

## 17. Dashboard updates

Existing dashboard at port 8765 is extended:
- `/roles` — live status of all 26 roles, last-run timestamp, current health, last KPI value.
- `/leaderboard` — current top-10 with fitness deltas and promotion eligibility.
- `/history/equity` — equity curve vs SPY, with annotations for fallback enter/exit.
- `/audit/<routine>` — recent role runs with full context (input snapshot, output, errors).
- `/calibration` — backtest-vs-paper drift over time.

No layout overhaul. Routes added incrementally as roles ship.

## 18. Open questions / future work

- **Leveraged ETF rotation as a hold_spy alternative.** When in fallback, should we hold SPY or rotate among IVV/QQQ/IWM based on regime? Defer; keep SPY for v1.
- **Sector-tilt overlay.** Could add a sector-momentum tilt on top of the active strategy. Defer.
- **Options.** Currently disallowed. Reconsider when paper account has 6+ months of stable alpha.
- **Live capital ramp policy.** When `bot promote` first runs, what's the live notional cap? Recommendation: start with 10% of paper notional, double monthly if 30d realized alpha holds, until 100% of paper notional. Codify in spec for v2.
- **Paper-vs-live divergence handling.** If live underperforms paper by > 20% Sharpe, do we auto-pause live? Defer to first promotion.
- **Multiple concurrent strategies.** Currently one active template at a time. Could run a portfolio of templates with capital weighted by fitness. Defer to v2.

## 19. Acceptance criteria

The system is considered "shipped" when, on a representative trading week:
1. Watchdog auto-restart triggers at least once and recovers without owner action.
2. Daily digest arrives at 18:00 ET ± 5 min, every weekday.
3. At least one auto-promote of paper_active.json occurs from lab activity.
4. Strategy Architect proposes at least one template; Code Reviewer rejects at least one (proving the gate works).
5. Calibrator reports backtest-vs-paper correlation > 0.5 (not yet 0.7 — that's the long-term target).
6. Schedule Auditor reports zero missed scan windows for 5 consecutive trading days.
7. No interactive prompts encountered during normal operation.
8. Owner did not need to log in to fix anything during the week.

When all 8 hold, the system has cleared the bar for "autonomous."
