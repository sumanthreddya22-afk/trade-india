# Trading Bot v4 — Phase 13: Event-Driven Curator Architecture

**Author:** drafted 2026-05-15 (Bharath + Claude planning session)
**Status:** ready for execution in a fresh build session
**Scope:** ~10 sub-phases, each one focused session worth of work
**Tests baseline:** 621 passing at HEAD `150e061`
**Constraint:** Every commit leaves the tree green; `boot_check.py`, `verify_ledger.py`, full test suite all pass before commit. CLAUDE.md hard rules respected.

---

## 1. North star

Move the bot from **pull-based universe-scan strategies** (today's v3 strategies iterate a policy-locked symbol list every weekday at 15:30 ET) to **event-driven signal harvesting** (curators continuously detect tradeable events; each event triggers symbol-specific research → gated decision → execution → monitor → postmortem).

Universe is no longer pre-defined. The universe is **whatever the curators surface**. Each event runs through the same hardened gate stack so risk discipline is invariant.

This phase brings:

- 9 new event-detector curators (insider clusters, news velocity, options flow, etc.)
- Deterministic feature_brief + LLM scout/adversary split (backtestable, hallucination-resistant)
- Alpha-at-entry gate (don't trade signals already played out)
- Heuristic pre-mortem (catch pump/laddering/lockup traps before burning LLM budget)
- 4 new risk caps (liquidity, spread/depth, proximity, sector/correlation)
- Per-kill response level matrix (L1/L2/L3) replacing uniform halt-new
- 3-layer monitor (WS event-driven + intraday + daily)
- EOD reconciliation + weekly cohort postmortem + monthly mutation council
- Cockpit "Flows" tab with per-lane pipelines + per-source health grid + recent policy changes panel
- Policy model simplification (drop bureaucratic time gates; keep statistical + data accumulation gates)

---

## 2. Decisions made in the 2026-05-15 planning session

These decisions are inputs to every sub-phase below. **Do not re-debate during execution.**

### 2.1 Cockpit "3 flows" definition

Three strategy lanes mirroring the existing risk-kernel lane structure:

| Lane | Strategies (existing + new) |
|---|---|
| **Equity** | DUAL_MOMENTUM_v1/v3, ETF_MOMENTUM_v1/v3, INSIDER_CLUSTER_v1 (new), NEWS_VELOCITY_v1 (new), EARNINGS_DRIFT_v1 (new), OPTIONS_FLOW_v1 (new), SHORT_SQUEEZE_v1 (new), 13D_ACTIVIST_v1 (new), CONGRESS_TRADE_v1 (new) |
| **Crypto** | CRYPTO_MOMENTUM_v1/v3, CRYPTO_ONCHAIN_v1 (new), CRYPTO_NEWS_VELOCITY_v1 (new) |
| **Options-Wheel** | SPY_WHEEL_v1/v3 |

### 2.2 Phasing

Architecture-first (B → D → C → A → E → F per the planning conversation). Concretely: foundations + new architecture → external sources → cockpit → personas/locks → strategies.

### 2.3 Policy model — bureaucratic gates dropped

Three buckets, three treatments:

| Bucket | Examples | Treatment |
|---|---|---|
| **A. Bureaucratic gates** | 7-day cooldown for loosening, 30-day dry-run windows | **Dropped.** Audit + hash chain + immutable ledger + operator discipline are the safety net. |
| **B. Statistical gates** | Tier 1/2/3 validation, BH-FDR, DSR/PBO bars, 90-day failure_memory | **Kept strict.** Numbers may tune; existence does not. |
| **C. Data accumulation gates** | MVP-OP 60-day recon, ALPHA 365-day paper, 10-trade promotion bar | **Kept absolute.** No code can shortcut wall-clock evidence. |

Each new lock fits one of three patterns:

1. **Audit-only:** dated, hashed, change logged to `policy_change_event`. No timer.
2. **Mode-flagged:** as above + `mode={dry_run, enforce}` field operator flips when convinced.
3. **Numerical:** as audit-only; tightening effective next cycle; loosening also effective next cycle but surfaced in cockpit's "Recent Policy Changes" panel for visibility.

### 2.4 Promotion bar

`tiny_paper` → `tiny_live` requires:
- 10 paper trades minimum, AND
- Tier-3 validation artifact (DSR ≥ floor, PBO ≤ ceiling, walk-forward holdout pass)

(Existing `paper_fast_track` waiver remains for `research_only` → `tiny_paper` per operator override.)

### 2.5 LLM call budget

**500 calls/day** (was 180). Set in `shared/llm_transport.py`. Budget headroom for: 8 curators × ~10 calls + research_bot ~30 + mutation_cycle ~50 + cohort_reviewer ~10 + drift_postmortem ~10 + universe_audit ~10 + adversary calls ~30 ≈ 230 baseline. ~270 buffer for spikes / new jobs.

### 2.6 Source reliability — weighted 0–1

Each source in `source_registry.lock` has a `trust_score` in [0.0, 1.0]. When multiple sources contradict (e.g., Polygon news positive, GDELT tone negative), `feature_brief` aggregator does weighted average. Sources with `trust_score < 0.3` excluded from gate inputs.

Seed weights (operator can tune):

| Source category | Initial trust_score |
|---|---|
| SEC EDGAR (filings) | 1.0 |
| Polygon (filings/financials/options/ratings) | 0.95 |
| Polygon Benzinga news | 0.85 |
| Polygon news (general) | 0.75 |
| GDELT 2.0 | 0.7 |
| Yahoo Finance, Finnhub, Marketaux | 0.55 |
| PR Wires (BusinessWire/PRNewswire/GlobeNewswire) | 0.8 (high — these are primary sources) |
| Hacker News, Seeking Alpha | 0.4 |
| Reddit, StockTwits | 0.3 (vol-filter only, not direction signal) |
| Capitol Trades scrape | 0.7 |
| CryptoQuant, CoinGlass, DefiLlama | 0.85 |

### 2.7 Kill switch response matrix

Three response levels with per-kill mapping:

| Level | New entries | Existing positions | Autonomous exits | Writes |
|---|---|---|---|---|
| **L1 — Lane halt** | Blocked in affected lane | Managed normally | Run | OK |
| **L2 — Global halt** | Blocked everywhere | Managed normally | Run | OK |
| **L3 — Read-only** | Blocked | Operator only | **Suspended** | Blocked |

| # | Kill | Level | Escalation | Auto-clear |
|---|---|---|---|---|
| 1 | sqlite_integrity | L3 | n/a | No — operator |
| 2 | policy_hash_mismatch | L3 | n/a | No — operator |
| 3 | recon_mismatch | L2 | → L3 after 30 min unresolved | Yes — when next recon matches |
| 4 | unknown_position | L2 | n/a | Yes — when classified |
| 5 | data_freshness | L1 (per lane) | → L2 if ≥2 lanes stale simultaneously | Yes — when fresh |
| 6 | broker_api_error_rate | L2 | n/a | Yes — when error rate < threshold for N min |
| 7 | clock_skew | L2 | n/a | Yes — when skew < threshold |
| 8 | intraday_pnl_floor | L2 circuit-breaker | n/a | **No** — operator clears next session |
| 9 | manual_operator_halt | Operator-selectable L1/L2/L3 | n/a | Operator |

**Force-flatten is NEVER automatic.** Always operator-initiated. Existing stop-losses already protect downside.

### 2.8 Intraday P&L floor — dynamic, regime-aware

Floor scales with VIX / regime classification:

| Regime | Floor |
|---|---|
| `risk_off` | −1.5% of equity |
| `neutral` | −2.0% |
| `risk_on` | −2.5% |

Implementation: floor read from `policy/intraday_pnl_floor.lock` at each tick; floor selection driven by `regime_event` table latest row.

### 2.9 Source health degradation — heartbeat-based

Per source:
- `green` if `last_fetch_age < expected_cadence × 2`
- `yellow` if `last_fetch_age < expected_cadence × 5`
- `red` if older

`expected_cadence` is part of each source's `source_registry.lock` entry. Cockpit shows per-source colored chip.

### 2.10 Cockpit "Recent Policy Changes" panel

Always visible at the footer of the Flows tab. Last 10 entries from `policy_change_event` table:
- Date
- Lock file changed
- Operator
- One-line diff hint
- Tightening (green up-arrow) / Loosening (red down-arrow) / Informational (gray dot) icon

---

## 3. Architecture overview

### 3.1 End-to-end pipeline (every signal flows through this)

```
[1] Curator (poll or WS)
      │
      ↓
[2] Intake: candidate_event + priority queue + dedup + confluence
      │
      ↓
[3a] feature_brief builder (deterministic Python, parallel fetch)
      │     → writes feature_brief row
      ↓
[3b] Scout (LLM, Sonnet, no tools) → writes strategy_decision row
      │
      ↓
[4a] Heuristic pre-mortem (deterministic kills)
      │     → reject path or continue
      ↓
[4b] Adversary (LLM, Opus, no tools) → writes annotations
      │
      ↓
[4c] Alpha-at-entry gate (deterministic)
      │     → reject if alpha_remaining < threshold; size_multiplier otherwise
      ↓
[5]  Risk precheck (existing 7 caps + 4 new caps + halt_router state)
      │
      ↓
[6]  Order submit (existing)
      │
      ↓
[7]  Fill (existing)
      │
      ↓
[8]  Monitor — 3 layers (WS event + intraday loop + daily sweep)
      │
      ↓
[9]  Close fill
      │
      ↓
[10] Postmortem (per-trade Haiku/Sonnet)
      │
      ↓
[11a] EOD recon (daily, new)
[11b] Weekly cohort reviewer (new, Opus)
[11c] Monthly mutation council (existing)
```

### 3.2 Data flow

```
Source ──poll/WS──> curator daemon ──parse──> candidate_event
                                                       │
                                              priority queue
                                                       │
                                              intake router
                                                       │
                                              [Polygon REST/WS]
                                              [external sources]
                                                       │
                                              feature_brief builder ──> feature_brief
                                                                              │
                                                                       Scout LLM
                                                                              │
                                                                  strategy_decision
                                                                              │
                                                          alpha gate + heuristic kills
                                                                              │
                                                                       risk precheck
                                                                              │
                                                                       order_master, etc.
```

---

## 4. Implementation manifest

Full inventory of what gets built. Numbered for cross-reference.

### 4.1 New ledger tables (Phase 13.1)

| # | Table | Purpose | Hash-chained? |
|---|---|---|---|
| L1 | `candidate_event` | One row per detected event (insider Form 4, news spike, options sweep, etc.) | Yes |
| L2 | `feature_brief` | Structured features assembled by deterministic builder; consumed by Scout LLM | Yes |
| L3 | `source_health` | Heartbeat per source (last fetch, error count, latency p50/p95) | No (mutable, time-series) |
| L4 | `source_event_log` | Per-call audit (source, endpoint, status, latency, bytes) | No (rotates after 30d) |
| L5 | `policy_change_event` | Every lock file write logged with diff hint + tightening/loosening tag | Yes |
| L6 | `kill_switch_escalation` | Per-kill timer state (fired_at, escalates_at, escalated?) | Yes |
| L7 | `cohort_review_event` | Weekly cohort_reviewer LLM output | Yes |

### 4.2 New policy locks (Phase 13.7)

| # | Lock file | Class | Mode flag |
|---|---|---|---|
| P1 | `policy/source_registry.lock` | Audit-only | — |
| P2 | `policy/news_wire_policy.lock` | Audit-only | — |
| P3 | `policy/signal_half_life.lock` | Numerical | — |
| P4 | `policy/alpha_gate.lock` | Mode-flagged | dry_run / enforce |
| P5 | `policy/heuristic_kills.lock` | Mode-flagged | dry_run / enforce |
| P6 | `policy/new_risk_caps.lock` | Numerical | — |
| P7 | `policy/curator_config.lock` | Audit-only | — |
| P8 | `policy/monitor_layers.lock` | Audit-only | — |
| P9 | `policy/kill_response_matrix.lock` | Numerical | — |
| P10 | `policy/intraday_pnl_floor.lock` | Numerical | — |

Plus update `policy/HASHES` and `tools/recompute_hashes.py`.

### 4.3 New personas (Phase 13.7)

| # | Persona | Model | Purpose |
|---|---|---|---|
| R1 | `prompts/roles/cohort_reviewer.v1.md` | Opus | Weekly per-strategy trade cohort review |
| R2 | `prompts/roles/decision_adversary.v1.md` | Opus | Hot-path adversarial pre-trade check |

### 4.4 New ingest modules (Phases 13.4 + 13.5)

#### Polygon (Phase 13.4 — already authenticated)

| Module | Endpoints | Cadence |
|---|---|---|
| `ingest.polygon.filings` | Form 4, Form 3, 13F | 15-min poll |
| `ingest.polygon.short` | short-interest, short-volume | daily / bi-weekly |
| `ingest.polygon.options` | options snapshot, options trades WS | 5-min snapshot + WS |
| `ingest.polygon.news` | news REST, Benzinga WS | 1-min REST + WS |
| `ingest.polygon.ratings` | analyst ratings, consensus, bulls-bears-say, analyst-insights, analyst-details | 15-min poll |
| `ingest.polygon.earnings` | Benzinga earnings, guidance | 15-min poll |
| `ingest.polygon.corporate_events` | WSH corporate events | hourly |
| `ingest.polygon.fed` | treasury yields, inflation, labor (existing partial wiring) | daily |
| `ingest.polygon.financials` | income statements, balance sheets | quarterly |
| `ingest.polygon.etf` | ETF fund flows, ETF constituents | daily |
| `ingest.polygon.indices` | VIX/SPX/sector indices snapshot | 5-min poll + WS for VIX |
| `ingest.polygon.ipos` | IPOs (for lockup expiry detection) | daily |
| `ingest.polygon.consumer_eu` | EU consumer spending (Fable) | weekly |
| `ingest.polygon.reference` | tickers, GICS subindustry (for correlation cap) | daily |
| `ingest.polygon.market` | last trade, snapshot, daily bars (cross-checks) | on-demand |
| `ingest.polygon.ws_streams` | Trades, options trades, Benzinga news | WS persistent |

#### External free (Phase 13.5)

| Module | Source | Cadence |
|---|---|---|
| `ingest.external.sec_edgar` | 13D, Form 144, 8-K Item parser | 15-min poll |
| `ingest.external.gdelt` | GDELT 2.0 GKG via BigQuery | 15-min query |
| `ingest.external.alfred` | Vintage macro (for backtest integrity) | on-demand backtest |
| `ingest.external.capitol_trades` | Capitol Trades scrape | hourly |
| `ingest.external.reddit` | WSB / stocks / investing per-ticker | 5-min poll |
| `ingest.external.stocktwits` | StockTwits per-ticker stream | 5-min poll |
| `ingest.external.pr_wires` | BusinessWire, PRNewswire, GlobeNewswire, AccessWire RSS | 1-min poll |
| `ingest.external.news_aggregators` | Yahoo Finance, HN Algolia, Seeking Alpha RSS, Finnhub, Marketaux | 5-min poll |
| `ingest.external.cryptopanic` | CryptoPanic API | 5-min poll |
| `ingest.external.cryptoquant` | Exchange flows, funding rates (when added) | 15-min poll |

### 4.5 New curator daemons (Phase 13.4)

Each curator = a single daemon job that polls or subscribes to its source(s), parses, computes detection criteria, emits to `candidate_event`.

| # | Curator | Source(s) | Detection criterion |
|---|---|---|---|
| C1 | `INSIDER_CLUSTER` | Polygon Form 4 | ≥3 insiders / 30d, opportunistic only (filter routine 10b5-1) |
| C2 | `13D_ACTIVIST` | EDGAR 13D | New 13D filing on any ticker |
| C3 | `FORM_144_SALE` | EDGAR Form 144 | New 144 filing — leading indicator for upcoming Form 4 sells |
| C4 | `EARNINGS_DRIFT` | Polygon corporate-events + earnings | Earnings beat + raise guidance |
| C5 | `OPTIONS_FLOW` | Polygon options trades WS | Sweep + OI delta > 3σ |
| C6 | `SHORT_SQUEEZE` | Polygon short-interest + short-volume | Days-to-cover > 5 + recent volume spike |
| C7 | `NEWS_VELOCITY` | Polygon news + Benzinga + GDELT + PR wires | Mention z-score > 5σ in 1h |
| C8 | `CONGRESS_TRADE` | Capitol Trades | High-performer member trade |
| C9 | `8K_ITEM` | EDGAR 8-K Item parser | Items 1.01/2.01/5.02/8.01 |
| C10 | `CRYPTO_NEWS_VELOCITY` | CryptoPanic + Polygon crypto news + GDELT | Crypto-specific velocity |
| C11 (later) | `FDA_EVENT` | openFDA + Federal Register | Drug approvals, recalls — Phase 14 |
| C12 (later) | `CRYPTO_ONCHAIN` | CryptoQuant + CoinGlass | Exchange outflow + funding rate — Phase 14 |

### 4.6 New gate / decision modules (Phase 13.3)

| Module | Function |
|---|---|
| `research.feature_brief` | Parallel-fetch builder; computes all features deterministically |
| `research.scout` | LLM call wrapper, persona=strategy_scout, no tools, structured output |
| `research.adversary` | LLM call wrapper, persona=decision_adversary, no tools, structured output |
| `research.heuristic_kills` | 8 deterministic detectors (pump, laddering, lockup, lawyer-stock, halt, pre-bankruptcy, earnings front-run, halted/T-12) |
| `research.alpha_at_entry` | Half-life decay + price-move-since-signal check; outputs alpha_remaining_score + size_multiplier |
| `risk.liquidity_cap` | Max % ADV check |
| `risk.spread_depth_cap` | Bid-ask spread + top-of-book depth check |
| `risk.proximity_cap` | Earnings ±2d, ex-div ±1d, IPO lockup ±5d guards |
| `risk.correlation_cap` | GICS subindustry concentration limit |
| `risk.halt_router_v2` | Per-lane + per-action_type state from kill matrix |

### 4.7 New observability + postmortem (Phase 13.8)

| Module | Cadence |
|---|---|
| `obs.cohort_reviewer` | Weekly per-strategy (Opus) |
| `obs.intraday_monitor` | Every 5–15 min during RTH |
| `obs.ws_event_monitor` | Real-time — reacts to new candidate_event on held symbol or regime flip |
| `obs.eod_recon` | Daily after market close |
| `obs.order_management` | Slicing (TWAP/VWAP for size > 1% ADV); dynamic re-pricing |

### 4.8 Cockpit UI (Phase 13.6)

| Component | File / location |
|---|---|
| New tab "Flows" beside System Health | `design_handoff_cockpit/surface_flows.jsx` (new) + `app-v4.jsx` (registration) |
| Per-lane pipeline view (3 lanes) | within `surface_flows.jsx` |
| Per-source streaming health grid | within `surface_flows.jsx` |
| Recent Policy Changes panel | footer of `surface_flows.jsx` |
| Data builder | `src/trading_bot/operator_ui/cockpit_data.py` (extend existing `build_state()`) |
| HTTP route | `src/trading_bot/operator_ui/app.py` (no new routes; existing `/api/cockpit/state` covers it) |

### 4.9 New strategies (Phase 13.9)

Registered at `research_only` initially, promoted via `paper_fast_track` per existing flow.

| Strategy | Lane | Curator(s) driving it |
|---|---|---|
| `INSIDER_CLUSTER_v1` | Equity | C1 |
| `NEWS_VELOCITY_v1` | Equity | C7 |
| `EARNINGS_DRIFT_v1` | Equity | C4 |
| `OPTIONS_FLOW_v1` | Equity | C5 |
| `SHORT_SQUEEZE_v1` | Equity | C6 |
| `13D_ACTIVIST_v1` | Equity | C2 |
| `CONGRESS_TRADE_v1` | Equity | C8 |
| `CRYPTO_NEWS_VELOCITY_v1` | Crypto | C10 |
| (later) `FDA_EVENT_v1`, `CRYPTO_ONCHAIN_v1` | various | C11, C12 |

Each requires Tier-1 validation artifact before registration.

---

## 5. Sequenced build plan (10 sub-phases)

Each sub-phase is a focused build session. Each ends with: tests green, `boot_check.py` passing, `verify_ledger.py` passing, one logical commit.

### Phase 13.1 — Foundations (Session 1)

**Deliverables:**

1. New ledger tables L1–L7 (DDL in `ledger/schema.py`).
2. Writer modules for each (mirror existing `llm_call_event.py` pattern).
3. `policy/source_registry.lock` seed (P1) with ~30 sources catalogued.
4. `source_registry` Python module reading the lock.
5. `obs.source_health` heartbeat infrastructure.
6. `tools/init_ledger.py` updated to create new tables.
7. `tools/verify_ledger.py` updated to verify new hash chains.
8. Tests: 25–30 new tests covering table schemas, writer idempotency, hash chain continuity for each new table, source registry loading.

**Commit:** `feat(v4-phase13.1): event-driven curator foundations — 7 new tables + source registry`

**DoD:**
- 621 + ~30 = ~650 tests pass.
- `verify_ledger.py` proves all hash chains continuous.
- `boot_check.py` validates new HASHES entries.

### Phase 13.2 — Polygon core wiring (Session 2)

**Deliverables:**

1. `ingest.polygon.*` modules per 4.4 (organized into ~15 submodules; one per capability area).
2. Each module exposes `fetch_*(...)` functions returning typed dicts.
3. Centralized `polygon_client.py` with rate-limit-aware request layer, response caching, retry/backoff.
4. WS subscriber scaffold for 3 streams (Benzinga news, options trades, equity trades on a watchlist).
5. `research.feature_brief` builder skeleton — parallel-fetch via `asyncio.gather`, persists `feature_brief` row.
6. Tests: 35–40 new tests with mocked Polygon responses for each module.

**Commit:** `feat(v4-phase13.2): Polygon REST + WS wiring + feature_brief builder`

**DoD:**
- All Polygon endpoints fetchable in tests via mock.
- `feature_brief` builder produces deterministic output given fixed inputs (test with golden fixtures).
- WS scaffold connects + receives + parses in test mode.

### Phase 13.3 — Architecture gates (Session 3)

**Deliverables:**

1. `research.scout` and `research.adversary` LLM wrappers (no tools, structured Pydantic output).
2. `research.heuristic_kills` — 8 detectors per 4.6.
3. `research.alpha_at_entry` with seed half-life table from `policy/signal_half_life.lock`.
4. 4 new risk caps per 4.6: `risk.liquidity_cap`, `risk.spread_depth_cap`, `risk.proximity_cap`, `risk.correlation_cap`.
5. `risk.halt_router_v2` with per-lane + per-action_type state from kill matrix.
6. `risk.precheck.evaluate` extended to call new caps + halt_router_v2.
7. Kill switch escalation timer (background daemon job).
8. Tests: 35–40 new tests covering each gate's pass/fail logic against fixtures.

**Commit:** `feat(v4-phase13.3): gate stack — scout/adversary/alpha/heuristic kills + 4 new risk caps + halt_router v2`

**DoD:**
- Existing precheck behavior preserved for trades that don't trigger new gates (regression-tested).
- Each new cap has both pass and fail test fixtures.
- Heuristic kills run in `dry_run` mode by default (per mode flag).
- Alpha gate runs in `enforce` mode by default (clean math, low FP risk).

### Phase 13.4 — Curators (Session 4)

**Deliverables:**

1. `curator.base.CuratorJob` abstract class — handles heartbeat, error tracking, candidate_event writing.
2. 8 curator implementations (C1, C4–C8, C2, C9 — the Polygon-backed ones; external-source curators are Phase 13.5).
3. WS-based curators for C5 (options flow) and C7 (news velocity) using Polygon streams.
4. `curator.intake.IntakeRouter` — priority queue, dedup (symbol + source_family + 1h window), confluence detection.
5. Daemon scheduler entries for all polling curators.
6. Hooks: curators write to `candidate_event` + `source_health` heartbeat per fetch.
7. Tests: 35–40 new tests — each curator's parse + detect logic with realistic fixtures + intake router behavior under contention.

**Commit:** `feat(v4-phase13.4): 8 curator daemons + intake router + priority queue`

**DoD:**
- Curators run in idle-no-data mode without error (e.g., a curator running on Saturday when no Form 4s file).
- Intake router correctly dedups duplicate emissions.
- Confluence flag set when ≥2 curators fire on same symbol within 1h.

### Phase 13.5 — External free sources (Session 5)

**Deliverables:**

1. `ingest.external.sec_edgar` — 13D / Form 144 / 8-K Item parser (Items 1.01 / 2.01 / 5.02 / 8.01).
2. `ingest.external.gdelt` — BigQuery client, GKG query, tone + volume + theme features.
3. `ingest.external.alfred` — vintage macro fetcher (Python via `fredapi` configured for ALFRED endpoint).
4. `ingest.external.capitol_trades` — scraper for `capitoltrades.com/trades`.
5. `ingest.external.reddit` (PRAW) + `ingest.external.stocktwits` per-ticker.
6. `ingest.external.pr_wires` — RSS pollers for BusinessWire, PRNewswire, GlobeNewswire, AccessWire.
7. `ingest.external.news_aggregators` — Yahoo Finance, HN Algolia, Seeking Alpha RSS, Finnhub free, Marketaux free, CryptoPanic.
8. Extend curators C2, C3, C7, C8, C9, C10 to consume the external sources where applicable.
9. Tests: 35–40 new tests with mocked external responses.

**Commit:** `feat(v4-phase13.5): external free sources — EDGAR / GDELT / Capitol Trades / PR wires / news aggregators / social`

**DoD:**
- Each external source has rate-limit-aware fetcher with backoff.
- Source health monitor tracking heartbeats per external source.
- Curators C2/C3/C7/C8/C9/C10 cleanly consume both Polygon and external feeds (weighted aggregation per source_registry trust_scores).

### Phase 13.6 — Cockpit Flows tab (Session 6)

**Deliverables:**

1. `design_handoff_cockpit/surface_flows.jsx` — new tab component.
2. Three lane swim-lanes (Equity / Crypto / Options-Wheel) showing per-strategy pipeline state: events ingested → feature_briefs built → scout calls → adversary calls → alpha gate pass/reject → precheck pass/reject → fills → monitor exits.
3. Per-source streaming health grid — chip per source with green/yellow/red per heartbeat policy.
4. Recent Policy Changes panel — last 10 from `policy_change_event` with tightening/loosening icons.
5. `cockpit_data.py` `build_flows_state()` function returning the data the tab needs.
6. Tab registration in `app-v4.jsx`.
7. Tests: 15–20 new tests covering `build_flows_state()` against fixture ledgers.

**Commit:** `feat(v4-phase13.6): cockpit Flows tab — 3 lanes + source health grid + policy changes panel`

**DoD:**
- Tab renders with mock data when ledger is empty.
- Tab renders with live data once any curator has emitted a candidate_event.
- Source health chips update on every fetch via cockpit refresh cycle.

### Phase 13.7 — Personas + locks + boot_check (Session 7)

**Deliverables:**

1. All 10 policy locks per 4.2 with real content (seed values per decisions in §2).
2. 2 new personas per 4.3.
3. `policy/HASHES` regenerated via `tools/recompute_hashes.py`.
4. `kernel/boot.py` extended to verify new lock + persona hashes.
5. `risk/policy_loader.py` loads new locks at boot.
6. **CLAUDE.md updated** — drop the 7-day cooldown rule; document the new 3-bucket policy model + mode flags.
7. Memory entries updated under `~/.claude/projects/-Users-bharathkandala-Trading/memory/`:
   - Update `feedback_trading_bot_v4_conventions.md` to reflect new policy model.
   - Add `project_trading_bot_v4_phase_13.md` for current phase reference.
8. Tests: 10–15 new tests for lock loading + hash verification + boot_check.

**Commit:** `feat(v4-phase13.7): 10 new policy locks + 2 personas + CLAUDE.md policy-model update`

**DoD:**
- `python tools/recompute_hashes.py --check` passes.
- `boot_check.py` succeeds.
- All locks loadable + parseable in tests.

### Phase 13.8 — Monitor layers + postmortems + EOD recon (Session 8)

**Deliverables:**

1. `obs.intraday_monitor` daemon job — 5-min RTH cadence.
2. `obs.ws_event_monitor` — subscribes to candidate_event queue, regime_event flips; re-evaluates held positions.
3. `obs.cohort_reviewer` — weekly cron, per-strategy cohort analysis via Opus, writes `cohort_review_event`.
4. `obs.eod_recon` — daily cron after market close, writes `reconciliation_proof` with `recon_window='eod'`.
5. Existing monthly deep recon job updates to include corporate-actions cross-check.
6. `obs.order_management` — order slicing (TWAP/VWAP) for size > 1% ADV, dynamic re-pricing for unfilled limits.
7. Tests: 25–35 new tests.

**Commit:** `feat(v4-phase13.8): 3-layer monitor + cohort reviewer + EOD recon + order management`

**DoD:**
- Intraday monitor runs every 5 min without erroring during RTH.
- WS event monitor reacts to fixture-injected candidate_events for held symbols.
- Cohort reviewer produces structured weekly output (tested with mocked Opus).
- EOD recon proof committed daily.

### Phase 13.9 — Event-driven strategies registered (Session 9)

**Deliverables:**

1. `strategies.insider_cluster_v1` — strategy module consuming `candidate_event` rows from C1.
2. Similarly for: news_velocity_v1, earnings_drift_v1, options_flow_v1, short_squeeze_v1, activist_13d_v1, congress_trade_v1, crypto_news_velocity_v1.
3. Each strategy:
   - Reads candidate_events targeted at its curator family.
   - Calls feature_brief builder + scout + adversary + alpha gate.
   - Submits decision_packet to risk precheck → order router.
4. Tier-1 validation artifact for each (backtest harness produces, registry consumes).
5. Auto-registration via `paper_fast_track` after Tier-1 passes.
6. Existing v3 strategies left untouched (continue running their pull-based logic in parallel).
7. Tests: 25–35 new tests — each strategy's end-to-end happy path with mocked LLM + fixture candidate_events.

**Commit:** `feat(v4-phase13.9): 8 event-driven strategies registered at research_only`

**DoD:**
- Each strategy has Tier-1 validation artifact in registry.
- Each strategy registers + appears in cockpit Flows tab.
- One end-to-end test per strategy: fixture candidate_event → feature_brief → scout (mock) → adversary (mock) → alpha gate (real) → precheck (real, fixture-permissive) → order_router (mock broker) → fill event written.

### Phase 13.10 — Dynamic P&L floor + integration burn-in (Session 10)

**Deliverables:**

1. `policy/intraday_pnl_floor.lock` with regime-scaled thresholds per §2.8.
2. `risk.intraday_pnl_floor` reads current regime + applies appropriate threshold.
3. Full daemon scheduler wiring for ALL new jobs (curators, monitor layers, cohort_reviewer, eod_recon).
4. 24h paper-mode burn-in observation: bot runs in paper mode for 24h, every curator emits, every gate runs, no errors.
5. Documentation pass:
   - Update `CLAUDE.md` Phase 13 entry (similar to existing Phase 12+ABCD entry).
   - Update session memory entries.
6. Total test target: ~800 tests passing (from 621 baseline).

**Commit:** `feat(v4-phase13.10): dynamic intraday floor + daemon integration + 24h burn-in + Phase 13 docs`

**DoD:**
- Bot runs 24h paper-mode without unhandled exception.
- All daemon jobs ticking on schedule.
- Cockpit Flows tab populated with real data.
- `verify_ledger.py` and `boot_check.py` pass continuously.

---

## 6. Testing strategy

### 6.1 Per-module unit tests

Each new module gets:
- Happy-path test (intended use produces expected output).
- Error-path test (input violations raise expected exceptions).
- Edge-case test (boundary conditions, empty inputs, max sizes).

### 6.2 Integration tests

- `tests/integration/test_phase13_end_to_end.py` — full pipeline from candidate_event injection through fill, using fixtures and mocked broker/LLM.
- `tests/integration/test_phase13_kill_switch_response.py` — fire each of the 9 kill switches and verify the correct response level.
- `tests/integration/test_phase13_alpha_gate_decay.py` — half-life decay correctness across signal types.

### 6.3 Regression suite

- All 621 existing tests must continue passing throughout.
- `verify_ledger.py` + `boot_check.py` run at the end of every commit.

### 6.4 Burn-in test (Phase 13.10)

- 24h continuous paper-mode run with no operator intervention.
- Telemetry collected: errors / sec, candidate_events / hr, feature_briefs / hr, gate rejection rate, source health distribution.
- Pass criterion: zero unhandled exceptions; source health green for ≥80% of sources for ≥80% of duration.

---

## 7. Risk + rollback

### 7.1 Per-phase rollback

Each phase ends with a single commit. Rollback = `git revert <sha>`. No phase mutates an existing kernel/risk/execution file destructively; all changes are additive (new modules, new tables, new caps appended to precheck).

### 7.2 Hot-path safety

- All new gates default to `dry_run` mode where appropriate (heuristic kills especially).
- Alpha gate ships in `enforce` mode but with conservative threshold (0.40) — easy to relax later if blocking valid trades.
- New risk caps are tightening — they can only reject more trades, never approve new ones.

### 7.3 Existing functionality preserved

- v3 strategies untouched.
- Existing precheck + order_router behavior preserved for trades that don't trigger new gates (regression-tested in 13.3).
- `feature_flags.is_llm_hotpath_enabled()` still gates LLM in hot path; new architecture respects it.
- `TRADING_BOT_ALLOW_LIVE_PARAM_WRITES` still required for parameter mutation; new locks respect it.

### 7.4 Data accumulation safety

- New event-driven strategies register at `research_only` only.
- Tier-1 validation required for `research_only` registration.
- No new strategy reaches `tiny_live` in Phase 13 — that requires 10 paper trades + Tier-3 validation per §2.4.

### 7.5 LLM budget exhaustion

- Budget raised to 500/day per §2.5.
- Existing P0/P1/P2/P3 priority queue handles over-budget by dropping P3.
- New jobs use P2 or P3 (curators, postmortems) leaving P0/P1 for adversarial pair + emergency operator calls.

---

## 8. Definition of done (Phase 13 as a whole)

| # | Criterion |
|---|---|
| 1 | All 10 sub-phases committed; tree green after each |
| 2 | ~800 tests passing (621 baseline + ~180 new) |
| 3 | `verify_ledger.py` passes; all new hash chains continuous |
| 4 | `boot_check.py` passes; HASHES verified |
| 5 | 9 event-driven strategies registered at `research_only` with Tier-1 artifacts |
| 6 | 24h paper-mode burn-in: zero unhandled exceptions |
| 7 | Cockpit "Flows" tab live with all 3 lanes + per-source health + policy changes panel |
| 8 | CLAUDE.md updated to reflect new policy model + Phase 13 state |
| 9 | Session memory updated under `~/.claude/projects/.../memory/` |
| 10 | At least one event-driven strategy has produced ≥1 paper trade end-to-end (insider_cluster or news_velocity most likely) |

---

## 9. Appendix A — Source registry seed (Phase 13.1 input)

Catalogue for `policy/source_registry.lock`. Schema:

```json
{
  "source_id": "polygon.form4",
  "category": "filings",
  "endpoint_url": "/stocks/filings/vX/form-4",
  "auth": "polygon_api_key",
  "cadence_seconds": 900,
  "expected_latency_ms": 500,
  "trust_score": 0.95,
  "criticality": "high",
  "rate_limit": {"requests_per_second": 100},
  "failure_mode": "exponential_backoff",
  "owner_module": "ingest.polygon.filings"
}
```

Full catalogue (30 sources) lives in the lock file; created in Phase 13.1.

---

## 10. Appendix B — Half-life table seed (Phase 13.7 input)

Initial values for `policy/signal_half_life.lock`:

```json
{
  "version": "2026-05-15.v1",
  "_source": "Cohen-Malloy-Pomorski (insider), Bernard-Thomas (PEAD), Pan-Poteshman (options), Brav-Jiang (13D), Engle-Russell (news), practitioner data (squeeze, congress, FDA, 8-K)",
  "half_lives": {
    "news_velocity":       {"half_life_hours": 2,    "total_lifetime_hours": 24},
    "options_flow":        {"half_life_hours": 24,   "total_lifetime_hours": 120},
    "insider_cluster":     {"half_life_hours": 120,  "total_lifetime_hours": 720},
    "earnings_drift":      {"half_life_hours": 720,  "total_lifetime_hours": 2160},
    "activist_13d":        {"half_life_hours": 720,  "total_lifetime_hours": 8760},
    "form_13f":            {"half_life_hours": 504,  "total_lifetime_hours": 2160},
    "congress_trade":      {"half_life_hours": 336,  "total_lifetime_hours": 1440},
    "fda_event":           {"half_life_hours": 96,   "total_lifetime_hours": 336},
    "8k_item":             {"half_life_hours": 120,  "total_lifetime_hours": 720},
    "short_squeeze":       {"half_life_hours": 168,  "total_lifetime_hours": 504},
    "crypto_news_velocity":{"half_life_hours": 4,    "total_lifetime_hours": 48},
    "crypto_onchain":      {"half_life_hours": 48,   "total_lifetime_hours": 168}
  },
  "alpha_gate_threshold": 0.40,
  "size_multiplier_curve": "linear_above_0.70_else_decay"
}
```

These will tune via cohort_reviewer's `observed_half_life` measurements over time, per the operator-floor + bot-observed two-tier scheme (operator owns loosening).

---

## 11. Appendix C — Curator → strategy mapping

For quick reference during execution:

| Curator | Strategy | Primary signal | Half-life class |
|---|---|---|---|
| C1 INSIDER_CLUSTER | INSIDER_CLUSTER_v1 | ≥3 insider buys / 30d | insider_cluster |
| C2 13D_ACTIVIST | 13D_ACTIVIST_v1 | New 13D filing | activist_13d |
| C3 FORM_144_SALE | (no strategy — feeds heuristic kill: insider laddering) | 144 filing | (heuristic only) |
| C4 EARNINGS_DRIFT | EARNINGS_DRIFT_v1 | Beat + raise | earnings_drift |
| C5 OPTIONS_FLOW | OPTIONS_FLOW_v1 | Sweep + OI Δ > 3σ | options_flow |
| C6 SHORT_SQUEEZE | SHORT_SQUEEZE_v1 | DTC > 5 + vol spike | short_squeeze |
| C7 NEWS_VELOCITY | NEWS_VELOCITY_v1 | Mention z > 5σ / 1h | news_velocity |
| C8 CONGRESS_TRADE | CONGRESS_TRADE_v1 | High-performer trade | congress_trade |
| C9 8K_ITEM | (feeds feature_brief; no dedicated strategy yet) | 8-K Items 1.01/2.01/5.02/8.01 | 8k_item |
| C10 CRYPTO_NEWS_VELOCITY | CRYPTO_NEWS_VELOCITY_v1 | Crypto news z > 5σ | crypto_news_velocity |

---

## 12. Open items for future phases (NOT in Phase 13)

These were discussed but deferred:

- **Quiver Quantitative API ($25/mo)** for Congress + lobbying + patents + contracts. Can replace `ingest.external.capitol_trades` if budget allows. → Phase 14.
- **Phase 14 curators:** FDA / Federal Register / EIA weekly petroleum / CryptoQuant / CoinGlass / DefiLlama / Etherscan whale. → Phase 14.
- **Estimize crowdsourced estimates** via SSRN historical dataset. → Phase 14 research.
- **MVP-OP promotion path** — requires 60-day clean recon under new architecture. Earliest ~July 2026 calendar.
- **Live capital flip (`TRADING_BOT_ALLOW_LIVE_PARAM_WRITES=1`)** — requires Tier-3 validation across all paper strategies + 60-day MVP-OP. Earliest Q3 2026.

---

## 13. Session 1 (Phase 13.1) — concrete starting checklist

For the new build session, here's the first session's todo list:

1. Open this plan file as reference.
2. Read existing ledger pattern in `src/trading_bot/ledger/llm_call_event.py` (~80 lines, the canonical writer module).
3. Read `src/trading_bot/ledger/schema.py` to see DDL patterns.
4. Add DDL for L1–L7 to `schema.py`.
5. Bump `SCHEMA_VERSION` from 1 to 2 (new tables = new version).
6. Create 7 new writer modules in `src/trading_bot/ledger/`:
   - `candidate_event.py`
   - `feature_brief.py`
   - `source_health.py` (mutable; non-hash-chained)
   - `source_event_log.py` (mutable; rotates)
   - `policy_change_event.py`
   - `kill_switch_escalation.py`
   - `cohort_review_event.py`
7. Update `ledger/__init__.py` exports.
8. Update `tools/init_ledger.py` migration logic for v1 → v2.
9. Update `tools/verify_ledger.py` to validate new chains.
10. Create `policy/source_registry.lock` seed with 30 sources per appendix A.
11. Create `src/trading_bot/ingest/source_registry.py` Python loader.
12. Create `src/trading_bot/obs/source_health.py` heartbeat module.
13. Create test files:
    - `tests/ledger/test_candidate_event.py`
    - `tests/ledger/test_feature_brief.py`
    - `tests/ledger/test_source_health.py`
    - `tests/ledger/test_policy_change_event.py`
    - `tests/ledger/test_kill_switch_escalation.py`
    - `tests/ledger/test_cohort_review_event.py`
    - `tests/ingest/test_source_registry.py`
    - `tests/obs/test_source_health.py`
14. Run full test suite: 621 + ~30 = ~650 passing.
15. Recompute `policy/HASHES` (includes new source_registry.lock).
16. Run `boot_check.py`, `verify_ledger.py`.
17. Single commit: `feat(v4-phase13.1): event-driven curator foundations — 7 new ledger tables + source registry seed`.

That's session 1. Each subsequent session has its own concrete checklist that the plan above describes at "Deliverables" level — flesh it out at session start by reading the corresponding `## Phase 13.N` section.

---

**End of plan.** Sized for 10 focused build sessions. Ready for execution.
