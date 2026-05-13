# Trading Bot v4 — Phase 6 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-6-design.md`
**Status:** Shipped 2026-05-13 (trading halted; no calendar pressure).

## What landed

### Mutation engine

```
src/trading_bot/research/
  mutation_schema.py        # DDL for mutation_log + mutation_outcome (hash-chained)
  mutation_engine.py        # propose_candidates / record_candidate / record_outcome
  bh_fdr.py                 # Benjamini-Hochberg adjust + apply (writes back via new event row)
  run_mutation_cycle.py     # orchestrator: propose → backtest → record → BH-FDR
```

### Persona runner + sandbox isolation

```
src/trading_bot/research/
  persona_runner.py         # SubprocessPersonaRunner + verify_persona_hash
  sandbox.py                # activated() context manager: blocks trading_bot.execution / kernel / risk.precheck imports
```

### Tests (26 new; 319 total v4 tests green)

```
tests/test_phase6_mutation_engine.py         (7 tests)
tests/test_phase6_bh_fdr.py                  (6 tests)
tests/test_phase6_persona_runner.py          (5 tests)
tests/test_phase6_sandbox.py                 (5 tests)
tests/test_phase6_run_mutation_cycle.py      (2 tests)
```

## P1 acceptance items satisfied

- **Mutation sandbox isolation** (Plan §14 P1) — `sandbox.activated()` raises
  `SandboxImportError` when the mutation runner tries to import
  `trading_bot.execution` / `kernel` / `risk.precheck` /
  `shared.alpaca_client`.
- **LLM hallucinated proposal rejected at intake** —
  `mutation_engine.propose_candidates` validates each `mutation_id`
  against the hash-locked `research/search_space_v1.json` and rejects
  unknowns.
- **Strategy failure memory + BH-FDR** — `bh_fdr.apply` writes back
  adjusted p-values + survived flags; rejected candidates flow into
  `failure_memory` via the Phase 5 driver.

## Deferred to Phase 7+

- **Real LLM persona calls** — the SubprocessPersonaRunner ships now;
  the operator wires `command=("claude", "--json")` (or equivalent) in
  Phase 6+ as part of running an actual mutation cycle. CI tests use
  the `runner_callable` seam.
- **Daemon scheduler that runs the mutation cycle monthly** — bundled
  with the kernel daemon (after MVP-OP exit).
- **Second lane** (Mean Reversion or Crypto Trend) — Phase 7.
- **Wheel lane** — Phase 8.
- **Live-readiness packet + operator-signed Tier-3** — Phase 9.

## Wall-clock note

**ALPHA** (~365 calendar days of paper observation for the monthly seed
thesis) starts running once the first Tier-1 `validation_artifact`
passes and the operator promotes ETF_MOMENTUM_v1 from `research_only`
to `shadow`. The mutation engine produces additional candidates inside
that lane during the observation window; only the original seed thesis
counts for ALPHA gate.
