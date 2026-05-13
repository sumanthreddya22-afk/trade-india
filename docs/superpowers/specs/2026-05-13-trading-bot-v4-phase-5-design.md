# Trading Bot v4 — Phase 5 Design (Research Factory)

**Source plan:** Plan v4 §3 (L3 Research Factory) + §1A (adversarial pair on every hypothesis) + §8 (mutation engine + multiple-testing correction) + §13 (Tier-1 Research candidate gates).

**Phase duration in plan:** 3 calendar weeks. **Trading remains halted.**

## Goal

Ship the **L3 research factory** — the only layer where LLM is allowed
besides L8 postmortem. It produces `validation_artifact` rows that gate
strategy promotion (Phase 4).

Per Plan §1A and §8 the factory has four sub-modules:

1. **Hypothesis intake** — adversarial pair (`quant_research_lead.v1`
   proposer + `risk_validator.v1` critic). Both transcripts written to
   `strategy_decision` regardless of who wins. Schema-validated outputs;
   block at intake if validator returns `verdict=block` with
   `confidence > 0.7` absent a documented operator override.
2. **Backtest runner** — point-in-time data, pessimistic-cost lens
   (Phase 3 `cost_model.py`), no lookahead. Produces a P&L series.
3. **Robustness lab** — walk-forward + locked holdout + Monte Carlo
   path perturbation + PBO + DSR + ablation + parameter plateau.
4. **Failure memory** — rejected `hypothesis_hash` cached 90 days;
   auto-reject re-submissions of identical hypotheses.

This phase ships the **math** + the **orchestrator** + the **mock
persona shim**. Real LLM persona calls wire in Phase 6 alongside the
mutation engine. Real market data wiring is operator-driven (the
backtest runner consumes injected P&L series for Phase 5 tests).

## Math modules (pure, no dependencies on broker/data)

### `research/dsr.py` — Deflated Sharpe Ratio

Bailey & López de Prado (2014). The DSR adjusts the observed Sharpe
ratio for: (a) non-normality of returns (skew + kurtosis), (b) number of
trials, (c) variance of trials. Returns the probability that the true
Sharpe is above zero given the observed sample. Plan §4 thresholds:
Tier-1 ≥ 0.50, Tier-2 ≥ 0.70, Tier-3 ≥ 0.85.

Public API:
```python
dsr.deflated_sharpe(
    returns: Sequence[float],        # period returns (e.g. monthly)
    n_trials: int = 1,                # candidate strategies tested
    variance_trials: float = 1.0,     # variance across the trial Sharpes
    benchmark_sr: float = 0.0,        # null hypothesis SR
) -> DSRResult
```

`DSRResult` carries `observed_sr`, `deflated_sr`, `probability_sr_positive`.

### `research/pbo.py` — Probability of Backtest Overfitting

Bailey, Borwein, López de Prado, Zhu (2016). PBO splits the returns
matrix (strategies × periods) into S random in-sample / out-of-sample
permutations; counts the fraction where the best in-sample strategy
ranks below median out-of-sample. Plan §4 thresholds: Tier-1 ≤ 0.50,
Tier-2 ≤ 0.35, Tier-3 ≤ 0.25.

Public API:
```python
pbo.probability_of_overfit(
    returns_matrix: np.ndarray,      # shape (strategies, periods)
    n_splits: int = 16,               # number of random partitions
    rng_seed: int = 42,
) -> PBOResult
```

### `research/walkforward.py` — walk-forward folds + locked holdout

Splits a time series into N folds (train_window, test_window) plus a
**locked holdout** = last 30% of history that no parameter selection
ever touches.

Public API:
```python
walkforward.build_folds(
    start: date, end: date,
    train_months: int = 24, test_months: int = 6,
    min_folds: int = 5,
    holdout_pct: float = 0.30,
) -> WalkforwardSchedule
```

### `research/ablation.py` — monotone degradation check

Plan §13 Tier-1: "ablation produces monotone degradation". Given a
sequence of (feature_set, score) pairs ordered by feature richness, the
score must be monotone — removing features should not improve
performance. Returns the degradation map + a pass / fail flag.

### `research/parameter_plateau.py` — plateau coverage

Plan §13 Tier-1: "parameter plateau ≥ 25% of swept range". Given the
metric across the parameter sweep, the function returns the largest
contiguous region (in % of swept range) where the metric is within a
tolerance of the maximum.

## Orchestrator — `research/robustness_lab.py`

Single-entry function that consumes:

- A returns time series (or a callable that produces one per parameter
  point — for ablation / plateau)
- The candidate parameter sweep
- The shipped validation thresholds (loaded from
  `policy/validation_policy.lock`)

and produces a `RobustnessReport` dataclass containing every metric the
Tier-N evaluation in `registry.evaluate_tier` requires.

```python
robustness_lab.evaluate(
    *,
    primary_returns: Sequence[float],
    walkforward_returns: Sequence[Sequence[float]],   # one series per fold
    holdout_returns: Sequence[float],
    sweep_metric: Mapping[str, float],                # parameter -> metric
    ablation_series: Sequence[tuple[str, float]],     # ordered most→least rich
    n_trials: int,
    variance_trials: float,
    tier: str,                                         # research_candidate | …
) -> RobustnessReport
```

## Hypothesis intake — `research/hypothesis_intake.py`

The adversarial pair (Plan §1A). Phase 5 ships:

- The schema validator for persona outputs (matches the JSON shape in
  `prompts/roles/*.v1.md`).
- A `MockPersonaRunner` that returns canned, valid persona outputs for
  testing.
- An intake function that runs the pair, records both transcripts to
  `strategy_decision`, and returns a verdict + the
  `hypothesis_id`.

The real LLM call (via the mailbox / Claude CLI subprocess) wires in
Phase 6.

Public API:
```python
hypothesis_intake.run_intake(
    conn,
    *,
    strategy_id: str,
    hypothesis: HypothesisProposal,
    research_lead_runner: PersonaRunnerT,
    risk_validator_runner: PersonaRunnerT,
    policy: PolicyBundle,
    now: datetime,
) -> IntakeResult
```

## Failure memory — `research/failure_memory.py`

Plan §8: "rejected candidates stored with `hypothesis_hash` + reason;
same `hypothesis_hash` auto-rejected for 90 days unless thesis changes".

Two operations:

```python
failure_memory.record_rejection(
    conn, hypothesis_hash, reason, now,
)
failure_memory.is_blocked(
    conn, hypothesis_hash, now, ttl_days=90,
) -> tuple[bool, str]
```

Backed by a new append-only table `failure_memory` in `ledger.db`
(hash-chained per Plan §5 immutability).

## End-to-end driver — `research/run_research.py`

A thin orchestrator that ties the four sub-modules together for a
single strategy_id. For Phase 5 the driver takes injected P&L series +
sweep metrics (real market-data wiring lands as a separate operator
step). It:

1. Computes `hypothesis_hash`; checks `failure_memory.is_blocked`.
2. Runs `hypothesis_intake.run_intake` with mock personas.
3. Calls `robustness_lab.evaluate` on the supplied series.
4. Calls `registry.record_validation_artifact` for the target tier.
5. Returns the `(artifact_id, pass_or_fail)` tuple to the caller.

## Files shipped

```
src/trading_bot/research/
  __init__.py
  README.md                  # already shipped Phase 0
  dsr.py
  pbo.py
  walkforward.py             # clean rewrite (legacy migrated file was broken)
  ablation.py
  parameter_plateau.py
  failure_memory.py
  failure_memory_schema.py   # DDL for the failure_memory table
  hypothesis_intake.py
  persona_schema.py          # validator for prompts/roles/*.v1.md outputs
  robustness_lab.py
  run_research.py

tests/
  test_phase5_dsr.py
  test_phase5_pbo.py
  test_phase5_walkforward.py
  test_phase5_ablation.py
  test_phase5_parameter_plateau.py
  test_phase5_failure_memory.py
  test_phase5_persona_schema.py
  test_phase5_hypothesis_intake.py
  test_phase5_robustness_lab.py
  test_phase5_run_research.py
```

## P0 / P1 acceptance items satisfied

P0:

- **Adversarial pair on every hypothesis** — `hypothesis_intake.run_intake`
  runs the pair and persists both transcripts to `strategy_decision`.

P1:

- **Grounding refs on every persona output** — persona-schema validator
  rejects outputs without `grounding_refs`; the runner's contract is to
  verify each ref points to a real ledger row before accepting the
  call.
- **Strategy failure memory** — `failure_memory.is_blocked` enforces the
  90-day cooldown.

## Deferred to later phases

- **Real persona calls** (LLM via mailbox or subprocess) — Phase 6
  alongside the mutation engine.
- **Market data wiring for the backtest runner** — a separate operator
  step; the Phase 5 math is data-agnostic.
- **Mutation engine** — Phase 6 (consumes `research/search_space_v1.json`
  shipped in Phase 4).
- **BH-FDR multiple-testing correction across a candidate batch** —
  Phase 6 (the mutation engine has the candidate set; Phase 5 ships the
  per-strategy math).

## Wall-clock note

Plan §12 lists **MVP-OP** (60 calendar days of reconciliation match)
as the next milestone after Phase 5. The clock starts the day after
Phase 5 ships; the operator runs the integrity-check + reconciliation
nightly; the code already ships (Phase 1 + Phase 2 boot check).
