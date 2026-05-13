# `kernel/` — L5 Deterministic Trading Kernel

**Status:** Empty skeleton — populated in Phase 1+ (see
`docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-0-design.md` for
the phase roadmap).

## Mandate (Plan v4 §3)

The kernel is the single producer of order intents. It is:

- **Deterministic.** Same `(features, registry_row)` always produces the same
  `intent`. No LLM calls. No dynamic code generation. No unversioned config.
- **Pure.** It reads the strategy registry, reads the point-in-time feature
  store, emits an intent for the risk kernel to gate. It never writes to the
  ledger directly — that is L7's job.
- **Single-process.** Only one kernel daemon may write to the ledger
  database; every other tool opens it read-only. (See `ledger/README.md`.)

## What goes here

- `strategy_runner.py` — the per-tick driver. Inputs: active strategy rows in
  `registry/`, a feature snapshot from `features/`. Output: `intent_json`
  passed to `risk/precheck.py`.

## What does NOT go here

- LLM calls (forbidden by `policy/role_personas.lock` scope).
- Dynamic strategy authoring (mutations live in `research/`).
- Order submission (lives in `execution/`).
- Risk gating (lives in `risk/`).
