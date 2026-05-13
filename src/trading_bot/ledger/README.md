# `ledger/` — L7 Append-Only Event Store

**Status:** Empty skeleton — schema lands in **Phase 1** (next session).

## Mandate (Plan v4 §5)

Every numeric in every report — PnL, exposure, attribution, drawdown — derives
from these tables and nothing else. The schema is **event-sourced**,
**hash-chained**, **append-only**.

## Tables (lands Phase 1)

- `order_master` — immutable identity record per order (one row at intent time).
- `order_state_event` — append-only state transitions. Hash chained
  (`this_hash = sha256(prev_hash || canonical(row))`).
- `fill_event` — append-only fills, joined by `order_uid`.
- `position_snapshot` — every 5 minutes during session and at session close.
- `strategy_decision` — every kernel run logs inputs/outputs of one decision.
- `reconciliation_proof` — nightly + at-close proof that bot ledger == broker
  ledger (`match=0` halts new entries).

## Hard rules

1. **Single-writer process.** Only the kernel daemon holds the WAL writer.
2. **No UPDATE / DELETE.** Triggers raise on either. Schema-level enforcement.
3. **Hash chain.** Verified at startup and nightly. Tamper → halt new entries.
4. **Off-host mirror.** Every event also written to a separate volume / object
   store; chain re-verified there nightly.

## What goes here

- Alembic migrations for the v4 ledger schema (separate alembic version path
  from the legacy `migrations/` that v4 cleanup deleted).
- Writer modules: one per table, each enforcing append-only contract.
- Hash-chain verifier.

## What does NOT go here

- Reads. Reads happen through views (`order_current`) and read-only sessions.
- Anything LLM-related.
- Cost model math (lives in `policy/cost_model.lock` and is consumed by
  `execution/`).
