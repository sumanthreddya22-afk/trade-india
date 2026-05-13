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
| Live param mutation | **Blocked** unless `TRADING_BOT_ALLOW_LIVE_PARAM_WRITES=1` (default off). Auto-tune still computes observations into shadow rows. |
| Crypto exposure cap | 15 % of equity. Phase 0 unwind: see `tools/phase0_crypto_unwind.py`. |
| Position classification | `bot \| external \| manual \| unknown` on every position. Runtime halt-on-unknown lands in Phase 2. |
| Policy locks | 9 skeleton `.lock` files in `policy/`, anchored by `policy/HASHES`. Real content lands in the owning phase. |
| Persona files | 8 personas under `prompts/roles/`. Runtime hash check lands in Phase 2. |
| Ledger schema | Deferred to Phase 1. |

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
