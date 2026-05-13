# Trading Bot v4 — Phase 3 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-3-design.md`
**Status:** Shipped 2026-05-13 (trading halted; no calendar pressure).

## What landed

### Execution kernel

```
src/trading_bot/execution/
  __init__.py
  cost_model.py        # 3 lenses (raw / broker_paper / pessimistic) for stocks/crypto/options
  order_router.py      # submit_order: precheck → freshness → idempotent → broker
  drift_monitor.py     # 20-trade rolling realised-vs-modelled slippage
  orphan_loop.py       # run_once wraps Phase 1's find_orphans + recover_orphan
```

### Ingest layer (schema + watermarks + corporate actions)

```
src/trading_bot/ingest/
  __init__.py
  schema.py            # DDL for data_watermark + corporate_action
  watermarks.py        # write/read/latest/check_lane_freshness
  corporate_actions.py # record_action + cross_check + apply_split/dividend math
```

### Tests (38 new)

```
tests/test_phase3_cost_model.py
tests/test_phase3_watermarks.py
tests/test_phase3_corporate_actions.py
tests/test_phase3_order_router.py
tests/test_phase3_drift_monitor.py
tests/test_phase3_orphan_loop.py
```

**Combined Phase 0 + 1 + 2 + 3 suite: 211 tests green.**

## P0 / P1 acceptance items satisfied

- ✓ Idempotent client_order_id wired (router consults `ledger.check_idempotent`).
- ✓ Three-lens cost reporting (raw, broker_paper, pessimistic).
- ✓ Live-vs-model drift demote (drift_monitor recommendation).
- ✓ Alt-data freshness watermark (write/read/check shipped; populating from
  live ingest is wired source-by-source as L1.5 ships).

## Deferred to Phase 5+

- Kernel daemon driver loop (Phase 5).
- Wiring orphan_loop / drift_monitor / corporate-actions cross-check into a
  scheduled job (Phase 5).
- Real Alpaca SDK call inside the broker_submit callback (Phase 5).
- Population of watermarks from Alpaca quotes / bars feed (Phase 5).
