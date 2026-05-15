# Trading Bot — Project Instructions

## North star

> Build an **autonomous systematic trading laboratory**, not a chatbot that
> trades. AI may generate hypotheses, mutate strategies, run research, write
> postmortems, and recommend promotions. A **deterministic risk, ledger,
> validation, and execution kernel** decides whether anything may trade.

The full plan lives at:

- PDF: `Best Prompt Design for a Production-Ready Autonomous AI Trading Agent.pdf` (Plan v4, 2026-05-13)
- Phase 0 design: `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-0-design.md`
- Phase 0 plan: `docs/superpowers/plans/2026-05-13-trading-bot-v4-phase-0.md`
- Seed thesis: `docs/edge_thesis_v1.md`

## Current state — v4 Phase 0 (post 2026-05-13)

| Item | State |
|---|---|
| LLM in trading hot path | **Quarantined** behind `TRADING_BOT_ENABLE_LLM_HOTPATH` (default off). |
| Live param mutation | **Blocked** unless `TRADING_BOT_ALLOW_LIVE_PARAM_WRITES=1` (default off). |
| Crypto exposure cap | 15 % of equity. **Executed 2026-05-13** — 12 sells, $5,114 proceeds, 58.19% → 15.02%. |
| Position classification | `bot \| external \| manual \| unknown` on every position. Runtime halt-on-unknown lands in Phase 2. |
| Policy locks | **Phase 2 populated** — 7 locks carry real numerics (validation, risk, pdt, lane_caps, cost_model, data_freshness, role_personas); 2 remain skeletons (source_reliability → Phase 1.5; short_policy → SHORT phase). All anchored by `policy/HASHES`; loader hash-verifies at boot. |
| Persona files | 8 personas under `prompts/roles/`. Runtime hash check enforced by `kernel/boot.py`. |
| Ledger schema | **Shipped Phase 1** — 6 hash-chained append-only tables + `order_current` view; UUIDv7 order_uid; idempotent client_order_id; orphan recovery; reconciliation; off-host mirror; single-writer lock. Init via `tools/init_ledger.py`; verify via `tools/verify_ledger.py`. |
| Risk kernel | **Shipped Phase 2** — `risk.precheck.evaluate` is the single-entry gate. 7 cap categories (account / asset-class / lane / strategy / symbol / order / pdt) + 8 kill switches + halt router. Boot integrity check via `tools/boot_check.py`. |
| Execution + ingest | **Shipped Phase 3** — `execution.order_router.submit_order` (precheck → freshness → idempotent → broker, callback-injected); `execution.cost_model` (raw / broker_paper / pessimistic lenses); `execution.drift_monitor` (20-trade rolling); `ingest.watermarks` (lane freshness); `ingest.corporate_actions` (record + cross-check, hash-chained). Live Alpaca call inside the broker callback lands Phase 5. |
| Strategy registry | **Shipped Phase 4** — `strategy_version` + `validation_artifact` (3 tiers from Plan §13) + `promotion_packet` (Tier-3 human sign-off); `registry.promotion.gate` enforces tiered validation; hash-locked `research/search_space_v1.json` for the mutation engine (Phase 6 consumer); seed strategy `ETF_MOMENTUM_v1` registered at `research_only`. |
| Research factory | **Shipped Phase 5** — `research.deflated_sharpe` (Bailey/LdP) + `research.probability_of_overfit` + `research.build_folds` (walk-forward + 30% locked holdout) + `research.is_monotone_degradation` + `research.plateau_coverage` + `research.failure_memory` (90-day reject cache, hash-chained); `research.run_intake` runs adversarial pair and persists both transcripts to `strategy_decision`; `research.run_cycle` end-to-end driver emits Tier-1 `validation_artifact` via the registry. |
| Mutation engine | **Shipped Phase 6** — `research.propose_candidates` enumerates variants from the hash-locked `search_space_v1.json` (rejects unknown mutation_ids; budget-capped at 64/family/month); `mutation_log` + `mutation_outcome` tables (hash-chained, append-only); `bh_fdr.apply` writes adjusted p-values + survived flags via new event rows; `SubprocessPersonaRunner` with hash check (operator-configured command, e.g. `claude --json`); `sandbox.activated()` blocks `execution/kernel/risk.precheck` imports during mutation runs; `run_mutation_cycle` propose → backtest → BH-FDR end-to-end. |
| Autonomy expansion (2026-05-15) | **Shipped Phase 12 + A + B + C/D scaffold** — single LLM transport (`shared.llm_transport.invoke` wraps `claude --json` with Sonnet default / Opus for codegen+adversarial / priority queue / 180 calls-per-day budget / content-hash cache). 11 new hash-chained ledger tables: `llm_call_event`, `universe_audit_event`, `drift_postmortem_event`, `regime_event`, `intel_feature_snapshot`, `paper_validation_event`, `mutation_review_event`, `search_space_proposal_event`, `source_scout_event`, `strategy_candidate`, `strategy_blueprint`, `strategy_codegen_event`. **Phase A** — 9 new policy locks (paper_fast_track / etf_universe / crypto_universe / dual_momentum_sleeves / wheel_universe / regime_protocols / strategy_signal_features / intel_feeds / research_bot_sources / strategy_taxonomy), 3 v3 strategies (DUAL / ETF / CRYPTO momentum v3 — daily cadence + `research.universe_discovery` filter rule replacing hardcoded allowlists), 7 new hash-anchored personas (drift_postmortem / universe_audit_analyst / regime_analyst / mutation_reviewer / search_space_expander / strategy_scout / strategy_implementer), `risk.regime_classifier` + `risk.regime_protocols` + `risk.manual_regime_override` wired into `risk.precheck` (regime size_multiplier + new_entries cap). **Phase B** — multi-underlying SPY Wheel v3 (`spy_wheel_v3.state_machine` parametrised by underlying, capital equal-weight across `top_n_by_option_volume`). **Phase C** — `research.paper_validation` (precheck→submit→fill→slippage compare) + `registry.auto_register` (auto-promotes v_n+1 to `tiny_paper` when paper_fast_track active; **voids** on `live_capital_enabled=true`) + `research.mutation_runner` (nightly cycle / weekly review / monthly expansion). **Phase D** — `research.research_bot` scaffold (source scouts → candidate → blueprint → codegen) + 17 cache-backed intel feeds across stocks / crypto / options / macro. 8 new daemon jobs (`universe_audit`, `regime_monitor`, `mutation_review`, `search_space_proposal`, `source_scout`, `strategy_intake`) wired to scheduler. **Fast-track cooldown waived per operator 2026-05-15** (lock structure retained for audit). 598 tests pass (was 542 → +56). |

Autonomy level (Plan §16): **L2 — autonomous research, no autonomous live
trading**. The bot may run sandbox experiments and validation packets; it may
NOT promote to live capital.

## Directory taxonomy

| Directory | Kind | LLM allowed? | Auto-mutation allowed? |
|---|---|---|---|
| `kernel/` (Phase 1+) | Deterministic trading kernel | **No** | **No** |
| `risk/` (Phase 2+) | Risk kernel | **No** | **No** |
| `execution/` (Phase 1+) | Execution router + ledger | **No** | **No** |
| `ledger/` (Phase 1+) | Append-only event store | **No** | **No** |
| `registry/` (Phase 4+) | Strategy registry | **No** | **No** |
| `policy/` | Hash-locked policy | **No** (operator-edited, signed) | **No** |
| `research/` (Phase 5+) | Research factory | **Yes** (sandbox only, no broker creds) | **Yes** |
| `prompts/roles/` | Hash-locked personas | n/a (data) | **No** |
| `obs/` (Phase 1+) | Observability + L8 postmortem | **Yes** (postmortem only) | **No** |
| `src/trading_bot/` | Legacy pre-v4 codebase | Quarantined per feature flag | Blocked per env flag |
| `docs/` | Versioned governance documents | n/a | **No** (new version = new file) |

## Hard rules

1. **No LLM in the trading hot path.** Every entry / hold / scout / unblock /
   risk debate now early-exits unless `TRADING_BOT_ENABLE_LLM_HOTPATH` is
   explicitly truthy. Do not bypass `feature_flags.is_llm_hotpath_enabled()`.
2. **No silent live-param writes.** `threshold_overrides.write_override` and
   `evolution.save_params` are blocked unless `TRADING_BOT_ALLOW_LIVE_PARAM_WRITES=1`.
3. **No new entries while crypto > 15 % of equity.** Phase 2 risk kernel will
   enforce; Phase 0 only documents it. The Phase 0 unwind tool is the
   one-time correction.
4. **No new top-level kernel directory without a phase plan.** v4 specifies
   them in Plan §3; do not invent your own.
5. **Append-only.** No `UPDATE` / `DELETE` on `order_master`,
   `order_state_event`, `fill_event`, `position_snapshot`, `strategy_decision`,
   `reconciliation_proof` (Phase 1+). Schemas enforce.
6. **One thesis at a time** in the production strategy registry. Research
   factory may sandbox-test many in parallel (Plan §2 multi-hypothesis policy).
7. **Cost lens.** All backtests report raw / broker-paper / pessimistic.
   Only the pessimistic lens gates promotion.

## Conventions

- **Validation cooldowns.** Loosening any threshold requires a new dated
   `.lock` file, signed, AND a 7-day wait before the system honours it.
   Tightening takes effect on the next kernel cycle (Plan §4 asymmetric
   cooldown).
- **Phase numbering is not a gate.** A new lane / sleeve / feature is gated
   by its own validation lock, not by which phase number we are on.
- **Wall-clock gates can't be compressed.** MVP-OP needs 60 calendar days of
   reconciliation matches. ALPHA needs ~365 days of paper observation for
   the monthly-cadence seed thesis. The agent cannot finish these by writing
   code.

## When in doubt

- Read the plan PDF.
- Read the Phase 0 design doc.
- Verify with `python tools/recompute_hashes.py --check` that the policy
   files are intact before making structural changes.
- Add a new persona / lock / persona-version as a *new file*, not a mutation
   of an existing one.
