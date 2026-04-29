# Options Wheel Strategy — Design

**Date:** 2026-04-28
**Status:** Draft
**Author:** Claude (Opus 4.7) for bharath

## 1. Goal

Add an *intelligent* wheel-strategy lane to the existing trading bot, paper-only, using Alpaca's options API. "Intelligent" means: the bot only enters when conditions favor the wheel (sideways/up regime, decent IV, no looming earnings, name worth owning), sizes by collateral against allocation caps, manages exits with tested rules (50%-of-max-profit or 21-DTE roll), and degrades gracefully when data feeds stall.

This is additive: momentum/mean-reversion lanes keep running on stocks/crypto; the wheel runs on a curated equity sub-universe and competes for the `options` allocation bucket already reserved in `strategy/config.yaml` (15–20% in trending_up / sideways regimes).

## 2. Feasibility — Alpaca Options

**Yes, this is supported on the paper account.**

- **Paper enrollment:** Alpaca paper accounts get Levels 1–3 enabled by default, no API call required. ([source](https://docs.alpaca.markets/changelog/multi-leg-level-3-options-trading-in-paper))
- **What we'll use:** Level 1 only — selling cash-secured puts (CSPs) and selling covered calls (CCs). No multi-leg, no naked calls.
- **SDK:** `alpaca-py` already in `pyproject.toml`. New imports: `alpaca.data.historical.option.OptionHistoricalDataClient`, `alpaca.trading.requests` for option order requests, `alpaca.trading.enums.OrderClass`/`OrderSide`/`PositionIntent`.
- **Contract symbol format:** OCC standard, e.g. `AAPL250117C00190000` (underlying + YYMMDD + C/P + strike×1000 padded to 8 digits).
- **Option chain endpoint:** `GET /v1beta1/options/snapshots/{underlying}` — returns latest quote, IV, and Greeks (delta/gamma/theta/vega/rho) for every contract. The free **indicative** feed has delayed trades and modified quotes; OPRA is paid. Indicative is fine for our wheel cadence (we re-evaluate at most a few times per day). ([source](https://docs.alpaca.markets/reference/optionchain))
- **Assignment behavior:** Auto-exercise/assignment on ITM-by-≥$0.01 at expiry. If the account lacks buying power for assignment, Alpaca sells out the short option within 1 hour before expiry. ([source](https://docs.alpaca.markets/docs/options-trading))
- **Order constraints:** `qty` must be whole-number (1 contract = 100 shares). No `notional`, no `extended_hours`.

## 3. News / Data Audit

### What we already have (inventory)

| Source | What it gives | Wheel-relevance |
|---|---|---|
| Alpaca News API | Per-symbol headlines | Useful: ad-hoc sentiment context |
| Polygon (Massive) | Per-ticker sentiment scores (-1..+1) | **Already wired into `sentiment_floor` gate — reuse for wheel entries** |
| FRED | VIX, 10Y, fed funds | Useful: VIX < 15 = thin premiums; VIX > 30 = avoid CSPs |
| GDELT 2.0 | Macro-news sentiment | Marginal for single-name wheel; keep as macro overlay |
| SEC EDGAR (Form 4) | Insider trades | Tangential |
| Alpaca option chain | IV + Greeks + OI per contract | **Required — primary data source for wheel** |

### Gaps for an intelligent wheel

1. **Earnings dates.** Selling a CSP across an earnings date converts a probabilistic theta trade into a binary gap-risk trade. We must avoid this. Polygon (Massive) has earnings but our current wrapper doesn't expose it. Easiest fix: add a free Finnhub feed (earnings calendar is a free endpoint, 60 calls/min).
2. **IV rank / IV percentile.** We get raw IV per contract from Alpaca, but raw IV is meaningless without a history. Need to compute IV rank ourselves: take ATM 30-day IV, store daily, compute rank over trailing 252 trading days. Cache in SQLite alongside `news_sentiment.db`.
3. **Crowding / social attention.** ApeWisdom (no-auth, free) gives WSB/r/stocks mention counts. Useful as a *negative* filter — sudden social spike on a wheel name means avoid (too easy to get assigned at a bad strike).

### Curated additions (free)

| New source | Endpoint | Used for | Tier |
|---|---|---|---|
| **Finnhub** earnings calendar | `/calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD` | Block CSPs/CCs whose DTE crosses an earnings date | Free, 60 req/min, requires API key (free signup) |
| **Finnhub** company news sentiment | `/news-sentiment?symbol=XYZ` | Cross-check Polygon's sentiment (only for US-listed) | Free, 60 req/min |
| **ApeWisdom** WSB mentions | `/api/v1.0/filter/wallstreetbets` | Crowding filter — refuse wheel entries with sudden mention spikes | Free, no auth, no documented limits ([source](https://apewisdom.io/api/)) |
| **Computed IV rank** (in-house) | from Alpaca chain snapshots | Entry filter (only sell when IV rank > 30) | Local |

I'm explicitly **not** adding Marketaux (100 reqs/day free is too tight — covers neither universe scan nor monitoring) or paid options aggregators (ORATS/MarketChameleon).

## 4. The Intelligent Wheel — Strategy Spec

### 4.1 Phases

```
                    ┌───────────────┐
   no position  →   │  SELL CSP     │  ──┐
                    │  (cash held)  │    │ assigned
                    └───────────────┘    ▼
                          ▲       ┌─────────────┐
                  bought  │       │  HOLD 100   │
                  back at │       │  shares     │
                  ≤50% of │       └─────────────┘
                  max     │              │
                          │              ▼
                          │       ┌─────────────┐
                          └───────│  SELL CC    │
                                  │  on shares  │
                                  └─────────────┘
                                          │
                                  shares called away → back to top
```

### 4.2 Universe (curated, hand-picked, ~20 names)

Wheel candidates must be names you would happily own at the strike. Initial list (overridable in `strategy/wheel_universe.yaml`):

- **Mega-cap tech:** SPY, QQQ, AAPL, MSFT, GOOGL, AMZN, NVDA, META
- **Dividend-payers / defensives:** KO, PG, JNJ, WMT, T, VZ, JPM, BAC
- **Liquid ETFs:** XLK, XLF, XLE, IWM

Selection criteria (documented in the YAML, not hard-coded):
- ≥ $50B market cap OR widely-held ETF
- Average daily option volume > 5,000 contracts (ensures tight bid/ask)
- No active spinoff / merger / delisting

### 4.3 Entry filters (CSP)

A symbol passes only if **all** are true:

| Filter | Threshold | Why |
|---|---|---|
| In wheel universe | Yes | Must be a name we'd own |
| Regime | `trending_up` or `sideways` | Avoid CSPs in `trending_down` / `risk_off` |
| VIX (FRED) | 15 ≤ VIX ≤ 30 | < 15 = thin premiums; > 30 = tail risk |
| Polygon sentiment | ≥ -0.3 (tighter than the global -0.5 momentum floor) | Wheel is collateral-heavy; avoid negative drift |
| Earnings (Finnhub) | No earnings date inside `today + DTE + 2 days` | Avoid binary gap risk |
| IV rank (computed) | ≥ 30 | Don't sell cheap premium |
| WSB mentions (ApeWisdom) | Not in top-50 with >2× day-over-day mention growth | Avoid social-spike traps |
| Existing position | No open CSP/CC/share position on this symbol | One leg at a time |
| Allocation | Total option collateral after this trade ≤ regime cap (`options_max_pct`) | Honor existing config |

### 4.4 Strike & expiry selection (CSP)

- **DTE target:** 30–45 days. Pick the standard monthly that falls in this window (or weekly if no monthly). Source: Tasty Trade research, balances theta decay vs. gamma risk.
- **Delta target:** 0.20–0.30. Walk the chain at the chosen expiry, pick the put with abs(delta) closest to 0.25 *within* the [0.20, 0.30] band. If no contract falls in the band, skip the symbol this cycle.
- **Premium floor:** Bid ≥ $0.20 *and* (credit / collateral) × (365 / DTE) ≥ 0.12. Otherwise skip — not worth the capital lockup.
- **Liquidity:** Bid-ask spread is acceptable if it satisfies *either* (a) ≤ 5% of mid, or (b) ≤ $0.10 absolute. Open interest ≥ 100.

### 4.5 Entry filters (CC after assignment)

When we get assigned and hold 100×N shares of a wheel name:

- **Strike:** ≥ assignment cost basis (never sell a CC below where we got assigned — wheel rule #1).
- **DTE:** 30–45 days, same as CSP.
- **Delta target:** 0.20–0.30.
- **Premium floor:** Bid ≥ $0.20.
- **Skip if:** Earnings within DTE window, regime = `risk_off` (we'd rather get out via stop than cap upside), unrealized loss on shares > 8% (don't lock in losses on a CC strike near cost basis when name is tanking — convert to a "repair" candidate).

### 4.6 Exit / management rules

- **Take profit:** Buy-to-close when option price ≤ 50% of credit received. (Industry standard; locks gains, resets theta clock.)
- **DTE exit:** At 21 DTE, close regardless of P&L if not already closed by 50% rule. (Avoids gamma-acceleration zone.)
- **Roll defensively (CSP):** If the short put delta climbs above 0.45 and DTE > 21, roll to next monthly same delta (close current, open new). Limit: max 2 rolls per cycle, then accept assignment.
- **Roll defensively (CC):** If short call delta > 0.55 and unrealized credit on the call leg < 50% of received (i.e., we're losing on the call), roll up-and-out to next monthly, same 0.20–0.30 delta. Don't roll if it would set strike below cost basis.
- **Assignment:** Always allowed (wheel design). On assignment, the next scheduled wheel run notices the share position and switches the symbol to the CC phase.

### 4.7 Sizing

- **Per-CSP collateral:** `strike × 100 × num_contracts`. Default `num_contracts=1` per cycle per symbol — no pyramiding.
- **Allocation gate:** Sum of all CSP collateral + market value of wheel-assigned shares ≤ `options_max_pct` of equity (currently 15–20% by regime).
- **Per-symbol cap:** ≤ 5% of equity in any one wheel name (matches existing `max_symbol_concentration_pct`).
- **Risk-manager integration:** Reuse `RiskManager` but add `pre_check_option_collateral()`. Daily/weekly loss limits already cover wheel P&L because realized credits/debits show up in `pnl_state`.

### 4.8 Cadence

- **Scan & enter:** Once per trading day at 10:15 ET (after the open settles, before lunch dead zone). New cron job alongside the momentum scan.
- **Manage:** Every 30 min during market hours from 10:30 to 15:30 ET. Re-pulls option snapshots for *open* short positions only, applies 50%/21-DTE/roll rules. (Reuses existing scheduler; new job `wheel_manage`.)
- **Skip-day:** If `state_pause` is set or daemon is in halt, wheel respects the same kill-switch.

## 5. Architecture

### 5.1 New modules

```
src/trading_bot/
├── options/                      # NEW package
│   ├── __init__.py
│   ├── alpaca_options.py         # OptionAlpacaClient — wraps OptionHistoricalDataClient + option order calls on TradingClient
│   ├── chain.py                  # ChainSnapshot dataclass, helpers to find delta-target contract
│   ├── iv_rank.py                # daily ATM IV capture + rank computation; SQLite-backed
│   ├── wheel_state.py            # tracks wheel cycle per symbol (none → csp_open → assigned → cc_open → ...)
│   └── wheel_lane.py             # WheelLane (matches Lane protocol) — generates CSP/CC entry candidates
├── intelligence_finnhub.py       # NEW — Finnhub client (earnings calendar + sentiment)
├── intelligence_apewisdom.py     # NEW — ApeWisdom WSB mentions client
└── orchestrator.py               # MODIFIED — add wheel scan + wheel manage entry points
```

Existing files modified:
- `alpaca_client.py` — add `submit_option_order(...)` (sell-to-open / buy-to-close), `get_option_positions()`. Order-request building stays here so the AlpacaClient remains the single Alpaca surface.
- `risk_manager.py` — add `option_collateral_ok(...)` returning bool + reason.
- `config.py` — add settings for `finnhub_api_key` (env), `wheel_enabled` (bool), `wheel_universe_path`.
- `state_db.py` — add `option_iv_history` table (`symbol, date, atm_iv_30d`) and `wheel_cycles` table (`symbol, phase, entry_credit, entry_strike, entry_expiry, current_contract_id, opened_at`).

### 5.2 Data flow

```
[10:15 ET cron: wheel_scan]
   │
   ├─→ regime.detect() → if risk_off: skip
   ├─→ load wheel_universe.yaml
   ├─→ for each symbol:
   │     pos = alpaca.get_position(symbol)
   │     wheel_state = wheel_state_db.get(symbol)
   │     if pos == None and wheel_state == None:
   │           ── CSP candidate path ──
   │           passes_filters?  (regime, vix, sentiment, earnings, iv_rank, wsb)
   │           if yes: pick contract, size, submit sell-to-open
   │     elif pos.qty == 100*N and wheel_state.phase == "assigned":
   │           ── CC candidate path ──
   │           passes_cc_filters?
   │           if yes: pick contract, submit sell-to-open
   │
   └─→ journal everything

[every 30min cron: wheel_manage]
   │
   ├─→ for each open short option:
   │     fetch snapshot
   │     if buyback_price ≤ 50% of credit:  buy-to-close
   │     elif DTE ≤ 21:                       buy-to-close
   │     elif delta breach + roll allowed:    close + open next monthly
   │
   └─→ on fill: update wheel_state, journal
```

### 5.3 Failure modes

| What fails | What we do |
|---|---|
| Option chain endpoint 429 | Skip this symbol this cycle; log, continue with next |
| Finnhub down / no key | Treat as "earnings unknown" → conservative: skip CSP for that symbol this cycle |
| ApeWisdom down | Skip the crowding filter (it's a tiebreaker, not a hard gate) |
| IV rank history < 30 days | Use IV percentile vs. available history, mark candidate as "low-confidence" — still allow but cap to 1 wheel position until history fills |
| Assignment we didn't expect (gap-down overnight) | Reconciler picks up the share position; next scan advances state to CC phase automatically |
| Option order rejected (e.g., not optionable) | Mark symbol disabled in `wheel_universe.yaml` runtime cache; alert via existing critical-email path |

### 5.4 Tests

- **Unit:** Strike/expiry picker (synthetic chain → expected contract). IV rank from a fixed history. Roll trigger logic. Sizing math against allocation cap.
- **Integration (paper):** End-to-end CSP → wait for fill → verify journal. Mocked Alpaca for CI; live paper for one-off smoke runs.
- **No live-trading test harness** — paper account only, by hard-coded URL prefix check (already present in `AlpacaClient.__init__`).

## 6. Out of scope (intentionally)

- Multi-leg spreads, iron condors, protective puts on existing equities
- Naked calls
- Crypto options (Alpaca doesn't offer)
- Live (non-paper) trading
- Auto-exercising long options (we never go long an option in this design)

## 7. Open questions / decisions made for you

In the spirit of auto mode, here's what I picked without asking:

1. **Universe size kept small (~20 names).** Easier to reason about, and wheel works best on names you'd want assigned. Override: edit `strategy/wheel_universe.yaml`.
2. **Indicative options feed, not OPRA.** Free, sufficient for daily/half-hour cadence.
3. **Finnhub over Marketaux** for earnings + sentiment cross-check (better rate limit, dedicated earnings endpoint).
4. **Defensive parameters** (delta 0.20–0.30, 30–45 DTE, 50% / 21 DTE exit). These are the most-cited published parameters; the existing `evolution.py` framework can tune them later from realized P&L.
5. **No backtest before first paper trade.** A wheel backtest is non-trivial (needs historical option chains we don't pay for). Plan: paper-trade live with the proposed defaults, evaluate after 30–60 days, *then* feed `evolution.py` the closed-trade history to tune.

## 8. Success criteria (12-month rolling, once enough closed cycles exist)

- Wheel-lane Sharpe ≥ 0.8 (lower bar than momentum because lower vol)
- Win rate ≥ 75% of CSP cycles (industry baseline ~80% at 0.30 delta)
- No single wheel cycle losing > 1.5% of equity
- Max 2 assignment events per symbol per quarter (above this = picker is too aggressive)

## Sources

- [Alpaca options trading docs](https://docs.alpaca.markets/docs/options-trading-overview)
- [Alpaca option chain reference](https://docs.alpaca.markets/reference/optionchain)
- [Alpaca multi-leg / Level 3 in paper](https://docs.alpaca.markets/changelog/multi-leg-level-3-options-trading-in-paper)
- [Alpaca options wheel example](https://alpaca.markets/learn/options-wheel-strategy)
- [Finnhub free APIs](https://finnhub.io/docs/api)
- [ApeWisdom API](https://apewisdom.io/api/)
- [FlashAlpha free IV-rank scanner](https://flashalpha.com/articles/iv-rank-scanner-highest-implied-volatility-stocks)
