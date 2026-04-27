# Plan 6 — Massive-first architecture (full-market participation)

**Status:** Design
**Date:** 2026-04-26
**Supersedes:** the data-layer assumptions in [2026-04-26-revised-plan-sequence.md](2026-04-26-revised-plan-sequence.md). Plan numbers 5d/5e/5f stay the same; this plan changes how they're fed.

## TL;DR

Today the bot's universe and signal data come from **Alpaca paper**, which has three structural problems: (1) sandbox-level crypto volume that hides real liquidity, (2) per-ticker bar fetches that don't scale beyond a few hundred names, and (3) zero non-price data (no news, no sentiment, no fundamentals). The trader's analysis kept calling out "you're trading the most-watched names with no edge" — that's a direct consequence of these limitations, not a strategy problem.

**The pivot:** Massive (Polygon) becomes the primary data source for **everything except order placement**. Alpaca paper stays only as the execution venue. The rank pipeline switches from per-ticker calls to one daily grouped-aggregates call covering all ~10,000 US equities. The screener output expands from "~25 mega-caps" to "top 25 from the entire liquid US tradable universe." News sentiment becomes a first-class entry filter.

This unblocks four things the current architecture can't reach:
- Full-market screening (was theoretically possible but per-ticker rate limits made it painful)
- Real-volume crypto signals (was outright impossible — Alpaca paper crypto bars lie about volume)
- News + sentiment as a tradeable signal (was outright impossible — no source)
- Short interest, fundamentals as alpha factors (same)

## What "entire market" actually means

Tradable on Alpaca paper:
- US equities: ~10,000+ symbols (every common stock + ETF)
- Crypto: 73 USD-quoted pairs (the platform's hard limit)
- Options: not in scope this plan

After our liquidity filter (price ≥ $10, ADV ≥ $10M):
- Stocks: ~1,500–3,000 (the deep liquid universe)
- Crypto: ~25–35 (those with real-market volume; the rest are illiquid alts)

After daily ranking (composite score + lane endorsement):
- ~25 stocks + ~10 crypto in `opportunities.md` daily

So "look at the entire market" doesn't mean trading 10,000 names. It means **ranking against the full liquid universe**, not a pre-curated mega-cap shortlist. Today's ranking only sees ~500–800 names because we hand-fetch per-ticker bars; the new pipeline ranks against ~3,000.

## Architectural change

```
                              BEFORE                                  AFTER
                              ──────                                  ─────

 Universe       alpaca.get_active_assets   →  ~3,000        massive.aggs/grouped/locale/us  →  ~10,000
                + per-ticker bar loop      →  ~600 liquid       (one call gets all daily OHLC + volume)
                (slow, rate-limited)                            ∩ alpaca tradable list   →  ~5,000 tradable

 Bars (rank)    alpaca per-ticker × 3000   →  3000 calls     massive grouped daily       →  1 call
                                                            + alpaca per-ticker (live entries only)

 Bars (intel)   alpaca per-ticker × 25-40  →  unchanged      same — already efficient at this scale

 Bars (back-    alpaca historical          →  shallow        massive aggregates (already integrated)
   test)        Alpaca paper crypto fake   →  garbage         real volume → empirical edge confirmed

 News           none                       →  blind          massive /v2/reference/news with
                                                            built-in sentiment per ticker

 Earnings       Benzinga (paid)            →  blocked        Finnhub free-tier OR drop until paid
                                                            (low priority — backtest didn't show
                                                            earnings as a dominant loss source)

 Short int.     none                       →  no signal      massive /stocks/v1/short-interest
                                                            (alpha factor for screener)

 Order place    alpaca paper API           →  unchanged      unchanged — execution stays on Alpaca

 Macro          FRED VIX                   →  unchanged      unchanged — already free + reliable
```

## Why this hasn't been a no-op so far

Two reasons it hasn't already happened:
1. **Massive only entered the picture today** when you installed the MCP.
2. The bot's CLI calls Alpaca directly — to call Massive in production (cron), the bot needs `POLYGON_API_KEY` in `.env`. The MCP works for me (the agent) but not for the bot's own scheduled jobs.

Both are one-time setups: add the key, swap the data sources, push.

## Why "Massive primary, Alpaca for orders" is the right factoring

| Layer | Best provider | Why |
|---|---|---|
| Order placement (paper) | Alpaca | That's where the paper account lives |
| Order placement (live, future) | Alpaca | Same — bot is built for Alpaca |
| Tradable list (universe membership) | Alpaca | Ground truth for "can we even trade this" |
| Daily bars (rank, backtest) | Massive | One call → 10,000 tickers; deep history |
| Daily bars (live intel-scan, ~25-40 tickers) | Either | Massive marginally better but Alpaca works |
| News + sentiment | Massive | Built-in per-ticker sentiment scoring |
| Macro (VIX, yields) | FRED (free) | Already integrated; no Massive duplication needed |
| VIP social | Truth Social RSS | Already integrated; Massive doesn't cover social |

So the bot becomes: **Alpaca for "what can we trade" and "place the trade", Massive for everything else.**

## Effects on already-shipped plans

- **Phase 0 hotfixes** (universe expansion, VIX-aware regime, EOD decoupling, crypto pair filter) — unchanged. Still correct.
- **Plan 5a (universe + screener)** — *re-implemented* with `aggs/grouped` as the primary bar source. Same module names, much faster + broader.
- **Plan 5b (backtest harness)** — already uses Massive for bars. Now also uses Massive for news sentiment as a backtested filter.
- **Plan 5c (exit hardening)** — done (trailing stops empirically off, crypto post-fill verify, naked-position sweep). Earnings exclusion deferred — Finnhub free tier or skip.
- **Plan 5d (dynamic sizer)** — unchanged. After Plan 6 lands.
- **Plan 5e (halt)** — unchanged. After 5d.
- **Plan 5f (evolution)** — gets richer per-symbol data to evolve over.

## Implementation plan (Phase 6)

Six phases. Each ships independently.

### Phase 6a — Add `POLYGON_API_KEY` to env, build `MassiveClient`

- New `src/trading_bot/massive_client.py` — thin REST adapter around `api.polygon.io`. Three high-value endpoints:
  - `daily_grouped(date)` → DataFrame[ticker, o, h, l, c, v, vw] for all stocks on a trading day
  - `news(ticker, since=...)` → list of articles with sentiment per ticker
  - `short_interest(ticker)` → time series
- Env: `POLYGON_API_KEY` in `.env`. `Settings` validates presence; commands that need it fail fast with a helpful message if missing.
- Unit tests with mocked HTTP.

### Phase 6b — Universe build off `aggs/grouped`

- New `src/trading_bot/universe.py::build_universe_from_grouped(market: MassiveClient, alpaca: AlpacaClient, *, on_date: date)`:
  - Pull yesterday's grouped aggregates (one call) → ~10,000 rows
  - Pull Alpaca tradable list → set of tradable symbols
  - Intersect, apply liquidity filter, tag sectors
  - Return `list[LiquidAsset]`
- The existing per-ticker `build_universe()` becomes the fallback. New default: grouped path.
- `bot rank` measures and prints the speedup (expect 10×).

### Phase 6c — News-sentiment cache + entry filter

- New table `data/news_sentiment.db` keyed by (symbol, date). Stores aggregate sentiment scores per ticker per day from `/v2/reference/news` insights.
- Daily warmer: `bot news-warm` pulls latest articles for the active universe + computes a daily sentiment score per ticker.
- Strategy filter (gated by config): before MomentumStrategy emits BUY, check `sentiment_score(symbol, lookback=3d)`. If `< sentiment_floor` (e.g., -0.3), skip. Tunable; default off until backtested.
- Backtest harness fetches historical news sentiment for the test window — sweep the sentiment_floor knob to find the optimal threshold.

### Phase 6d — Earnings exclusion via free fallback (Finnhub)

- New `src/trading_bot/earnings_calendar.py` — Finnhub free-tier adapter. `next_earnings_within(symbol, days=3)` returns bool.
- Strategy gate: skip stock entries within 3 trading days of earnings. (Already spec'd in 5c-4; just unblocked by a non-Massive vendor.)
- Backtest validates whether the gate improves PF.

### Phase 6e — Short interest as a screener factor (optional, Plan 5f-adjacent)

- Pull biweekly short interest for the active universe, store in `data/short_interest.db`.
- Stage-1 screener composite gains a `short_squeeze_score` term: high short interest + recent positive return = candidate for a squeeze.
- Tested via backtest before being weighted; off by default.

### Phase 6f — Live intel-scan switches to Massive for bars (optional)

- For the 25-40 symbol intel-scan, swap Alpaca per-ticker bar fetches for Massive. Marginal improvement (Alpaca already works at this scale), but unifies the data layer.
- Order placement stays on Alpaca.

## Acceptance criteria

1. `bot rank` runs in <30s against the full ~10,000-symbol US tradable universe.
2. `opportunities.md` includes top-25 stocks drawn from the full liquid universe (not just mega-caps).
3. Backtest harness can fetch 2 years of news sentiment for any symbol set.
4. Strategy `MomentumStrategy.evaluate` accepts an optional `sentiment_score` parameter; if provided, gates BUY signals on it.
5. `POLYGON_API_KEY` documented in `.env.example` and `Settings` validates.
6. The full backtest (15 symbols, 24 months) re-runs with the news-sentiment filter and either improves PF/Sharpe or doesn't — either way, the verdict drives whether it ships in live.

## Effort

Phase 6a + 6b: 1 evening (the foundation).
Phase 6c: 1 evening (news cache + filter + backtest sweep).
Phase 6d: ½ evening (Finnhub free tier is straightforward).
Phase 6e: ½ evening (data plumbing only, no signal change yet).
Phase 6f: ½ evening (incremental).

Total: ~3-4 evenings of focused work.

## Risks

- **POLYGON_API_KEY rate limits.** User's plan has per-minute limits we hit earlier. Mitigation: grouped endpoint is one call per day; per-ticker calls only used in narrow places. Add exponential backoff to `MassiveClient`.
- **Massive 2-year history limit.** User's plan only goes back to 2024-04-26. Backtests longer than 2 years will fail; report errors clearly.
- **Sentiment-overfit risk.** A sentiment filter could overfit to the specific news regime in 2024-2026. Mitigation: backtest reports the per-strategy result with and without the filter; ship only if both regimes show improvement.
- **Universe explosion → entry-decision noise.** Going from 600 → 5000 raw candidates means more chances for low-quality signals to slip through. Mitigation: tighter ranking thresholds + the existing top-25 cap on `_load_active_universe`.

## Sequence recommendation

Ship 6a + 6b first (the foundation). Re-run rank locally and confirm the universe expansion. Then 6c (news sentiment) — this is the highest-leverage feature add, run the backtest sweep to find the right threshold. Then 6d (earnings via Finnhub) since it patches a known gap. Then 6e/6f as time permits.

After Plan 6, the locked sequence resumes:
- **5d** — Dynamic position sizer (knobs now data-driven)
- **5e** — Soft/hard halt + intraday equity polling
- **5f** — Regime-conditional evolution loop (now with sentiment + short-interest features available)

## Open questions

1. **POLYGON_API_KEY** — paste in `.env` now, or do you want to set up a `.env.example` and have me document the action for you to do later?
2. **Earnings vendor** — Finnhub free tier (60/min, 1k/day) is plenty for our 25-symbol watchlist. Acceptable, or wait until Massive plan is upgraded?
3. **News sentiment cache size** — 24 months × 30 symbols × ~5 articles/day = ~10,000 article rows. Tiny. SQLite keyed by (symbol, date) — confirmed acceptable?
4. **Live trading scope** — once Plan 6 ships, the bot can rank against the full tradable universe. Should we cap `_load_active_universe` at top-25 stocks for safety, or expand to top-50? More candidates → more positions → more concurrent risk.

## What changes for the user

- Add `POLYGON_API_KEY=...` to `.env`. (One-time, ~30 seconds.)
- Optional: add `FINNHUB_API_KEY=...` for the earnings adapter. (Free tier; sign-up takes a minute.)
- Tomorrow's bot fires (intel-scan, etc.) keep working as-is until Phase 6 is shipped — this is purely a research-stack expansion, not a live behavior change yet.
