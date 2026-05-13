# Trading Bot v4 — Phase 5 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-5-design.md`
**Status:** Shipped 2026-05-13 (trading halted; no calendar pressure).

## What landed

### Research math (pure, broker-free)

```
src/trading_bot/research/
  dsr.py                  # Deflated Sharpe Ratio (Bailey/LdP 2014)
  pbo.py                  # Probability of Backtest Overfitting (Bailey et al. 2016)
  walkforward.py          # walk-forward folds + locked 30% holdout
  ablation.py             # monotone-degradation check
  parameter_plateau.py    # plateau coverage (≥ 25% required)
```

### Hypothesis intake + failure memory

```
src/trading_bot/research/
  persona_schema.py            # validator for prompts/roles/*.v1.md outputs
  hypothesis_intake.py         # adversarial pair + MockPersonaRunner shim
  failure_memory.py            # 90-day reject cache (hash-chained, append-only)
  failure_memory_schema.py     # DDL for failure_memory table
```

### Orchestrator + driver

```
src/trading_bot/research/
  robustness_lab.py            # evaluate() composes DSR + PBO + plateau + ablation
  run_research.py              # run_cycle ties intake + lab + record_validation_artifact
```

### Tests (45 new; 293 total v4 tests green)

```
tests/test_phase5_dsr.py
tests/test_phase5_pbo.py
tests/test_phase5_walkforward.py
tests/test_phase5_ablation_plateau.py
tests/test_phase5_failure_memory.py
tests/test_phase5_persona_schema.py
tests/test_phase5_hypothesis_intake.py
tests/test_phase5_robustness_lab.py
tests/test_phase5_run_research.py
```

### Legacy cleanup

The migrated `research/walkforward.py` + `research/benchmark.py` + entire
`research/backtest_runner/` referenced deleted Phase-0-cleanup modules and
would have failed at import. Per Plan §15 ("Keep; promote into research/
folder with validation_artifact emission") these were placeholders for
the actual research factory. They are now deleted and replaced with the
clean Phase 5 modules above.

## P0 / P1 acceptance items satisfied

P0:

- **Adversarial pair on every hypothesis** — `run_intake` persists both
  `quant_research_lead.v1` and `risk_validator.v1` transcripts to
  `strategy_decision` regardless of which side wins.

P1:

- **Grounding refs on every persona output** — `persona_schema.validate_persona_output`
  rejects outputs without grounding_refs.
- **Strategy failure memory + BH-FDR** — `failure_memory` enforces the
  90-day cooldown. BH-FDR multiple-testing accounting lands in Phase 6
  alongside the mutation engine.

## Deferred to Phase 6+

- Real LLM persona calls (LLM via mailbox or subprocess) — Phase 6.
- Mutation engine consuming `research/search_space_v1.json` — Phase 6.
- BH-FDR multiple-testing correction across a candidate batch — Phase 6.
- Market data wiring for the backtest runner — separate operator step.

## Wall-clock note

**MVP-OP** (60 calendar days of reconciliation match) clock can start
once Phase 5 ships. The operator runs `tools/boot_check.py` +
`tools/verify_ledger.py` nightly; the kernel daemon (Phase 5+) will run
these on a schedule.
