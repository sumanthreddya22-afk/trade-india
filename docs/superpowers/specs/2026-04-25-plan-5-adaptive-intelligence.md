# Plan 5 — Adaptive Intelligence Design

**Date:** 2026-04-25
**Owner:** bharath8887@gmail.com
**Account:** Alpaca Paper Trading ($15,000 simulated)
**Phase:** Plan 5 (additive; Plans 1–4 remain in place during migration)

---

## 1. Problem Statement

The current bot (Plans 1–4) trades a hardcoded 7-symbol watchlist on a flat 15-min cadence, with static concentration caps and no real-time sentiment input. This caps the strategy's reach in three ways:

1. **Universe is too narrow.** ~10,000 tradable Alpaca assets; we look at 7. Most non-mega-cap setups are invisible.
2. **Risk caps are static.** A 5% concentration cap is correct for a low-conviction sideways trade and wrong for a high-conviction trending-up leader. Same for sizing.
3. **Cadence is uniform.** Same 15-min frequency at 9:30, 13:00, and Saturday 03:00 — wastes compute, burns cache, and misses event-driven moves.
4. **No VIP-tweet awareness.** Trump and Musk routinely move sectors with single posts. The bot is blind to this.
5. **Reports are ugly.** Plain HTML tables, no charts, no equity curve, not mobile-friendly.
6. **No discovery loop.** The bot doesn't learn about new strategies on its own.

Plan 5 addresses all six.

## 2. Goals

**Primary:** Capital preservation (unchanged from prior plans). Drawdown < 15% always.
**Secondary:** Beat S&P 500 with higher Sharpe over 12-month rolling window.
**New for Plan 5:**
- Trade from the full liquid US universe (~3,000 names) + crypto + commodity ETFs.
- Make every risk lever respond to context (regime, asset class, conviction, volatility, correlation, loss streak).
- Cut compute waste with tiered + event-driven cadence.
- Treat Trump / Musk posts as veto-or-boost signals (not entry signals).
- Email reports humans actually want to read.
- Surface new research ideas nightly for human approval.

### Success Criteria

- All hard guardrails preserved: 2% daily / 5% weekly halt, mandatory bracket stops, paper-only.
- Stage-1 daily screener completes in < 60 seconds against full Alpaca universe.
- Stage-2 lane scoring on 100-name shortlist completes in < 10 seconds per cycle.
- Sentiment-shock event triggers a sector-veto application in < 5 minutes from tweet publication (free-source target).
- Email reports render correctly in Gmail web + Gmail iOS + Apple Mail (light + dark mode).
- 90%+ unit-test coverage on new modules; property tests on `resolve_dynamic_limits`.

## 3. Non-Goals

- **Options trading.** Deferred to Plan 6 (separate brainstorm). Current `options_max_pct` config remains 0 in practice.
- **Futures.** Alpaca paper does not support; commodity exposure stays via ETF proxies (GLD, SLV, USO, UNG, GDX, XLE, URA).
- **Real-time X / Twitter.** Free sources only in v1; X API Basic ($200/mo) is a future plug-in via `SentimentSource` interface.
- **Auto-implementation of researched strategies.** Research scout proposes; user approves; Claude codes a follow-up plan. Bot never self-modifies strategies without explicit approval.
- **Live trading.** Paper-only enforcement persists.

## 4. Inspiration: Trading Codex Project

A previous trading project (`/Users/bharathkandala/Documents/trading codex`) independently developed several patterns we lift verbatim or with adaptation:

- **Smart cadence with state-file gating** — scheduler fires every 5 min; the script reads `last_run` and exits early if not due. Lets one cron back multiple cadences.
- **`RiskManager.resolve_dynamic_limits(order, state, regime)`** — chained multipliers (regime × asset_class × loss_streak) against base/ceiling caps. Already implemented in codex with tests; we extend with conviction × volatility × correlation.
- **Lane-based parallel scanner** with `ThreadPoolExecutor`, each lane returns ranked candidates merged into a top-N markdown file.
- **Markdown system-of-record** — `latest_intelligence.md`, `opportunities.md`, `market_state.md`, `decisions.md`, `performance.md`. Decouples data refresh (writers) from reasoning (readers); auditable, debuggable.
- **SPY-relative scoring** — every equity candidate scored on (1d return) + (5d return − SPY 5d) + volume_ratio. Filters out "rising tide" lift and surfaces real leaders.
- **Universe pull via `alpaca.get_active_assets("us_equity")`** then bucketed by exchange / fractionable / shortable / theme keywords.

What codex *did not* build (and what Plan 5 adds): full liquidity-screened universe (codex hardcoded 17 names), conviction/volatility/correlation in dynamic limits, VIP-tweet ingest, rich emails with charts, nightly research scout.

## 5. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ DATA LAYER (refreshed by scheduled writer scans → markdown snapshots)│
│  Alpaca universe │ FRED macro │ GDELT │ SEC EDGAR │ Truth RSS │      │
│  Nitter mirrors (best-effort) │ research-scout web search            │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STRATEGY STORE (markdown, system-of-record)                          │
│  latest_intelligence.md · market_state.md · opportunities.md ·       │
│  decisions.md · performance.md · sentiment_log.md · rules.md ·       │
│  research_pipeline.md                                                │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE-1 SCREENER (daily 08:30 ET)                                    │
│  ~3,000 liquid US equities + commodity ETFs + Alpaca crypto          │
│  → liquidity gate (price > $5, ADV > $5M) → 100-name shortlist       │
│  → sector tags + earnings-window flags                               │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE-2 LANES (intraday, parallel)                                   │
│  momentum │ mean-reversion │ breakout │ event/news                   │
│  + tweet-injected names (bypass stage-1 with sentiment-event tag)    │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ DYNAMIC RISK MANAGER                                                 │
│  resolve_dynamic_limits(                                             │
│    regime × asset_class × loss_streak                                │
│    × conviction × volatility × correlation                           │
│  ) → per-trade cap, position cap, concentration cap                  │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
            ┌──────── EVENT BUS ────────┐
            │ · VIP tweet               │ ─→ sentiment-shock mini-scan
            │ · VIX +10% intraday       │ ─→ regime-recheck
            │ · Held name ±3% in 5min   │ ─→ managed-position-check
            │ · Daily/weekly halt       │ ─→ cancel pending + email
            └─────────────┬─────────────┘
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ EXECUTION (Alpaca bracket orders, atomic entry+stop+target)          │
└─────────────────────────────────┬────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ RICH REPORTING                                                       │
│  Jinja2 HTML + matplotlib base64-PNG charts                          │
│  Variants: mid-day · EOD · event alert · nightly research            │
└──────────────────────────────────────────────────────────────────────┘
```

## 6. Components

### 6.1 New Modules

| Module | Purpose | LOC est. |
|---|---|---|
| `universe.py` | Pull Alpaca tradable assets, apply liquidity/price screen, write `latest_intelligence.md` | ~200 |
| `screener.py` | Stage-1 daily wide screen → 100-name shortlist; Stage-2 lane scoring orchestration | ~350 |
| `strategy_lanes.py` | 4 lanes (momentum, mean-reversion, breakout, event); each `Lane` returns ranked candidates | ~300 |
| `correlation.py` | Pairwise correlation across held + candidate names; produce penalty multiplier | ~120 |
| `volatility.py` | 20-day realized vol per symbol + vol-bucket multiplier | ~80 |
| `sentiment/sources.py` | Adapters: `TruthSocialRSS`, `GdeltVipFilter`, `NitterRSS` (best-effort). `SentimentSource` Protocol for plug-in. | ~250 |
| `sentiment/analyzer.py` | Score tweet → `(polarity, magnitude, affected_sectors, affected_symbols)` via Claude API structured output | ~150 |
| `sentiment/policy.py` | Apply veto + boost; pause sector entries; bump conviction; append `sentiment_log.md` | ~150 |
| `event_bus.py` | Detect cadence-overrides (tweet, VIX, position move, halt). Dispatch mini-scans. | ~180 |
| `cadence.py` | `is_scan_due(now, last_run, tier)` — port codex pattern; tier-aware base + event override | ~100 |
| `research_scout.py` | Nightly 19:00 ET; web-search new trading strategies; append `research_pipeline.md`; email | ~250 |
| `reports/templates/` | Jinja2 HTML — `mid_day.html`, `eod.html`, `event_alert.html`, `nightly_research.html`, partials | ~400 |
| `reports/charts.py` | matplotlib renderers → base64 PNG: equity curve, sparkline, regime gauge, sector heatmap, risk meter | ~250 |

### 6.2 Modified Modules

| File | Change |
|---|---|
| `risk_manager.py` | Add `resolve_dynamic_limits()` (lifted from codex), extended with `conviction_mult`, `volatility_mult`, `correlation_penalty`. Static config becomes base/ceiling. |
| `orchestrator.py` | Replace fixed-watchlist scan with two-stage flow: read shortlist + run lanes. Honor event-bus overrides. |
| `regime.py` | No internal change; called more often via event bus on VIX spikes. |
| `intelligence.py` | Extend with `collect_market_universe` (codex pattern); add Truth Social RSS adapter wiring. |
| `reports.py` | Replace bare `<table border='1'>` with Jinja templates + chart calls. Backwards-compat shim removed. |
| `cli.py` | New commands: `bot screen`, `bot sentiment-scan`, `bot event-fire <type>`, `bot rich-report --period mid\|eod\|event`, `bot nightly-research`. |
| `strategy/config.yaml` | Add `dynamic_risk:`, `sentiment:`, `cadence:`, `research:` sections with safe defaults. |
| `strategy/watchlist.yaml` | Rename to `core_watchlist.yaml`. Holds 7 anchor symbols always-monitored. Discovery flows from screener. |

### 6.3 Strategy Store Files

| File | Written by | Read by | Format |
|---|---|---|---|
| `latest_intelligence.md` | universe scan | screener, reports | overwritten each scan |
| `market_state.md` | regime detector | orchestrator, reports | overwritten when regime changes |
| `opportunities.md` | screener (cadence-gated) | orchestrator, reports | overwritten each scan-due cycle |
| `decisions.md` | orchestrator | weekly evolve, reports | append-only |
| `sentiment_log.md` | sentiment policy | orchestrator, reports | append-only |
| `performance.md` | reconciler + evolve | reports, evolve | overwritten weekly |
| `rules.md` | weekly evolve | orchestrator, reports | append + section rewrites |
| `research_pipeline.md` | research scout | user (read), implement plans | append-only with state field |

## 7. Dynamic Risk Formula

```
final_cap = base_cap
          × regime_mult        (risk_off 0.40 → trending_up 1.50)
          × asset_class_mult   (option 0.50, crypto 0.80, stock 1.00, etf 1.10)
          × loss_streak_mult   (0 days 1.00 → 3 days 0.55, floor 0.50)
          × conviction_mult    (rank-bottom 0.70 → rank-top 1.30)
          × volatility_mult    (high-vol 0.60 → low-vol 1.20, inverse 20d realized)
          × correlation_penalty (uncorrelated 1.00 → fully-correlated 0.50)
clamped to [base_floor_pct, hard_max_ceiling_pct]
```

Same formula resolves `per_trade_risk_pct`, `max_position_pct`, `max_symbol_concentration_pct` (with formula-specific multipliers — concentration uses 1.5× regime in trending_up vs 1.25× for sizing, mirroring codex).

**Hard guardrails (never auto-tuned):**
- Daily 2% / weekly 5% circuit breaker → halt
- Stop-loss mandatory on every entry (atomic bracket order)
- Paper-only enforcement
- `halted=true` requires manual reset

## 8. Cadence

**Base tiers** (one cron fires every 5 min; `is_scan_due()` decides if work runs):

| Window (ET) | Cadence | Rationale |
|---|---|---|
| 09:25–10:00 | 5 min | Open volatility, opportunity-rich |
| 10:00–15:30 | 15 min | Steady-state |
| 15:30–16:00 | 5 min | Closing prints, MOC risk |
| 16:00–18:00 | 30 min | After-hours news catches |
| 18:00–next 09:25 | 2 hr | Crypto + Asia + overnight news |
| Weekends | 4 hr | Crypto only |

**Event-driven overrides** (fire mini-scans on top of base):

- **VIP tweet** → sentiment-shock mini-scan: re-score affected sector only, apply veto/boost, alert email if material.
- **VIX intraday +10%** → regime-recheck full scan; may flip allocation cap.
- **Held position ±3% in 5 min** → managed-position-check on that one symbol.
- **Daily/weekly P&L breach** → halt event: cancel pending, urgent email, set `halted=true`.

**Daily fixed-time work (no gating, runs on cron):**

| Time (ET) | Routine | Purpose |
|---|---|---|
| 08:30 | premarket-screen | Refresh 100-name shortlist |
| 12:30 | rich-report-mid | Mid-day rich email |
| 16:30 | rich-report-eod | EOD rich email + reconcile + journal sync |
| 19:00 (M–F + Sun) | nightly-research | Web-search new strategies → email |
| Sat 10:00 | weekly-evolve | Performance review, rule-tweak proposals |

## 9. VIP-Tweet Sentiment Pipeline

**Sources (free, v1):**
- `TruthSocialRSS` — `trumpstruth.org/feed` polled every 5 min. ~5 min latency. Trump-only.
- `GdeltVipFilter` — GDELT articles filtered for Trump/Musk mentions. ~15 min latency, news-amplified only.
- `NitterRSS` — best-effort polling of public Nitter mirrors for `@elonmusk`. Often blocked; treated as bonus, not load-bearing.

**`SentimentSource` Protocol** lets us add `XApiSource` or paid third-party later by adding one adapter.

**Analyzer:**
- Each new post → Claude API call with structured output prompt:
  ```
  Output JSON: {
    polarity: -1.0..+1.0,
    magnitude: 0.0..1.0,
    affected_sectors: [list],
    affected_symbols: [list],
    rationale: 1-line
  }
  ```
- Use prompt caching (system prompt + few-shot examples cached per session).

**Policy (Veto + Boost):**

| Severity | Action |
|---|---|
| polarity ≤ −0.6 AND magnitude ≥ 0.7 (severe systemic) | Halve all new sizing for 4 hours; alert email |
| polarity ≤ −0.4 (sector negative) | Veto new entries in `affected_sectors` for 4 hours |
| polarity ≥ +0.4 (specific positive) | Boost conviction +0.2 for `affected_symbols`; inject into stage-2 |
| -0.4 < polarity < +0.4 (neutral) | Log only |

**Existing positions are not liquidated on a tweet.** Stops stay in place; tweets only affect new entries and sizing.

**Logging:** every tweet (acted on or not) appended to `sentiment_log.md` with timestamp, source, polarity, action taken.

## 10. Rich Email Reports

**Visual elements (all inline base64 PNG):**
- Equity curve — bot vs SPY, 30 days (matplotlib line chart)
- Sparkline column per held position (5-day mini-chart inside the position table)
- Color-coded P&L bars (green/red gradient by magnitude)
- Regime indicator gauge (trending_up / sideways / down / risk_off)
- Sector heatmap of current book
- Risk-utilization meter (daily-loss-limit usage, position-cap usage)

**Sections (top-down, EOD variant):**
1. Headline strip — equity, day Δ, week Δ, month Δ, vs SPY YTD — color badges
2. Equity curve chart
3. Open positions — sparkline + dynamic stop distance + conviction score + P&L bar
4. Today's decisions — bought/sold/skipped with reason badges (momentum / mean-rev / sentiment-veto / etc.)
5. Top 5 opportunities for next cycle — score + 1-line thesis
6. Sentiment radar — last 24h tweet alerts: source, score, action taken
7. Regime & macro panel — VIX, 10Y, SPY 50/200 EMA, current allocation pie
8. Risk gauges — daily/weekly P&L vs limits, biggest concentration
9. Claude's notes — auto-generated 2–4 line freeform observations

**Variants:**
- **Mid-day (12:30 ET)**: sections 1–4 + 6 (compact)
- **EOD (16:30 ET)**: all 9 sections
- **Event alert**: short-form, single-section template per event type
- **Nightly research (19:00 ET)**: card-style, one card per proposal

**Style:**
- Inline CSS only (Gmail strips `<style>` blocks unreliably)
- Mobile-friendly (max-width 600px, table-based layout for legacy Gmail)
- Dark-mode aware (semantic colors that flip in `@media (prefers-color-scheme: dark)`)

## 11. Nightly Research Scout

**Schedule:** 19:00 ET, weekdays + Sunday (skip Saturday).

**Sources searched (free):**
- arxiv q-fin (last 24–48h)
- SSRN finance papers (last 48h)
- Quantocracy aggregator
- r/algotrading top posts (last 24h)
- GitHub trending in `algorithmic-trading` topic
- Two Sigma / AQR / Citadel public research blogs
- Seeking Alpha quant-tagged articles

**Filter criteria:**
- Applicable to retail / paper trading
- Compatible with our universe (US equities + crypto + commodity ETFs)
- Compatible with capital-preservation-first risk profile (no high-leverage strategies, no naked options)
- Implementation feasibility within 1–2 weeks of work

**Output:** `strategy/research_pipeline.md` (append-only) + email card per proposal.

Each entry:
```
- id: 2026-04-25-001
  title: "Term-Structure Momentum on Bond ETFs"
  source: "arxiv.org/abs/2604.12345"
  summary: "..."
  why_it_might_help: "..."
  complexity: M  # S/M/L
  risk_introduced: low  # low/med/high
  state: proposed  # proposed | approved | rejected | implemented
  proposed_at: 2026-04-25T19:00:00Z
```

**Approval workflow:** user reads email, replies in chat with which to implement; Claude drafts a follow-up plan; bot does not auto-modify strategies.

## 12. Migration Strategy

Plan 5 is **additive**. Plan 1–4 routines remain operational while Plan 5 modules deploy alongside. Cutover happens module-by-module:

1. New module ships with tests passing.
2. Deployed in parallel (e.g., new screener writes `opportunities.md`; old orchestrator still uses `core_watchlist.yaml`).
3. After ~3 days of clean runs, orchestrator switches read-path to new file.
4. Old code removed in cleanup commit.

Hard guardrails (circuit breakers, stop-loss mandate, paper-only) preserved across the migration. Any guardrail change requires a separate explicit user-approved spec.

## 13. Testing Strategy

- **Unit tests** per module, ≥90% line coverage on new code.
- **Property tests** on `resolve_dynamic_limits` (Hypothesis):
  - Output never exceeds base ceiling regardless of inputs
  - Output never below configured floor
  - Multipliers all in valid declared ranges
  - Monotonicity: same inputs → same output
- **Integration test** with replay fixture: canned tweet → sentiment scan → veto applied → orchestrator skips sector → email rendered. End-to-end, mock Alpaca client.
- **Snapshot tests** on rich email HTML: render fixture data → assert section structure (headers, chart `<img>` tags, color tokens) + golden-file diff for layout regressions.
- **Backtest harness** (`bot backtest --start 2025-01 --end 2026-04`): replay historical bars + canned events through strategy. Sanity tool only — not a full backtester.
- **Email rendering** verified manually in Gmail web, Gmail iOS, Apple Mail (light + dark) once per implementation milestone.

## 14. Open Questions / Future Work

- **Plan 6:** Options layer (covered calls, cash-secured puts first; long calls/puts second; multi-leg later). Needs its own brainstorm.
- **Plan 7:** Real-time X API integration ($200/mo) when paper proves out and a live-trading discussion happens.
- **Plan 8:** Reddit / WSB sentiment overlay (codex spec listed it; defer until VIP-tweet pipeline is stable so we have a sentiment infra to reuse).
- **Plan 9:** QuiverQuant ($10/mo) for congress trades + insider buys; Unusual Whales ($50/mo) for options flow.
- **Plan 10:** Replace markdown system-of-record with a small Postgres or DuckDB store once volume justifies it. Markdown stays as the human-readable view.

## 15. References

- Trading Codex project: `/Users/bharathkandala/Documents/trading codex` (copied to `.codex-inspiration/` for reference reading)
- Existing spec: `docs/superpowers/specs/2026-04-25-trading-bot-design.md`
- Plans 1–4: `docs/superpowers/plans/`
