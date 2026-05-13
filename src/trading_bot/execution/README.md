# `execution/` — L7 Execution Kernel

**Status:** Empty skeleton — populated **Phase 1** (router + fill listener)
and **Phase 3** (Alpaca adapter hardening + cost model integration).

## Mandate (Plan v4 §3, §9)

Translate risk-cleared intents into broker submissions and capture the
resulting fills. **All paths must be idempotent.**

## Modules (lands Phase 1+)

- `order_router.py` — accepts a risk-cleared intent, generates the
  `client_order_id = YYYYMMDD_<strategy>_<symbol>_<seq>`, submits to Alpaca.
  Refuses to re-submit a `client_order_id` already present in `order_master`
  with current-state ∈ {submitted, acked, partially_filled, filled}.
- `fill_listener.py` — streams broker fill events; writes `fill_event` rows
  joined by `order_uid` (not `broker_order_id`).
- `cost_model.py` — pessimistic-lens fill formula from `policy/cost_model.lock`.
- `corporate_actions.py` (Phase 3) — split/dividend/merger normalisation
  cross-checked against a second source.

## Idempotency contract

Any order in state=submitted older than 60 s without ack is queried against
the broker. If found, a new `order_state_event` row back-fills `broker_order_id`
and transitions to=acked. If not found, transitions to=cancelled with
reason='orphan_recovered'.

## What does NOT go here

- LLM calls.
- Risk decisions (gated upstream in `risk/`).
- Strategy logic.
