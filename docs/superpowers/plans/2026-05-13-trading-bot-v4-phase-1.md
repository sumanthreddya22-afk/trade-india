# Trading Bot v4 — Phase 1 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-1-design.md`
**Status:** Shipped 2026-05-13 in one session (trading halted; no calendar pressure).

## What landed

```
src/trading_bot/ledger/
  schema.py                 # DDL constants + create_ledger()
  canonical.py              # canonical_json() (excludes ledger_seq + hash fields)
  hash_chain.py             # compute_this_hash / verify_chain / verify_all_chained
  connection.py             # writer/reader factory + acquire_writer_lock
  order_master.py           # OrderIntent + insert + check_idempotent + current_state
  state_event.py            # append_state_event + legal-transition table
  fill_event.py             # append_fill_event
  position_snapshot.py      # write_snapshot + write_snapshot_batch
  strategy_decision.py      # write_decision
  reconciliation.py         # hash_position_vector + compute_recon + write_recon_proof
  orphan_recovery.py        # find_orphans + recover_orphan
  mirror.py                 # init_mirror + mirror_event + mirror_order_master
  __init__.py               # public API
  README.md

tools/
  init_ledger.py            # one-shot DB initialiser
  verify_ledger.py          # hash-chain verifier (nightly + boot)

tests/
  conftest.py               # ledger_conn + ledger_pair fixtures
  test_phase1_schema.py
  test_phase1_immutability_triggers.py
  test_phase1_hash_chain.py
  test_phase1_order_master_idempotent.py
  test_phase1_state_event.py
  test_phase1_fill_event.py
  test_phase1_position_snapshot.py
  test_phase1_strategy_decision.py
  test_phase1_reconciliation.py
  test_phase1_orphan_recovery.py
  test_phase1_off_host_mirror.py
  test_phase1_single_writer_guard.py
```

## P0 acceptance items satisfied

- Append-only ledger (UPDATE / DELETE raise at the trigger).
- Idempotent client_order_id (`check_idempotent` returns absent / active / terminal).
- Mandatory broker-to-ledger reconciliation (`compute_recon` + `write_recon_proof`).
- Hash chain verified at any time (`verify_all_chained`; CLI at `tools/verify_ledger.py`).
- Single-writer guard (`acquire_writer_lock` enforces PID lock; tests confirm second writer is refused).
- Off-host append-only mirror (`mirror.db` sibling; `mirror_event` copies preserves hashes).

## Deferred to Phase 2

- Runtime startup hash-check (kernel boot doesn't exist yet).
- Halt routing on recon mismatch / hash-chain break.
- Real `.lock` content (numeric thresholds in `policy/risk_policy.lock` etc.).
