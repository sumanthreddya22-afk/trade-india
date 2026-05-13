# Trading Bot v4 — Phase 3 Design (Cost Model + Adapter Hardening + Corporate Actions + Stale-Data)

**Source plan:** Plan v4 §9 (Reality / Execution Model) + §5 idempotency contract + §6 freshness + §15 ("Best-effort write paths to broker state — Remove after Phase 1").

**Phase duration in plan:** 1 calendar week. **Trading remains halted.**

## Goal

Ship the **L7 execution kernel** — the layer that translates risk-cleared
intents into broker submissions and captures the results. Three sub-systems
land here:

1. **Pessimistic cost model.** Used by backtests (Phase 5) AND by the
   live-vs-model drift comparator. The lock numerics already shipped in
   `policy/cost_model.lock` (Phase 2); Phase 3 ships the math that reads
   the lock and applies it.

2. **Hardened Alpaca submission path.** Idempotent client_order_id (uses
   Phase 1's `ledger.check_idempotent`); refuses to submit when the
   relevant data watermark is stale; refuses to submit when `risk.precheck`
   returns halt; orphan-recovery loop (uses Phase 1's
   `ledger.find_orphans`).

3. **Corporate actions + data watermarks.** Watermarks table per
   `(source_id, lane)` so the kernel can ask "is this lane's data fresh
   enough to act?". Corporate-action ingest from a primary + secondary
   source with mismatch-halt; lane halts on adjustment-series divergence.

## Components shipped

```
src/trading_bot/execution/
  __init__.py
  README.md                    # already shipped Phase 0
  cost_model.py                # 3 reporting lenses + fees + slippage
  order_router.py              # submit_order: precheck + freshness + idempotent + submit
  fill_listener.py             # apply broker fill events to ledger.append_fill_event
  drift_monitor.py             # 20-trade rolling live-vs-model; demotion recommendation
  orphan_loop.py               # periodic find_orphans + recover_orphan caller

src/trading_bot/ingest/
  __init__.py
  README.md                    # already shipped Phase 0
  schema.py                    # DDL for data_watermark + corporate_action
  watermarks.py                # write_watermark / read_watermark / check_lane_freshness
  corporate_actions.py         # apply_action + cross_check + ingest_alpaca_actions

tools/
  cost_lens_report.py          # CLI that prints all 3 lenses for a given trade

tests/
  test_phase3_cost_model_stocks.py
  test_phase3_cost_model_crypto.py
  test_phase3_cost_model_options.py
  test_phase3_watermarks.py
  test_phase3_corporate_actions.py
  test_phase3_order_router.py
  test_phase3_drift_monitor.py
  test_phase3_orphan_loop.py
```

## Cost model — the three lenses

Each lens is a pure function `(intent, fill_qty, fill_price, lock) → FillCost`:

| Lens | Formula | Used by |
|---|---|---|
| `raw` | mid-to-mid; no spread, no slip, no fees | Diagnostic only |
| `broker_paper` | models Alpaca's optimistic paper-trading behaviour | Live-vs-model baseline comparison |
| `pessimistic` | Plan §9 formula (midpoint-relative + extra slip + fees) | **THE GATE.** All validation_policy thresholds compute against this. |

**Stocks (per Plan §9):**

```
half_spread  = (ask - bid) / 2
extra_slip   = mid * (extra_slippage_bps / 10000)
buy_fill     = mid + half_spread + extra_slip + broker_fees_per_share
sell_fill    = mid - half_spread - extra_slip - broker_fees_per_share
               - sec_section_31_fee     (sells only)
               - finra_taf_fee          (sells only, capped)
```

**Crypto:**

```
buy_fill   = mid * (1 + taker_bps/10000) + slip
sell_fill  = mid * (1 - taker_bps/10000) - slip
```

**Options (per contract, multiplier 100):**

```
buy_fill   = mid + half_spread + extra_slip + per_contract_fee
sell_fill  = mid - half_spread - extra_slip - per_contract_fee
```

## Order router — submission contract

```python
order_router.submit_order(
    conn, intent, account, positions, policy, lane,
    quote_ts: datetime,                # the watermark we evaluate freshness against
    broker_submit: Callable[..., dict], # caller supplies the broker call (mockable)
    intent_price: float,
    stop_loss_price: float | None = None,
) -> SubmissionResult
```

Sequence:

1. **Risk precheck.** Calls `risk.precheck.evaluate(...)`. If `halt`, writes a
   `strategy_decision` row with `risk_decision='halt'` and returns
   `SubmissionResult(submitted=False, reason=...)`.

2. **Freshness check.** If `quote_ts` is older than the lane's threshold in
   `policy/data_freshness.lock`, refuses to submit; writes `strategy_decision`
   with `risk_reason='data_freshness:stale'`.

3. **Idempotency check.** Calls `ledger.check_idempotent(client_order_id)`. If
   status is `active`, refuses to submit (already in flight). If `terminal`,
   the caller must generate a new CID — returns `SubmissionResult(submitted=False)`.

4. **Insert order_master.** Writes the master row and an `intent` state event.

5. **Submit to broker.** Calls `broker_submit(intent_dict)`. On success, writes
   `submitted` state event with `broker_order_id`. On failure, writes
   `cancelled` state event with the failure reason.

6. **Write strategy_decision.** Always.

The function is fully unit-testable: the broker submit callback is injected,
so we can test happy path, rejection, error path, and orphan-recovery
without touching Alpaca.

## Drift monitor

Reads `fill_event` rows for the past 20 closed trades per lane. Compares
realised slippage (filled price vs decision-time mid) to the pessimistic-lens
prediction. If 20-trade mean realised > 2× modelled, emits a demotion
recommendation: `lane → observe_only` via `risk.lane_caps.demote_on_breach`.

For Phase 3 we ship the comparator + the recommendation; the live-job
scheduler lands in Phase 5 alongside the kernel daemon.

## Data watermarks

New table in `ledger.db`:

```sql
CREATE TABLE data_watermark (
    source_id        TEXT NOT NULL,
    lane             TEXT NOT NULL,      -- equity | crypto | option
    last_event_ts    TEXT NOT NULL,
    last_ingest_ts   TEXT NOT NULL,
    raw_payload_hash TEXT,
    PRIMARY KEY (source_id, lane)
);
```

Note: this table is **NOT** hash-chained or append-only. Watermarks are
mutable by design — every fresh quote updates the latest watermark.
The `data_watermark` table is the only mutable surface in the ledger DB;
it lives alongside the hash-chained event tables so the kernel can read it
under the same single-writer guard.

Functions in `ingest/watermarks.py`:

- `ensure_watermark_table(conn)` — DDL on first use.
- `write_watermark(conn, source_id, lane, event_ts, ingest_ts, payload_hash)` — upsert.
- `read_watermark(conn, source_id, lane) -> Optional[Watermark]`.
- `check_lane_freshness(conn, lane, lock, now) -> RiskDecision` — used by router.

## Corporate actions

New append-only table:

```sql
CREATE TABLE corporate_action (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    action_type     TEXT NOT NULL,        -- split | dividend | merger | spinoff
    ex_date         TEXT NOT NULL,
    factor          REAL,                 -- split ratio or dividend amount
    source_id       TEXT NOT NULL,        -- alpaca | yfinance | etc.
    raw_payload_hash TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL,
    UNIQUE (symbol, action_type, ex_date, source_id)
);
```

Hash-chained like the event tables.

Functions in `ingest/corporate_actions.py`:

- `record_action(conn, action)` — append one.
- `cross_check(symbol, ex_date) -> CrossCheckResult` — reads primary + secondary
  source rows for the same (symbol, ex_date); reports match / mismatch /
  missing.
- `apply_split_to_qty(qty, factor)` / `apply_dividend_to_cash(...)` —
  pure-math helpers for backtest + reconciliation use.

## P0 / P1 acceptance items satisfied

P0:

- **Idempotent client_order_id** — Phase 1 added the helper; Phase 3 wires
  the router to consult it.
- **Crypto exposure cap enforced at submit time** — `order_router.submit_order`
  calls `risk.precheck.evaluate` before every submission.

P1:

- **Three-lens cost reporting** — `cost_model.{raw, broker_paper, pessimistic}`
  shipped with tests; backtest runner consumes in Phase 5.
- **Live-vs-model drift demote** — `drift_monitor.evaluate_lane` recommends
  observe-only when 20-trade slippage exceeds 2× model.
- **Alt-data freshness watermark** — `ingest/watermarks` shipped; populating
  it for each ingest source lands in Phase 1.5 (alt-data) and Phase 3 itself
  for `alpaca_bars / quotes / corporate_actions`.

## Deferred to Phase 5+

- Kernel daemon driver loop (Phase 5).
- Population of watermarks from a live Alpaca SDK feed (Phase 5; the writer
  is ready, just unwired).
- BH-FDR mutation accounting (Phase 6).
- Promotion packet artifact emission (Phase 4).
