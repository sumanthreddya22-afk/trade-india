# Trading Bot v4 — Phase 1 Design (Ledger Schema + Idempotent Submission + Reconciliation)

**Source plan:** Plan v4 §5 (Ledger schema) + §6 (Idempotency contract) + §14 (P0 acceptance items: append-only ledger, idempotent client_order_id, mandatory broker-to-ledger reconciliation, hash chain verified at startup and nightly, single-writer guard, off-host append-only mirror).

**Phase duration in plan:** 2 calendar weeks. **Trading remains halted.**

## Goal

Stand up the **append-only, hash-chained, event-sourced ledger** that becomes the single source of truth for every numeric in v4. No trading happens against it yet — Phase 2 wires the risk kernel, Phase 3 hardens the execution adapter — but the schema, the writers, the hash chain, the idempotency contract, and the off-host mirror all ship and pass tests at the end of this phase.

## Tables (6 + 1 view)

All tables live in `ledger.db` (single SQLite file) plus an off-host mirror at `mirror.db`. Each table is `INSERT`-only at the DB level (triggers raise on UPDATE/DELETE). Event tables carry a hash chain.

1. **`order_master`** — immutable identity record per order. Inserted once at intent time; never updated.
2. **`order_state_event`** — append-only state transitions. Hash chained: `this_hash = sha256(prev_hash || canonical(row))`.
3. **`fill_event`** — append-only fills, joined by `order_uid` (never by `broker_order_id`). Hash chained.
4. **`order_current`** — derived VIEW. Reads MAX(ledger_seq) per order_uid to give current state. No stored "current" rows.
5. **`position_snapshot`** — taken every 5 min during session and at session close. Hash chained.
6. **`strategy_decision`** — one row per kernel run. Carries `strategy_id`, `code_hash`, `config_hash`, `policy_hash`, `feature_snapshot_id`, `intent_json`, `risk_decision`, `risk_reason`, `emitted_client_order_id`. Hash chained.
7. **`reconciliation_proof`** — nightly + at-close proof that `bot_hash == broker_hash` (SHA-256 of position vectors). `match=0` halts new entries (Phase 2 wires the halt).

Schema lives as Python DDL constants in `src/trading_bot/ledger/schema.py` so it can be hashed into `policy/HASHES` and asserted by tests.

## Hash chain

For every event-table row:

- `prev_hash` = previous row's `this_hash` (or `'0' * 64` for the very first row).
- `this_hash = sha256( prev_hash.encode() + canonical(row).encode() )` where `canonical(row)` is `json.dumps(row_dict, sort_keys=True, separators=(",", ":"), default=str)` and `row_dict` excludes the hash fields themselves.
- Computed in Python inside an `IMMEDIATE` transaction so two writers cannot interleave. (Single-writer guard at the process level provides the primary defense; the IMMEDIATE transaction is belt-and-braces.)

The verifier `ledger.hash_chain.verify_chain(table)` walks all rows in `ledger_seq` order, recomputes each hash, and asserts equality with the stored `this_hash`. Used at startup (Phase 2 risk kernel boot) and nightly (Phase 1 supplies a CLI; nightly scheduling is operator's choice).

## Idempotency contract (Plan §5 box)

`client_order_id = YYYYMMDD_<strategy>_<symbol>_<seq>`. UNIQUE in `order_master`.

`ledger.order_master.check_idempotent(cid)` returns one of:

- `("absent", None)` — never seen; the caller may submit.
- `("active", <order_uid>)` — exists with current state ∈ {`submitted`, `acked`, `partially_filled`, `filled`}; the caller MUST refuse to re-submit.
- `("terminal", <order_uid>)` — exists with current state ∈ {`rejected`, `cancelled`, `expired`}; the caller may submit *a new* client_order_id (the existing one stays terminal).

The router (Phase 3 wires this) calls `check_idempotent` before every submission.

## Orphan recovery

Any `order_state_event` row with `to_state='submitted'` and `event_ts` older than 60 seconds without a subsequent `to_state='acked'` is an orphan. The recovery procedure (Phase 3 wires the runtime path; Phase 1 supplies the helper):

1. Query the broker for `broker_order_id` matching `client_order_id`.
2. If found: append `to_state='acked'` row with the discovered `broker_order_id`.
3. If not found: append `to_state='cancelled'` row with `reason='orphan_recovered'`.

## Single-writer guard

Process-level: only the kernel daemon process holds the SQLite WAL writer. All other tools (CLI, dashboard, tests) open the DB read-only. Enforcement is via an advisory PID lock file at `data/ledger/.writer.lock`. Acquire on writer-process start; verify the PID is alive on every write; refuse to write if the lock is held by a foreign PID.

For Phase 1 the lock file is implemented; it's exercised by `init_ledger.py` and the test suite. The kernel daemon doesn't exist yet (Phase 1+); when it lands it will be the lock holder.

## Off-host append-only mirror

Every event written to `ledger.db` is also written, in order, to `mirror.db`. For Phase 1 the mirror is a sibling file (`data/ledger/mirror.db`). Operator can later move it to a different volume or object store — the writer API stays the same.

The mirror's hash chain is re-verified nightly (Phase 1 supplies the CLI). If the mirror diverges, the operator gets an alert; new entries halt (Phase 2 wires the halt routing).

## Files shipped this phase

```
src/trading_bot/ledger/
├── __init__.py                  # public API
├── README.md                    # (already shipped Phase 0)
├── schema.py                    # DDL constants + create_ledger()
├── connection.py                # writer/reader factory; single-writer lock
├── canonical.py                 # canonical_json(); excludes hash fields
├── hash_chain.py                # hash compute + verify
├── order_master.py              # insert_order_master + check_idempotent
├── state_event.py               # append_state_event (hash-chained)
├── fill_event.py                # append_fill_event (hash-chained)
├── position_snapshot.py         # write_snapshot (hash-chained)
├── strategy_decision.py         # write_decision (hash-chained)
├── reconciliation.py            # compute_recon + write_recon_proof
├── orphan_recovery.py           # find_orphans helper
└── mirror.py                    # write_to_mirror (every event)

tools/
├── init_ledger.py               # one-shot DB initializer
└── verify_ledger.py             # hash-chain verifier (used nightly)

tests/
├── test_phase1_schema.py
├── test_phase1_immutability_triggers.py
├── test_phase1_hash_chain.py
├── test_phase1_order_master_idempotent.py
├── test_phase1_state_event.py
├── test_phase1_fill_event.py
├── test_phase1_position_snapshot.py
├── test_phase1_strategy_decision.py
├── test_phase1_reconciliation.py
├── test_phase1_orphan_recovery.py
├── test_phase1_off_host_mirror.py
└── test_phase1_single_writer_guard.py
```

## P0 acceptance items satisfied (Plan §14)

- **Append-only ledger** — UPDATE / DELETE on any ledger table raises schema-level error (BEFORE-trigger).
- **Idempotent client_order_id** — submitting the same id twice with an active state is refused by `check_idempotent`; the helper returns `("active", order_uid)` and the caller (Phase 3 router) refuses to re-submit.
- **Mandatory broker-to-ledger reconciliation** — `compute_recon()` reads `position_snapshot` and produces `bot_hash`; reads broker positions and produces `broker_hash`; `write_recon_proof()` inserts a row with `match=0` or `1`. The runtime halt on `match=0` lands in Phase 2.
- **Hash chain verified at startup and nightly** — `verify_chain(table)` works; nightly job is the operator's responsibility for now.
- **Single-writer guard** — `connection.acquire_writer_lock()` enforces. Test suite confirms a second writer is refused.
- **Off-host append-only mirror** — every event is mirrored; mirror chain re-verified nightly.

## Deferred items (next phases)

- **Runtime halt routing on recon mismatch** — Phase 2 (risk kernel + halt_router).
- **Daily SHA-256 of closed ledger range signed into policy/HASHES** — Phase 2 alongside the startup hash-check enforcement.
- **Actual order submission** — Phase 3 (execution router).
- **Position snapshot scheduler** (every 5 min) — Phase 5 (kernel daemon).

## Risks & mitigations

- **Risk:** Two writers race the hash chain. **Mitigation:** Single-writer lock + IMMEDIATE transaction wrapping each insert. Tests confirm.
- **Risk:** SQLite cannot enforce true immutability against a privileged local process that opens the file directly. **Mitigation:** Triggers prevent the application path; off-host mirror provides a tamper detector; daily SHA-256 closure signs the closed range; the operator's expectation is honest defense-in-depth, not cryptographic guarantee against rooted-host attackers.
- **Risk:** Canonical JSON formatting drift produces different hashes on different platforms. **Mitigation:** `canonical_json()` uses `sort_keys=True, separators=(",",":"), default=str` — deterministic across Pythons. Tests assert byte-equal output for a fixture row.
- **Risk:** Schema mismatch between code and a long-lived DB. **Mitigation:** `schema.SCHEMA_VERSION` constant; `init_ledger.py` refuses to run against a DB with a different version. Migrations land per-phase alongside new tables.
