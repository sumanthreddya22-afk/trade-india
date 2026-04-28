# Naked Position Auto-Protect — Design

**Status:** approved, ready for implementation plan
**Date:** 2026-04-28
**Author:** brainstorm session with Bharath

## Goal

Today the `verify-stops` sweep alerts the user by email when an open position lacks a live stop order. Bharath wants this changed in two ways:

1. The alert email's subject line should read `Open Positions — N unprotected` instead of `⚠ NAKED POSITION ALERT — N unprotected`.
2. The sweep should stop being alert-only and instead **act**: protect the position with a stop, or close it, automatically. Email becomes a **summary of actions taken**, not a request for manual intervention.

## Trigger context

- The sweep runs every `:20` and `:50` of every hour, 24/7, from `bot verify-stops` ([`cli.py:767`](../../src/trading_bot/cli.py)).
- A position is "naked" when no live stop / stop-limit order references it.
- Once a stop is placed (or position is closed), subsequent sweeps see no naked positions and take no further action — so the cadence does not cause repeated emails.

## Decision rule per naked position

For each naked symbol, fetch 60d daily bars, compute indicators (`rsi_14`, `macd`, `macd_signal`, `ema_20`, `last_close`, `return_5d`) using the existing pipeline (`MarketDataClient.get_daily_bars` + `compute_indicators` from `market_data.py`).

Compute the **strategy-aligned protective stop**:

```
protective_stop = max(ema_20, last_close * (1 - naked_recovery_stop_pct))
```

This mirrors `MomentumStrategy.evaluate` ([`strategy.py:77`](../../src/trading_bot/strategy.py:77)) but is computed unconditionally (we don't gate on entry conditions; we only need the stop level).

Then branch:

- **`protective_stop < current_price` → "performing well":**
  Submit a protective stop. Stocks → plain `StopOrderRequest`. Crypto → `StopLimitOrderRequest` (Alpaca rejects plain stops on crypto), reusing the trigger/limit math from [`alpaca_client.py:289-303`](../../src/trading_bot/alpaca_client.py:289).
  - `qty = position.qty`
  - `side = opposite(position.side)` (long → SELL stop, short → BUY stop)
  - `time_in_force = GTC`

- **`protective_stop >= current_price` → "performing badly":**
  Submit a market order to flatten:
  - Stocks: `MarketOrderRequest(symbol, qty, side=opposite, time_in_force=DAY)`
  - Crypto: same with `time_in_force=GTC`

This collapses Bharath's "good vs bad" framing into one comparison: if the price has already broken below both the EMA-20 and the % floor, the position has earned a flatten. Otherwise it has earned protection.

## Asset-class gating

- **Stocks:** act only inside US regular trading hours (09:30–16:00 ET, Mon–Fri). Reuse `_is_market_hours_et()` from [`supervisor.py:43`](../../src/trading_bot/supervisor.py:43). Outside RTH, skip stock symbols entirely (do not place stops either, to keep behavior consistent and avoid GTC stops resting overnight on positions we just decided to evaluate next session).
- **Crypto:** act 24/7.

Asset class is read from `position.asset_class` (already on the Alpaca position object).

If a naked stock is encountered outside RTH, log it and include it in the email under a "Deferred to next session" section so the user knows nothing was actioned.

## Configuration

Add to `strategy/config.yaml`:

```yaml
risk:
  # ... existing keys ...
  naked_recovery_stop_pct: 0.05   # 5% default; matches MomentumStrategy default
```

And the corresponding pydantic field on `RiskConfig` in `config.py`. Default 0.05 if unset.

Single knob applies to both stocks and crypto; revisit if behavior diverges in practice.

## Email

Builder: replace `build_naked_stops_email_html` with `build_open_positions_email_html(actions, total_positions, deferred)`.

`actions` is a list of records, each containing:

- `symbol`
- `outcome`: `"stop_placed"` | `"flattened"` | `"failed"` | `"deferred_off_hours"`
- `qty`, `side`, `asset_class`
- For `stop_placed`: `stop_price`, `current_price`
- For `flattened`: `fill_estimate` (last_close at decision time — actual fill price not known yet)
- For `failed`: `error` (string)
- For `deferred_off_hours`: nothing extra

Subject:

- If any `failed` or `deferred_off_hours`: `Open Positions — N actioned, M need attention`
- Otherwise: `Open Positions — N actioned`
- If nothing was naked: **no email** (current behavior).

Body sections (only render those that have rows):

1. KPI cards: `Total Open`, `Stops Placed`, `Closed`, `Failed`.
2. **Protected** table: symbol, qty, side, current price, stop placed at, distance %.
3. **Closed** table: symbol, qty, side, last price.
4. **Failed** table: symbol, attempted action, error message — explicitly flagged as **needs manual review**.
5. **Deferred (off-hours)** table: symbol, qty, side — will be re-evaluated at the next sweep during RTH.

This replaces the existing red "naked positions" warning intro with a summary line at the top. The visual styling (colors, KPI grid, pills) reuses the helpers already in [`reports.py`](../../src/trading_bot/reports.py).

## Failure handling

If the Alpaca API rejects the stop or market order (rate limit, validation, transient error), catch the exception, record it as `outcome="failed"` with the error string, and continue to the next position. The email's "Failed" section becomes the manual-intervention prompt — same role the old alert email played, but only for positions where automation actually failed.

If `MarketDataClient.get_daily_bars` fails for a symbol (e.g., delisted ticker, API outage), record as `failed` with reason, no action taken.

If `_is_market_hours_et()` is false and the position is a stock, record as `deferred_off_hours`, no API calls made for that symbol.

## Module layout

New file: `src/trading_bot/naked_recovery.py` containing:

- `@dataclass NakedAction` — the per-symbol record described above.
- `def evaluate_and_act(client: AlpacaClient, market_data: MarketDataClient, naked_positions: list[NakedPosition], *, stop_pct: Decimal, now_in_market_hours: bool) -> list[NakedAction]` — pure orchestrator; takes injected clients so tests don't hit Alpaca.
- `def _decide(symbol, current_price, ema_20, stop_pct) -> Literal["protect", "flatten"]` — pure function, easy to unit-test.

`cli.py:verify_stops` shrinks to:

1. Pull positions and open orders (existing).
2. Filter to naked (existing logic, factored into a helper).
3. If naked is empty → return.
4. Build inputs, call `evaluate_and_act`.
5. Build email via `build_open_positions_email_html`, send.

The logging surface (`[verify-stops] positions=… stops=… naked=…`) keeps emitting per-position outcomes so the daemon log stays useful.

## Tests

In `tests/`, mirror existing patterns:

- `tests/test_naked_recovery.py`:
  - `_decide` returns `"protect"` when `protective_stop < current_price`, `"flatten"` otherwise (boundary case at equality goes to flatten).
  - `evaluate_and_act` with a mocked client/market-data:
    - Long stock, healthy → places `StopOrderRequest`, `qty=position.qty`, `side=SELL`, `stop_price` matches formula.
    - Long stock, broken → places `MarketOrderRequest(side=SELL)`, no stop placed.
    - Crypto, healthy → places `StopLimitOrderRequest` with the existing trigger/limit buffer math.
    - Stock encountered with `now_in_market_hours=False` → outcome `deferred_off_hours`, no API calls.
    - Crypto encountered with `now_in_market_hours=False` → still acts.
    - Alpaca raises on order submit → outcome `failed`, exception captured, loop continues.
    - `get_daily_bars` raises → outcome `failed`, no order submitted.
- `tests/test_email_open_positions.py`:
  - Subject reflects counts and the `actioned/need attention` split.
  - Each section renders only when it has rows.
  - Empty `actions` → no email built (or whatever `cli.verify_stops` does — covered there).

## Out of scope

- **No replay through `strategy.evaluate`'s BUY/HOLD gate.** That gate is built for fresh entries; reusing it as an exit rule would close winners that no longer meet entry criteria. The stop-price formula alone is what we want.
- **No regime-aware stop logic.** A single `naked_recovery_stop_pct` knob; revisit later if needed.
- **No partial position handling.** Whole position is protected or flattened.
- **No re-entry logic.** Closing a naked position does not queue a re-entry; that's the orchestrator's job on its normal cadence.
- **No state persistence across sweeps.** Each sweep is self-contained. If a market order fills and the next sweep sees the position gone, that's fine.

## Open risks

1. **Whipsaw on borderline positions.** A stock that's hovering right around EMA-20 could be flattened on a sweep that runs at a temporary dip. Mitigation: the EMA-20 is daily, so intraday noise doesn't cross it often; and once the position is closed, that's it for the day.
2. **Stop placed at level immediately triggered.** The `protective_stop < current_price` check guarantees the stop is below current — but if the market drops between price-fetch and order-submit, the stop could trigger immediately. Acceptable: that's the same outcome as flattening, just via stop fill.
3. **Crypto stop_limit slippage.** The existing `CRYPTO_STOP_LIMIT_BUFFER_PCT` math is reused — same risk profile as today's crypto entry path.
4. **Off-hours-naked stock that gaps overnight.** If a stock goes naked at 16:30 ET on a Friday and gaps down 8% on Monday's open, we still won't act until 09:30 Mon. Acceptable: today's behavior is also "do nothing automatically", and the daily digest will surface the loss.
