# `obs/` — L8 Observability + Postmortem

**Status:** Partially populated — surviving dashboard migrated here from
legacy `src/trading_bot/dashboard/` during the v4 cleanup. Rewiring its
queries to read **only** from the v4 ledger lands in **Phase 6+** (after
the ledger ships in Phase 1 and the registry in Phase 4).

## Current contents

- `dashboard/` — migrated as-is. Imports still reference deleted legacy
  modules; the dashboard will break until rewired. That's expected — Plan v4
  §16 lists L1 (assisted research) as the only autonomy level always-on, and
  the dashboard is read-only telemetry the operator looks at on demand.

## Modules (lands Phase 1+)

- `scorecard_runner.py` — daily scorecard derived only from ledger rows.
- `drift_detector.py` — live-vs-model fill drift, runs nightly.
- `postmortem_writer.py` — the L8 daily report. **LLM allowed here** under
  the persona contract in `prompts/roles/`. Output is read-only to the kernel.

## Mandate (Plan v4 §3, §1A)

L8 is the second of two layers where LLM is allowed (the other being L3).
Personas write the report; the kernel reads nothing back. Every metric in the
report must trace to a `ledger_seq`.

## What does NOT go here

- Order submission.
- Risk decisions.
- Strategy authoring.
