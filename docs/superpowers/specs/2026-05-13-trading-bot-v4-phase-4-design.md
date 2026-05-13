# Trading Bot v4 — Phase 4 Design (Strategy Registry + Validation Artifacts + Promotion Gate + ETF Momentum Seed)

**Source plan:** Plan v4 §3 (L4 Strategy Registry) + §4 (validation_policy.lock) + §8 (search space + mutation discipline) + §13 (Promotion scorecard — three tiers) + §14 P1 (tiered validation gate).

**Phase duration in plan:** 1 calendar week. **Trading remains halted.**

## Goal

Ship the **L4 Strategy Registry** + the **promotion gate**. This is the
artifact layer between research (L3) and the kernel (L5). No strategy can
emit orders unless its registry row references a `validation_artifact_id`
that has passed the appropriate tier in `policy/validation_policy.lock`.

Also: register the **ETF Momentum v1** seed strategy row referencing
`thesis_id: edge_thesis_v1`. Its status starts as `research_only`; Phase 5
ships the research factory that produces the first Tier-1 artifact.

## Schema

All three tables live in `ledger.db` (same single-writer guard as
Phase 1).

### `strategy_version`

```sql
CREATE TABLE strategy_version (
    strategy_id           TEXT NOT NULL,
    strategy_ver          INTEGER NOT NULL,
    code_hash             TEXT NOT NULL,
    config_hash           TEXT NOT NULL,
    thesis_id             TEXT NOT NULL,
    hypothesis_id         TEXT NOT NULL,
    validation_artifact_id TEXT,       -- nullable until Tier-1 passes
    lane                  TEXT NOT NULL,
    status                TEXT NOT NULL,   -- research_only | shadow | tiny_paper | scaled_paper | live | observe_only | reduce_only | halted
    expiry_date           TEXT,            -- ISO date; promotion packet sets, NULL for research_only
    owner                 TEXT NOT NULL,
    created_ts            TEXT NOT NULL,
    PRIMARY KEY (strategy_id, strategy_ver)
);
```

Append-only via new `strategy_ver` row. Trigger raises on UPDATE / DELETE.

### `validation_artifact`

```sql
CREATE TABLE validation_artifact (
    artifact_id           TEXT PRIMARY KEY,         -- sha256(canonical(metrics))
    strategy_id           TEXT NOT NULL,
    strategy_ver          INTEGER NOT NULL,
    tier                  TEXT NOT NULL,            -- research_candidate | paper_candidate | live_candidate
    produced_ts           TEXT NOT NULL,
    code_hash             TEXT NOT NULL,
    config_hash           TEXT NOT NULL,
    metrics_json          TEXT NOT NULL,            -- canonical metric bundle
    lens                  TEXT NOT NULL,            -- pessimistic | broker_paper | raw (must be 'pessimistic' for promotion)
    pass                  INTEGER NOT NULL,         -- 0 | 1
    failure_reasons       TEXT,                     -- JSON list when pass=0
    prev_hash             TEXT NOT NULL,
    this_hash             TEXT NOT NULL
);
```

Hash-chained, append-only.

### `promotion_packet`

```sql
CREATE TABLE promotion_packet (
    packet_id             TEXT PRIMARY KEY,         -- sha256(canonical contents)
    strategy_id           TEXT NOT NULL,
    strategy_ver          INTEGER NOT NULL,
    target_tier           TEXT NOT NULL,            -- paper_candidate | live_candidate
    code_hash             TEXT NOT NULL,
    config_hash           TEXT NOT NULL,
    validation_artifact_id TEXT NOT NULL,           -- the Tier-N artifact that justified promotion
    paper_scorecard_id    TEXT,                     -- Phase 6+
    risk_review_id        TEXT,                     -- multi-persona panel id (Phase 5+)
    known_failure_modes_json TEXT,
    expiry_date           TEXT NOT NULL,            -- 90 days unless re-validated
    operator_signed       INTEGER NOT NULL,         -- 0 | 1 (Tier-3 requires 1)
    created_ts            TEXT NOT NULL,
    prev_hash             TEXT NOT NULL,
    this_hash             TEXT NOT NULL
);
```

Hash-chained, append-only.

## Promotion gate (the single-entry check)

```python
registry.promotion.gate(
    conn,
    strategy_id: str,
    strategy_ver: int,
    target_status: str,             # shadow | tiny_paper | scaled_paper | live
    policy: PolicyBundle,
    now: datetime,
) -> PromotionDecision
```

`PromotionDecision` is a frozen dataclass with `allowed`, `reason`,
`tier_required`, `artifact_id`, `human_signoff_required`.

Logic per Plan §13:

- `research_only → shadow`: requires `validation_artifact` with
  `tier='research_candidate'` and `pass=1` for this strategy_ver.
- `shadow → tiny_paper`: requires Tier-2 (`paper_candidate`) artifact +
  operational readiness checks + 3-persona panel block-counts (Phase 5
  wires the panel; Phase 4 reads a `risk_review_id` if present).
- `tiny_paper → scaled_paper`: same Tier-2 artifact still passing.
- `scaled_paper → live`: Tier-3 (`live_candidate`) artifact + cadence-
  aware paper window + human sign-off recorded in `promotion_packet`.

The gate validates each policy threshold from
`policy/validation_policy.lock` against the artifact's metrics. If any
threshold fails, the gate refuses promotion with a specific reason.

## Expiry handling

Plan §13 Tier-3 row: "expiry_date (90 days unless re-validated)". When a
strategy_version's `expiry_date < now`, the kernel treats it as if the
status were `research_only` — no new orders. The registry helper
`is_version_active(version, now)` is the kernel's check.

## Search space (`research/search_space_v1.json`)

Plan §8: "Search space declared in research/search_space_v1.json (hash-
locked)." The mutation engine (Phase 6) consumes this; the LLM in L3 may
only propose mutation_ids that map to a registered dimension here.

Phase 4 ships the JSON file with the ETF Momentum v1 dimensions
(parameter / feature / universe variants), the loader
(`registry/search_space.py`), and adds it to `policy/HASHES`.

## Files shipped

```
src/trading_bot/registry/
  __init__.py
  schema.py
  strategies.py
  validation_artifacts.py
  promotion.py
  search_space.py

research/
  search_space_v1.json

tools/
  register_seed_strategy.py        # one-shot: writes ETF_MOMENTUM_v1 row

tests/
  test_phase4_registry_schema.py
  test_phase4_strategy_version.py
  test_phase4_validation_artifact.py
  test_phase4_promotion_tier1.py
  test_phase4_promotion_tier2.py
  test_phase4_promotion_tier3.py
  test_phase4_search_space.py
  test_phase4_seed_strategy.py
```

## P0 / P1 acceptance items satisfied

P1:

- **Tiered validation gate** — `registry.promotion.gate` enforces:
  "Strategy without a Tier-1 artifact cannot enter paper. Without a
  Tier-2, cannot go to scaled paper. Without a Tier-3, cannot go live."

P0 (carry-over):

- **Strategy identity in every risk decision** — already satisfied
  Phase 2; Phase 4 strengthens it (every strategy_version row has a
  `validation_artifact_id` for traceability).

## Deferred to later phases

- **Multi-persona panel verdicts** — Phase 5 wires real persona calls;
  Phase 4 ships the schema field (`risk_review_id`).
- **Backtest runner that emits validation_artifact** — Phase 5
  research factory.
- **Mutation engine consuming search_space_v1.json** — Phase 6.
- **Paper scorecard** — Phase 5+ (after some paper-trade history accumulates).
