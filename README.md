# Trading Bot — v4

> **Build an autonomous systematic trading laboratory, not a chatbot that
> trades.** AI may generate hypotheses, mutate strategies, run research,
> write postmortems, and recommend promotions. A deterministic risk, ledger,
> validation, and execution kernel decides whether anything may trade.

This repository is implementing **Plan v4** (Solo-retail blueprint: edge-first,
ledger-first, validation-locked, intel-aware). The plan PDF lives at
`docs/trading_bot_implementation_plan_v4_20260513.pdf`.

## Status

| Phase | Plan duration | This repo |
|---|---|---|
| 0 — Stabilize | 1 wk | **Shipped 2026-05-13** — cleanup, edge thesis, 8 personas, locks skeleton, feature flags, classifier, tests. Crypto exposure unwound to 15%. |
| 1 — Ledger schema | 2 wk | **Shipped 2026-05-13** — 6 hash-chained append-only tables + `order_current` view; UUIDv7 order_uid; idempotent client_order_id; orphan recovery; reconciliation; off-host mirror; single-writer lock. |
| 2 — Risk kernel + locks populated | 2 wk | **Shipped 2026-05-13** — 9 locks populated (7 real + 2 skeleton); 7 cap checks + 8 kill switches + halt router + precheck orchestrator; `kernel/boot.py` startup integrity gate. |
| 3 — Cost model + Alpaca hardening | 1 wk | **Shipped 2026-05-13** — 3-lens cost model (raw / broker_paper / pessimistic); order_router (precheck → freshness → idempotent → broker); drift_monitor (20-trade rolling); ingest layer with data_watermark + corporate_action (hash-chained). |
| 4 — Strategy registry | 1 wk | **Shipped 2026-05-13** — `strategy_version` + `validation_artifact` (3 tiers from Plan §13) + `promotion_packet` (Tier-3 human sign-off); hash-locked `research/search_space_v1.json`; ETF_MOMENTUM_v1 seed registered. |
| 5 — Research factory | 3 wk | **Shipped 2026-05-13** — DSR + PBO + walk-forward (5+ folds + 30% locked holdout) + ablation + parameter plateau; failure memory (90-day reject cache, hash-chained); adversarial-pair hypothesis intake (mock persona shim, Phase 6 wires real LLM); `run_cycle` driver emits Tier-1 `validation_artifact`. 293 tests pass. |
| MVP-OP | 60 calendar days | Wall-clock gate. |
| 6 — Mutation engine | 2 wk | — |
| ALPHA | ~365 calendar days | Wall-clock gate. |
| 7 — Second lane | 3 wk | — |
| 8 — Wheel lane | 4 wk | — |
| 9 — Live readiness | 2 wk | — |

**Trading is halted** until Phase 9 ships and the operator signs the live
readiness packet. Two safety flags currently make this explicit:

- `TRADING_BOT_ENABLE_LLM_HOTPATH` (default off) — any future hot-path LLM
  call must consult `feature_flags.is_llm_hotpath_enabled()`.
- `TRADING_BOT_ALLOW_LIVE_PARAM_WRITES` (default off) — any future module
  that wants to mutate live parameters must consult
  `feature_flags.live_param_writes_allowed()`.

## Layout

```
docs/
  trading_bot_implementation_plan_v4_20260513.pdf   ← the plan
  edge_thesis_v1.md                                  ← the single seed thesis
  superpowers/specs/2026-05-13-trading-bot-v4-phase-0-design.md
  superpowers/plans/2026-05-13-trading-bot-v4-phase-0.md

policy/                  ← 9 hash-locked .lock files + HASHES manifest
prompts/roles/           ← 8 hash-locked persona files (.v1.md)

src/trading_bot/
  feature_flags.py       ← env-var gates
  position_classifier.py ← bot|external|manual|unknown
  shared/                ← Alpaca client, settings, submit_txn primitives
  kernel/    (L5)        ← skeleton — Phase 1+
  ledger/    (L7)        ← skeleton — Phase 1
  execution/ (L7)        ← skeleton — Phase 1, 3
  risk/      (L6)        ← skeleton — Phase 2
  registry/  (L4)        ← skeleton — Phase 4
  ingest/    (L1, 1.5)   ← skeleton — Phase 1, 1.5
  features/  (L2)        ← skeleton — Phase 2
  research/  (L3)        ← migrated backtest utilities + skeleton
  obs/       (L8)        ← migrated dashboard (rewiring deferred)

tools/
  recompute_hashes.py            ← regenerate policy/HASHES
  phase0_crypto_unwind.py        ← Phase 0 unwind (dry-run by default)

tests/
  test_phase0_*.py               ← 46 tests, all green

archive/
  pre_v4_20260513.tgz            ← 46 MB tarball of the pre-cleanup state
```

## Hard rules

See `CLAUDE.md` for the full directory taxonomy and the v4 hard rules.

## Running tests

```bash
venv/bin/python -m pytest tests/
```
