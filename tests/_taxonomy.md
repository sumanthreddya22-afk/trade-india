# Test taxonomy — required test categories per pipeline

Every pipeline (`pipelines/stocks/`, `pipelines/crypto/`,
`pipelines/options/`) must implement tests in each category below. The
CI lint walks each pipeline's `tests/` directory and warns when a
required category is missing.

The point: drift across pipelines should be visible in test gaps. If
crypto has a stale-state-abort test and stocks does not, the CI lint
flags it on the next PR touching stocks.

## Required categories per pipeline

### 1. Happy-path

End-to-end scenario where every gate passes and the trade flows from
signal to filled order. Exercises: source ingestion → roll-up → scout
elevation → entry debate `place` → submit-txn → broker ack → trade
journal row.

File pattern: `tests/test_<pipeline>_happy_path.py`

### 2. Fail-soft on LLM error

Simulates an LLM transport failure (subprocess timeout, malformed JSON,
`SubscriptionRateLimited`). Bot must:

- NOT crash
- Emit a `SkipVerdict("rate_limited" or "transport_error", ...)`
- Fall through to deterministic gates only
- Log + alert
- Existing positions and pending orders untouched

File pattern: `tests/test_<pipeline>_fail_soft_llm.py`

### 3. Stale-state abort

Two debates on the same symbol fire near-simultaneously. The first
verdict's submit-txn must abort once it sees the second verdict's
newer `trigger_event_at`. The second verdict's submit succeeds.

File pattern: `tests/test_<pipeline>_stale_verdict_abort.py`

### 4. Broker-reject handling

Mocks Alpaca returning each of: `insufficient_buying_power`,
`no_such_position`, duplicate `client_order_id`. Bot must:

- Log the rejection (cleanly, no retry)
- Mark the verdict as failed with the broker reason
- Continue scanning other symbols
- Send operator alert if rejection rate spikes above threshold

File pattern: `tests/test_<pipeline>_broker_reject.py`

### 5. Persona inventory

Asserts every LLM-using point in this pipeline has a persona file
under `pipelines/<pipeline>/personas/` with the required `PERSONA`
dict (id, full_name, role_title, years_experience, firm_pedigree,
prompt_template). Catches "I added a new debate but forgot the
persona file."

File pattern: `tests/test_<pipeline>_persona_inventory.py`

### 6. Migration round-trip

For each migration in `pipelines/<pipeline>/migrations/`:

- `upgrade()` runs cleanly on an empty DB
- `downgrade()` reverses cleanly
- After upgrade + downgrade, schema matches pre-state

File pattern: `tests/test_<pipeline>_migrations.py`

## Cross-cutting (in `tests/`, not per-pipeline)

- `test_shared_llm_transport.py` — transport correctness, model routing,
  rate-limit graceful skip, batching, verdict cache hit path
- `test_shared_submit_txn.py` — optimistic concurrency, supersession,
  broker error handling, client_order_id idempotency
- `test_cross_pollination.py` — pre-commit hook, drift detector, CI lint
- `test_full_three_pipeline.py` — end-to-end stress with concurrent events
  across all three pipelines

## How CI lint enforces this

`shared/automation/ci_lint.py` walks each `pipelines/<pipeline>/tests/`
directory at PR time, checks for the file patterns above, and warns
on missing categories. Warnings, not errors — drift may be intentional
(an ADR explains why) but should always be visible.
