# Trading Bot v4 — Phase 4 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-4-design.md`
**Status:** Shipped 2026-05-13 (trading halted; no calendar pressure).

## What landed

### Strategy registry

```
src/trading_bot/registry/
  __init__.py
  schema.py                  # DDL: strategy_version + validation_artifact + promotion_packet
  strategies.py              # register_version / get_active_version / list_versions / expiry
  validation_artifacts.py    # evaluate_tier / record_validation_artifact / find_latest_pass
  promotion.py               # gate / record_promotion_packet / Tier-3 human sign-off
  search_space.py            # loader + validator for research/search_space_v1.json

research/
  search_space_v1.json       # ETF Momentum v1 dimensions (parameter / feature / universe)

tools/
  register_seed_strategy.py  # one-shot: ETF_MOMENTUM_v1 at status=research_only
```

### Seed strategy registered

```
ETF_MOMENTUM_v1 v1   thesis=edge_thesis_v1   lane=etf_momentum   status=research_only
```

Once Phase 5 ships the research factory and produces the first
research_candidate artifact, the operator can promote to `shadow`.

### Tests (37 new; 248 total v4 tests green)

```
tests/test_phase4_registry_schema.py        (4 tests)
tests/test_phase4_strategy_version.py       (7 tests)
tests/test_phase4_validation_artifact.py    (12 tests)
tests/test_phase4_promotion_gate.py         (7 tests)
tests/test_phase4_search_space.py           (8 tests)
tests/test_phase4_seed_strategy.py          (1 test against live data/ledger.db)
```

## P0 / P1 acceptance items satisfied

P1:

- **Tiered validation gate** — `registry.promotion.gate` enforces:
  "Strategy without a Tier-1 artifact cannot enter paper. Without a
  Tier-2, cannot go to scaled paper. Without a Tier-3, cannot go live."
- **Validation lock cooldown** — already shipped Phase 2; the gate reads
  the locked thresholds via PolicyBundle so cooldown semantics apply.

## Deferred to later phases

- **Multi-persona panel verdicts** — Phase 5 wires real persona calls;
  Phase 4 ships the schema field (`risk_review_id`).
- **Backtest runner that emits validation_artifact** — Phase 5 research
  factory.
- **Mutation engine consuming search_space_v1.json** — Phase 6.
- **Paper scorecard** — Phase 5+ (after some paper-trade history accumulates).
