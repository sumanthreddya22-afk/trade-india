# Phase 8 — Options / Wheel lane (BLOCKED on operator decision)

This directory is empty by design. The Wheel lane requires:

1. **Paid options data subscription.** Alpaca Options Trading is in private
   beta as of 2026-05; alternatives include Polygon Options, IBKR (with
   different broker), or Tradier. Pick one before writing code — the
   data shape, latency, and Greeks-availability differ enough that
   building against a placeholder is wasted effort.

2. **Risk policy extension.** Today `policy/risk_policy.lock` has
   `options_buying_power_util_max_pct: 30.0` but no greeks limits,
   no max assigned-stock notional, no expiration-week blackout policy.
   These need real numerics from the operator before code is useful.

3. **Tax-lot policy** (`docs/runbooks/tax_lot_policy.md`) — wheel assignments
   create cost-basis events. Operator must decide FIFO vs specific-lot
   identification and document the choice for the broker.

## What lands here when unblocked

- `wheel_strategy.py` — wheel state machine: cash-secured put → assigned
  → covered call → called away → repeat.
- `greeks.py` — delta/theta calc using BSM with broker-supplied IV.
- `assignment_handler.py` — converts an option assignment into a
  position_snapshot row and a position_classifier reclassification.
- `expiration_calendar.py` — third-Friday detector + blackout enforcement.

## Hard rule

No code in this directory may import from `execution/order_router.py`
until the three decisions above are signed off in
`docs/runbooks/options_signoff.md`.
