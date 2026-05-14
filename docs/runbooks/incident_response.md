# Incident response runbook (TEMPLATE — operator fills + signs)

## Severity

- **SEV1** — money at risk RIGHT NOW (broker disagrees with ledger, kill
  switch firing in a loop, unrecovered orphan order).
- **SEV2** — degraded but contained (data feed stale, one kill switch
  fired and cleared, daemon restart loop).
- **SEV3** — no money risk; needs attention within 24h (failed test,
  policy hash mismatch found by manual audit).

## SEV1 protocol

1. **Halt first, diagnose later.** `bot halt --reason "investigating"`.
2. Snapshot the ledger: `cp data/ledger/ledger.db /tmp/ledger-incident.db`.
3. Identify the failing kill switch: `bot status` → look at `active_kills`.
4. Check Alpaca account directly (broker web UI) — confirm position
   vector against `bot strategy list` and last `position_snapshot`.
5. If the two diverge, the **broker is truth** in the short term. Pause
   all bot writes. Manually flatten any rogue positions via Alpaca UI.
6. Page yourself; sleep on it before resuming.

## Communication template

```
[INCIDENT] trading-bot v4 — <SEV1|SEV2|SEV3>
What: <one-line summary>
When detected: <ISO-8601 ts>
Money at risk: <yes/no, $X if yes>
Actions taken: <halt? rollback? manual intervention?>
Next check: <ts>
```

(For solo operation, write this to yourself in a file under
`docs/incidents/<date>-<slug>.md` and commit at the end.)

## Postmortem

Within 7 days of SEV1 / 14 days of SEV2:
- Timeline (ts, event, action).
- Root cause (5-whys).
- Detection lag (when did it actually start vs when did you notice?).
- Fix (code? policy? process?).
- Prevention (test? alert? runbook update?).

Commit the postmortem to `docs/incidents/`.

---
**Operator sign-off:**
- Author:
- Date:
