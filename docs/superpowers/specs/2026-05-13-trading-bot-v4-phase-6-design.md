# Trading Bot v4 — Phase 6 Design (Mutation Engine + BH-FDR + Sandbox Isolation + Persona Runner)

**Source plan:** Plan v4 §8 (Mutation Engine + Multiple-Testing Correction) + §1A (personas hash-locked at call time) + §14 P1 (Mutation sandbox isolation; LLM hallucinated proposal rejected at intake).

**Phase duration in plan:** 2 calendar weeks. **Trading remains halted.**

## Goal

Ship the mutation engine that turns the hash-locked `research/search_space_v1.json` (Phase 4) into a *bounded* batch of candidate strategy variants per month, applies **Benjamini-Hochberg FDR** correction across the batch, and emits surviving variants as Tier-1 candidates via the Phase 5 `run_cycle` driver.

Also ship: a **real-persona shim** (subprocess) that hash-verifies the persona file at call time, plus a **sandbox import guard** that blocks the mutation runner from importing `kernel/`, `risk/`, `execution/`, or any broker-credentialed surface.

## Mutation engine

Plan §8 stages (the bot does each, gated):

1. **Idea intake** — catalogue each candidate variant with a
   pre-declared `mutation_id` (must map to a dimension in
   `research/search_space_v1.json`). Each candidate gets one entry in
   `mutation_log`.
2. **Mutation design** — generate parameter / feature / universe
   variants within the monthly budget (default 64 per family).
3. **Backtest sandbox** — point-in-time backtest with the pessimistic
   cost lens; store the raw p-value or t-stat per candidate.
4. **Robustness lab** — walk-forward + PBO + DSR + plateau + ablation
   (Phase 5 modules).
5. **Failure memory** (Phase 5) — rejected `hypothesis_hash` blocked
   for 90 days.
6. **Promotion** — surviving candidates become Tier-1 `validation_artifact`
   rows via the Phase 5 `run_cycle` driver.

**LLM authority** (Plan §8 explicit box): LLM may propose
`mutation_id`s and write the hypothesis rationale. **LLM cannot**
author strategy code, modify the search space, alter the cost model,
change thresholds, or place trades. Mutation_id is deterministically
generated from the search space; LLM only selects which to prioritise.

### Mutation log schema

```sql
CREATE TABLE mutation_log (
    ledger_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      TEXT NOT NULL UNIQUE,    -- sha256(thesis + mutation_id + value)
    thesis_id         TEXT NOT NULL,
    family            TEXT NOT NULL,           -- mutation family group (e.g., "parameter:lookback_months")
    mutation_id       TEXT NOT NULL,           -- dimension key from search_space
    variant_value     TEXT NOT NULL,           -- the specific value from the dimension's domain
    cycle_id          TEXT NOT NULL,           -- YYYY-MM batch this candidate belongs to
    proposed_ts       TEXT NOT NULL,
    raw_p_value       REAL,                    -- nullable until backtested
    adjusted_p_value  REAL,                    -- nullable until BH-FDR runs
    survived          INTEGER,                 -- 0|1, null = pending
    rationale         TEXT,                    -- LLM-generated text; provenance for audit
    proposer          TEXT NOT NULL,           -- 'mutation_engine' | 'llm_mlops' | 'operator'
    prev_hash         TEXT NOT NULL,
    this_hash         TEXT NOT NULL
);
```

Hash-chained, append-only (UPDATE on `raw_p_value` / `adjusted_p_value`
during a single batch is implemented as a new row with the same
`candidate_id` — but practical simplification: a single new row with
the same candidate_id triggers UNIQUE. Solution: a separate
`mutation_outcome` event table that records the backtest result with
prev/this hash, joined to mutation_log by candidate_id.)

For Phase 6 simplicity, we store the candidate at proposal time with
`raw_p_value=null`, then record outcome via append to a sibling
`mutation_outcome` table.

```sql
CREATE TABLE mutation_outcome (
    ledger_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      TEXT NOT NULL,
    outcome_ts        TEXT NOT NULL,
    raw_p_value       REAL NOT NULL,
    adjusted_p_value  REAL,                    -- null until BH-FDR batches
    survived          INTEGER NOT NULL,
    sanity_checks     TEXT,                    -- JSON
    prev_hash         TEXT NOT NULL,
    this_hash         TEXT NOT NULL
);
```

### Mutation engine API

```python
mutation_engine.propose_candidates(
    *,
    thesis_id: str,
    cycle_id: str,                  # "YYYY-MM"
    search_space: SearchSpace,
    families: Sequence[str] | None = None,
    budget_per_family: int = 64,
    rng_seed: int = 42,
) -> list[Candidate]
```

`Candidate` is a frozen dataclass `(candidate_id, thesis_id, family,
mutation_id, variant_value, hypothesis_hash, rationale)`. The
`candidate_id` is deterministic from `(thesis_id, mutation_id,
variant_value)` so re-running the same cycle produces the same
candidate ids (idempotent).

### Outcome + BH-FDR API

```python
mutation_engine.record_outcome(
    conn, candidate_id, raw_p_value, sanity_checks
)
bh_fdr.apply(
    conn, cycle_id, alpha=0.10
) -> BHFDRReport
```

`bh_fdr.apply` reads all `mutation_outcome` rows for the cycle, applies
Benjamini-Hochberg, writes back adjusted_p + survived per candidate.

## BH-FDR

The Benjamini-Hochberg procedure controls the **false discovery rate**
(expected proportion of false positives among rejections). For m
hypotheses with raw p-values p₁ ≤ p₂ ≤ … ≤ pₘ, find the largest k such
that pₖ ≤ k/m · α; reject hypotheses 1..k. The adjusted p-value for
candidate (i) is `min_{j>=i}(p_j · m / j)` (clamped to ≤ 1).

Plan §4 / §8: `alpha=0.10`.

## Persona runner (subprocess shim)

Plan §1A: "L3 and L8 calls reference personas by hash; the persona
content embedded in the prompt is pinned at call time, and a hash
mismatch halts the call."

`SubprocessPersonaRunner` shape:

```python
runner = SubprocessPersonaRunner(
    role="quant_research_lead.v1",
    persona_path="prompts/roles/quant_research_lead.v1.md",
    command=["claude", "--json"],          # operator-configured
    hashes_path="policy/HASHES",
)
output = runner(proposal)                    # returns persona JSON output dict
```

The runner:

1. Reads `policy/HASHES`; computes SHA-256 of the persona file; halts
   on mismatch.
2. Constructs the prompt with the (verified) persona content + the
   proposal.
3. Spawns the subprocess; writes prompt to stdin; reads JSON from
   stdout.
4. Validates the JSON via `persona_schema.validate_persona_output`.
5. Returns the dict.

For Phase 6 tests we ship a `ScriptedSubprocessRunner` that returns a
fixed JSON without spawning anything — confirming the hash-check + the
parsing path without requiring a real Claude CLI in CI.

## Sandbox isolation

Plan §14 P1: "Mutation sandbox isolation — Mutation runner cannot
access broker creds or place orders; attempting to import execution
module raises ImportError."

Phase 6 ships `research.sandbox.activate()` which installs a
`sys.meta_path` import hook that blocks the listed modules:

```python
BLOCKED = (
    "trading_bot.execution",
    "trading_bot.kernel",
    "trading_bot.risk.precheck",       # the kernel-gate path
    # ledger writers are allowed: the sandbox needs to write
    # mutation_log + mutation_outcome rows. The router that submits
    # broker orders is what we block.
)
```

The hook is installed for the duration of `with sandbox.activated(): ...`
and uninstalls on exit.

## Files shipped

```
src/trading_bot/research/
  mutation_schema.py
  mutation_engine.py
  bh_fdr.py
  persona_runner.py
  sandbox.py
  run_mutation_cycle.py

tests/
  test_phase6_mutation_schema.py
  test_phase6_mutation_engine.py
  test_phase6_bh_fdr.py
  test_phase6_persona_runner.py
  test_phase6_sandbox.py
  test_phase6_run_mutation_cycle.py
```

## P0 / P1 acceptance items satisfied

P1:

- **Mutation sandbox isolation** — `sandbox.activated()` raises
  `ImportError` when the mutation runner tries to import
  `trading_bot.execution`.
- **LLM hallucinated proposal rejected at intake** — `mutation_engine`
  validates each proposed `mutation_id` against the hash-locked
  `search_space_v1.json` and refuses unknowns at intake.
- **Strategy failure memory + BH-FDR** — `bh_fdr.apply` writes back the
  adjusted p-value and the survived flag; the Phase 5 driver records
  rejections into `failure_memory`.

## Deferred to Phase 7+

- **Second lane** (Mean Reversion or Crypto Trend) — Phase 7.
- **Wheel lane** — Phase 8.
- **Live readiness packet** + the operator-signed Tier-3 packet — Phase 9.
- **Daemon scheduler** that runs the mutation cycle monthly — bundled
  with the kernel daemon (after MVP-OP exit).
