# Phase 9 — Operational runbooks

These runbooks are templates. **Each one must be filled in and signed by
the operator before the corresponding action is taken.** Live capital is
gated on every runbook in this directory being completed.

| Runbook | What it gates |
|---|---|
| `incident_response.md` | Any production incident (paper or live). |
| `dr_drill.md` | Quarterly disaster-recovery rehearsal. |
| `tax_lot_policy.md` | Tax-lot identification policy for closed positions. |
| `live_ramp_checklist.md` | The 1% live capital ramp (Phase 9). |
| `daily_ops.md` | Daily operator routine (boot check, eyeball, sign-off). |
| `mutation_cycle_setup.md` | Configuring the monthly mutation cycle. |

Each runbook ends with a sign-off section. The operator commits the
signed runbook to git; the live-ramp checklist asserts each prior
runbook is signed before proceeding.
