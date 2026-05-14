# Live capital ramp checklist (Phase 9, TEMPLATE)

**This checklist must be 100% signed before any non-zero live capital
runs through the bot.** Plan v4 §9 + §16: autonomy level remains L2
(autonomous research, no autonomous live trading) until every box here
is checked.

## Pre-ramp gates

- [ ] MVP-OP: 60 consecutive calendar days of reconciliation `match=1`
      and chain verify `ok=1`. Cite `data/ledger/reconciliation_proof`
      query showing 60 unbroken rows.
- [ ] ALPHA: 365 days of paper observation of the seed thesis
      (`ETF_MOMENTUM_v1`) with a passing Tier-1 validation artifact.
- [ ] All other runbooks in `docs/runbooks/` are signed:
  - [ ] `incident_response.md`
  - [ ] `dr_drill.md` (with at least 2 completed drills)
  - [ ] `tax_lot_policy.md`
  - [ ] `daily_ops.md`
- [ ] Off-host mirror verified on a separate physical volume **AND**
      replicated to an off-site target (S3 with object-lock, or
      equivalent).
- [ ] NTP-synced clock; max observed skew < 2s over last 30 days
      (`kill_switch_event` table has zero `clock_skew` fires in that
      window).
- [ ] Operator has read Plan v4 §9 ramp procedure in full.

## Ramp procedure

| Step | $ at risk | Acceptance criteria |
|---|---|---|
| 1. Day 1 | 1% of intended scale | Zero kill-switch fires for 7 days. |
| 2. Day 8 | 5% | Zero fires; reconciliation match=1 every day. |
| 3. Day 15 | 25% | Same as above. |
| 4. Day 30 | 50% | Same as above + drawdown < expected. |
| 5. Day 60 | 100% | Same as above; quarterly DR drill since live. |

Halt the ramp at the first deviation. **Reverting is the default.**

## Operator sign-off

I have read every gate above. I understand the ramp. I am the only
person who will authorise each step.

- Operator (name):
- Date:
- Git SHA of repo at ramp start:
- Alpaca account ID (last 4):
- Intended scale ($):
- Day-1 size (1%):
