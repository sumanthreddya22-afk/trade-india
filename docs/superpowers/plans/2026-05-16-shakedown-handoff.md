# Shakedown handoff — what landed this session, what's still pending

> Companion to `/Users/bharathkandala/.claude/plans/review-the-users-bharathkandala-claude-p-proud-barto.md`
> (the full shakedown plan). This file lists what's complete in code,
> what the operator must do on the main repo, and what's still owed.

## ✅ Landed in code (worktree `claude/pedantic-keller-f5bed2`)

### WS4 — policy lock recalibration (1-2 days planned)
- [`policy/risk_policy.lock`](policy/risk_policy.lock) v3-live-shakedown
  - `daily_drawdown` 1% → 5%, `per_lane_daily_loss` 0.5% → 2.5%,
    `per_order_at_risk` 2% → 10% (loosens — 7-day cooldown)
  - `unknown_position_max_age_minutes` 15 → 5 (tightens — immediate)
- [`policy/live_capital.lock`](policy/live_capital.lock) v1-live-shakedown
  - `total_equity_ceiling` $1500, `max_drawdown_halt` 15%
  - Per-strategy caps: ETF_v3 $400, DUAL_v3 $400, CRYPTO_v3 $200, SPY_v3 $0
  - `live_mode_expiry_iso` pushed to 2026-07-15 (was 2026-06-14)
- [`policy/validation_policy.lock`](policy/validation_policy.lock) v5
  - `paper_candidate.min_paper_obs_days` 60 → 14
  - `live_candidate.min_paper_obs_days_per_rebalance_period.daily` 60 → 14
- [`policy/cost_model.lock`](policy/cost_model.lock) v3-webull
  - Added `options.regulatory_fees.orf_per_contract_usd` ($0.02925)
  - Verified crypto taker 30bps against Webull schedule
  - `live_vs_model_drift.tolerance_multiplier` 2.0 → 1.5
- [`policy/event_blackout.lock`](policy/event_blackout.lock) v1 NEW
  - Source list (FRED, BLS, Treasury, EDGAR, OPRA, CME), corroboration
    policy, action matrix per event class.
- [`policy/regime_protocols_v1.json`](policy/regime_protocols_v1.json) v2
  - Lock version bumped (re-sign for shakedown). Content unchanged —
    the 5-regime classifier already had concrete thresholds.
- [`policy/HASHES`](policy/HASHES) regenerated; verifier passes.
- [`src/trading_bot/execution/drift_monitor.py`](src/trading_bot/execution/drift_monitor.py)
  `TOLERANCE_MULTIPLIER_DEFAULT` 2.0 → 1.5

### WS3 — BrokerAdapter ABC + WebullAdapter (5-7 days planned)
- [`src/trading_bot/ingest/broker_adapter.py`](src/trading_bot/ingest/broker_adapter.py)
  NEW — 7-method ABC.
- [`src/trading_bot/ingest/alpaca_adapter.py`](src/trading_bot/ingest/alpaca_adapter.py)
  now subclasses `BrokerAdapter` (zero behaviour change).
- [`src/trading_bot/ingest/webull_adapter.py`](src/trading_bot/ingest/webull_adapter.py)
  NEW — HMAC-signed, 401-refresh-retry, token-bucket throttle,
  shadow-mode default (ENABLE_SUBMIT=false).
- [`src/trading_bot/cli.py`](src/trading_bot/cli.py) reads `BROKER` env;
  alpaca | webull.
- [`.env.example`](.env.example) extended with Webull + ENABLE_SUBMIT.
- [`tests/test_webull_adapter.py`](tests/test_webull_adapter.py) 10 unit
  tests (normalization + session refresh + crypto IOC + shadow mode +
  ABC compliance).
- [`tests/test_webull_adapter_live.py`](tests/test_webull_adapter_live.py)
  gated $1 SIRI live-smoke test (WEBULL_LIVE_SMOKE=1).

### WS5a — order-router retry-with-backoff (5-7 days planned for WS5 in total)
- [`src/trading_bot/execution/order_router.py`](src/trading_bot/execution/order_router.py)
  classifies broker errors transient vs permanent; transient → 3 retries
  with 0.5/1.0/2.0s backoff; permanent → cancel immediately.
- [`tests/test_order_router_retry.py`](tests/test_order_router_retry.py)
  7 unit tests.

### WS6a — broker_switch_event + recon kill-switch suppression (2-3 days)
- [`src/trading_bot/ledger/schema.py`](src/trading_bot/ledger/schema.py)
  new `broker_switch_event` table (hash-chained, append-only).
- [`src/trading_bot/ledger/broker_switch_event.py`](src/trading_bot/ledger/broker_switch_event.py)
  NEW — write_event + most_recent_switch_within(24h).
- [`src/trading_bot/risk/kill_switches.py`](src/trading_bot/risk/kill_switches.py)
  `detect_recon_mismatch` accepts `recent_broker_switch` to suppress
  the kill for 24h post-cutover.
- [`tests/test_broker_switch_event.py`](tests/test_broker_switch_event.py)
  5 unit tests (hash chain, append-only triggers, suppression).

### Test results
- 640 passed, 2 skipped (gated live-smoke), 2 pre-existing failures
  in `test_phase_a_v3_strategies.py` (universe fallback — unrelated to
  this session's changes; reproduces on the base branch).

---

## ⚠ Operator action items (main repo, not worktree)

### WS1 — boot the daemon on Alpaca paper TODAY
The main repo (`/Users/bharathkandala/Trading`) is already bootstrapped
— `.env` populated with Alpaca paper creds, `data/ledger/ledger.db`
initialised, strategies registered, ETF/DUAL/CRYPTO/SPY v3 already at
`tiny_paper`. The only outstanding WS1 ops are:

1. **Audit external positions before booting** — `position_snapshot`
   contains 13 crypto positions + 1 stock (`ARM`) classified as
   `external`. Per CLAUDE.md hard rule #3 (no new entries while crypto
   > 15% equity), confirm crypto exposure is within cap before letting
   `mutation_cycle` enable any crypto trades. If exposure is over,
   sell down through the Alpaca dashboard before booting the daemon.

2. **Install launchd plist** — copy
   `daemon/launchd/com.tradingbot.local.daemon.plist`
   to `~/Library/LaunchAgents/`, substitute `__USER__` +
   `__PYTHON_BIN__`, then `launchctl load`.

3. **Smoke** — `bot daemon --once`; confirm watermarks land and no
   exceptions. Then check `/?view=operator` cockpit at next 15:30 ET
   tick for the first `order_master` row.

### WS4 — re-sign locks AFTER merging the worktree
The locks landed in this worktree but are **not yet hash-anchored on
main**. After merging:

```sh
git checkout main
git merge --no-ff claude/pedantic-keller-f5bed2
uv run python tools/recompute_hashes.py
uv run python tools/recompute_hashes.py --check       # must exit 0
git add policy/HASHES
git commit -S -m "policy: anchor 2026-05-25 shakedown locks"
```

Then the 7-day loosen cooldown elapses 2026-06-01 — leaving an 11-day
buffer before the 2026-06-12 cutover.

### WS3 — Webull pre-cutover ops
- Apply for Webull Developer Portal access if not done. Plan WS3
  references `developer.webull.com/apis/docs/trade-api/*`.
- Verify crypto availability in Webull test mode **before 2026-05-25**.
  If unavailable, re-sign `live_capital.lock` with
  `CRYPTO_MOMENTUM_v3 = 0` and reallocate $200 to ETF + DUAL.
- Verify real-time L1 quote subscription (delayed quotes still pass
  data_freshness gate at ≤300s for equity but may surprise the drift
  monitor).
- Once creds in `.env`, run shadow week from 2026-05-25 with
  `BROKER=webull ENABLE_SUBMIT=false`.
- $1 SIRI smoke: `WEBULL_LIVE_SMOKE=1 ENABLE_SUBMIT=true uv run pytest
  tests/test_webull_adapter_live.py -v`.

### WS6 — at cutover (2026-06-12 16:30 ET)
Before flipping `BROKER=webull` in `.env`:

1. **List pre-existing Webull positions**, add their `client_order_id`
   prefixes (or symbol-based safe-list) to `DEFAULT_LEGACY_PREFIXES`
   in `position_classifier.py`. Otherwise the
   `unknown_position_max_age_minutes=5` kill switch fires within 5 min.
2. **Record the cutover**:
   ```python
   from trading_bot.ledger.broker_switch_event import write_event
   import sqlite3
   conn = sqlite3.connect("data/ledger/ledger.db")
   write_event(conn, from_broker="alpaca", to_broker="webull",
               operator="bharath@local", reason="2026-06-12 cutover")
   conn.commit()
   ```
   This is what suppresses the first night's `recon_mismatch`.
3. Flip `.env`: `BROKER=webull`, `BOT_MODE=live`, `ENABLE_SUBMIT=true`.
4. Re-sign `live_capital.lock` with `live_capital_enabled=true`, then
   `tools/recompute_hashes.py` + commit.
5. Restart daemon (`launchctl unload && launchctl load`).
6. Watch the cockpit for first live `order_master` row at next
   `strategy_runner` tick.

---

## 📋 Still owed (NOT done this session)

### WS2 — vectorbt backtest wired into mutation cycle (3-4 days)
- Add `vectorbt>=0.26` to `pyproject.toml`.
- New `src/trading_bot/research/backtest_vectorbt.py` with
  `vectorbt_backtest(candidate) -> (p_value, sanity_checks)`.
- Wire `job_mutation_cycle` in `src/trading_bot/daemon/jobs.py` to pass
  this callable to `run_cycle` (currently passes a stub).
- Verify with `bot run-mutation-cycle --once --dry-run`.

### WS5b — `broker_api_error_rate` end-to-end
- Detector exists at `src/trading_bot/risk/kill_switches.py:282-299`
  as pure logic.
- Need call-site instrumentation on every broker method (success/error
  counter), `job_broker_error_rate_check` scheduled every 1 min with a
  5-min rolling window.

### WS5c — `bot strategy sign-packet` CLI
- `promotion_packet.operator_signed` defaults to 0; no command flips it.
- Add to `src/trading_bot/operator/controls.py`; surface as a cockpit
  button.

### WS5d — 4 P&L tripwires
- Realized-loss / live-vs-model drift / execution-quality / behavioral
  tripwires with two-tier alert/halt severity.
- New `alert_event` table (or `severity` column on `kill_switch_event`).

### WS5e — Position classifier legacy-prefix backfill
- Pre-cutover op (see WS6 step 1 above).

### WS5f — 7-layer safety stack
- `src/trading_bot/risk/regime_classifier.py` (composite of 6 signals).
- `src/trading_bot/ingest/event_calendar.py` (6-source puller).
- `src/trading_bot/ledger/manual_halt_event.py` (PAUSE/FLATTEN audit).
- `src/trading_bot/operator/cockpit_safety.py` (PAUSE/FLATTEN UI + daily card).

These are substantial — each is its own session.
