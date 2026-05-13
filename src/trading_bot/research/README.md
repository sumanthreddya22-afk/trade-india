# `research/` — L3 Research Factory

**Status:** Partially populated by the v4 cleanup — surviving backtest
utilities migrated here from the legacy `backtest/`, `walkforward.py`,
`benchmark.py`. New modules land in **Phase 5**.

## Current contents

- `backtest_runner/` — migrated from legacy `backtest/`. Bar store,
  simulator, metrics, reporter. To be wrapped by Phase 5 with point-in-time
  feature-store reads and a `validation_artifact.json` emitter.
- `walkforward.py` — migrated walk-forward utility; gets PBO + DSR wrappers
  in Phase 5.
- `benchmark.py` — migrated benchmark utility.

## Modules (lands Phase 5)

- `hypothesis_intake.py` — adversarial-pair runner (quant_research_lead +
  risk_validator personas). Schema-validates every output; rejects on
  validator block + confidence > 0.7 absent a documented override.
- `robustness_lab.py` — walk-forward + PBO + DSR + ablation. Outputs a
  validation_artifact.
- `search_space_v1.json` — hash-locked search space for the mutation engine.

## Mandate (Plan v4 §1A, §3, §8)

L3 is the **only** place LLM is allowed besides L8 (postmortem). The sandbox
runner that hosts persona calls has no broker credentials, can't import any
module under `kernel/`, `risk/`, `execution/`, and rejects persona output that
doesn't carry a valid `grounding_refs` set.

## What does NOT go here

- Broker submissions.
- Live param mutation.
- Strategy code that the kernel will execute (that lives under `strategies/`
  in Phase 4).
