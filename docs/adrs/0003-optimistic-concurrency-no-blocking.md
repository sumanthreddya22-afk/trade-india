# ADR 0003: Optimistic concurrency, no blocking flows

- **Date:** 2026-05-02
- **Status:** accepted
- **Pipelines affected:** shared, stocks, crypto, options
- **Author:** operator

## Context

Multi-asset, multi-pipeline bot may have many debates in flight at once
(express stream events, regular cadence scans, parallel asset-class
ingestion). Two extreme designs:

- Strict serial queue + per-symbol locks: zero conflicts, head-of-line
  blocking.
- Fully parallel with no coordination: maximum throughput, race
  conditions on cash / positions / same-symbol verdicts.

The user explicitly required "no flow should block another flow."

## Decision

Optimistic concurrency. All debate work runs fully in parallel — no
mutex, no queue. The single synchronization point is the order-submit
transaction in `shared/submit_txn.py`:

```
BEGIN
  re-read current position for symbol (or contract for options)
  re-read latest verdict for symbol from debate tables
  if a newer verdict (by trigger_event_at) exists → ABORT, log "superseded"
  re-read current cash / buying power
  if insufficient → ABORT, log "outpriced by parallel order"
  submit to Alpaca with client_order_id (UUID)
COMMIT
```

Verdicts are timestamped by their *trigger event*, not completion time
— so out-of-order debate completion does not produce out-of-order
actions. The broker (Alpaca) is the source of truth for cash and
position state; broker errors (`insufficient_buying_power`, `no
position`, duplicate `client_order_id`) are caught and logged, never
retried mechanically.

Lock keys are asset-class-aware:

- Stocks / crypto: `{asset_class}:{symbol}`
- Options: `{asset_class}:{underlying}:{contract_id}` — different
  contracts on the same underlying are independent positions.

## Consequences

- Zero head-of-line blocking. A 10-second debate on AAPL never delays
  a debate on ETH.
- Occasional wasted LLM work when two debates fire on the same symbol
  near-simultaneously (the second's submit aborts as superseded).
  Cost: ~$0.05 per wasted debate; rare in practice.
- Simpler than queue + lock design — fewer moving parts to debug.
- Trust placed in Alpaca's broker-side serialization for cash safety.
  If broker behaves unexpectedly, bot logs and aborts cleanly rather
  than retrying.

## Alternatives considered

- Per-symbol mutex with queue: rejected because head-of-line blocking
  violates the user's "no flow blocks another" requirement.
- Pessimistic locking with broker reservations: not supported by Alpaca.
- Eventual-consistency with reconciliation: too much complexity for
  bot's volume.
