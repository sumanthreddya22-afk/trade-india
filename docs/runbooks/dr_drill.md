# DR drill — quarterly disaster recovery rehearsal (TEMPLATE)

The Plan v4 §5 immutability defense #3 requires the ledger to be
mirrored to a separate physical volume. This drill proves the mirror
actually works.

## Scenario: "Mac mini's internal SSD failed at 03:00"

You wake up to a stuck boot. Time to recover from the off-host mirror.

## Steps

1. From a known-good Mac, install Python 3.11+ and clone the repo:
   ```sh
   git clone <repo-url> Trading && cd Trading
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -e .[dev]
   ```
2. Mount the external SSD (the off-host mirror) as `/Volumes/mirror`.
3. Restore the ledger from the mirror:
   ```sh
   mkdir -p data/ledger
   cp /Volumes/mirror/ledger/mirror.db data/ledger/ledger.db
   # mirror.db becomes the new primary; a fresh mirror is initialised
   # on next boot.
   ```
4. Verify the hash chain:
   ```sh
   python tools/verify_ledger.py
   python tools/boot_check.py
   ```
   Both must pass. If chain verification fails, the mirror is corrupt —
   do not resume trading; restore from off-site backup (S3, Phase 9+).
5. Re-create `.env` from your password manager (Alpaca creds).
6. Boot the daemon in `--once` mode first:
   ```sh
   bot daemon --once
   ```
   Confirm zero errors before launching the persistent daemon.

## Acceptance criteria

| Check | Passed? |
|---|---|
| Hash chain verifies end-to-end | ☐ |
| Recovered position count matches Alpaca account | ☐ |
| Boot check returns ok=true | ☐ |
| Daemon ticks cleanly for 1 hour post-recovery | ☐ |
| Total recovery time recorded (target: < 1 hour) | ☐ |

## Cadence

Quarterly. Mark the next drill date on your calendar before signing
this drill off.

---
**Operator sign-off:**
- Date completed:
- Recovery time (start → daemon green): X minutes
- Issues discovered:
- Next drill date:
