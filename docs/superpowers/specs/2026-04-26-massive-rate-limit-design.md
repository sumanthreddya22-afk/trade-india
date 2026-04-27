# Massive Rate-Limit Hardening — Design

**Date:** 2026-04-26
**Status:** Approved, in implementation
**Predecessor:** [2026-04-26-plan-6-massive-first-architecture.md](2026-04-26-plan-6-massive-first-architecture.md)

## Problem

`bot rank` (scheduled at 8:00 ET premarket) stalls for 20+ minutes when run end-to-end. Two real failure modes contribute:

1. **Massive itself stalls.** `MassiveClient._get` does a single 65-second retry on 429 then raises. On the user's ~5 calls/min Polygon plan, the grouped-aggregates call can fail to land cleanly during peak windows.

2. **Legacy fallback fans out to 10k symbols.** When the Massive path raises, `bot rank` catches the exception and calls `_legacy_build_universe(alpaca, bar_loader=...)`, which iterates the entire Alpaca-tradable universe (~10k symbols) and fetches daily bars for each. At ~200ms/call this is 30+ minutes minimum.

Either path keeps `opportunities.md` un-refreshed, leaving downstream `intel-scan` runs blind for the trading day.

A separate but related problem: `news-warm` makes one Massive call per symbol with no "already fetched today" check, so every cron firing repays the full per-symbol cost.

## Goals

- `bot rank` completes in **under 2 minutes**, every time, with no exceptions
- The bot can survive Massive being completely unreachable for up to 5 calendar days (covers 3-day weekends + grace) without losing the ability to rank
- `news-warm` repeated within the same trading day is a near-no-op (cache hits)
- Zero new external dependencies; all changes inside the existing Python codebase

## Non-Goals

- Sentiment-floor activation (still off — needs a backtest sweep first)
- Earnings filter (Plan 6d, separate work)
- Massive MCP-as-cache-seeder (brittle — depends on a Claude session running)
- Anything that touches Alpaca rate-limit handling (out of scope)

## Architecture

Three-layer fallback hierarchy. Each layer is faster and more constrained than the one above it; consumer code (`bot rank`, scan tasks) reads only from cache and never calls Massive directly.

```
┌────────────────────────────────────────────────────────────────────┐
│ Layer 2 (writer): bot massive-refresh — cron @ 06:30 ET Mon-Fri    │
│  ↓ writes to                                                       │
│ Layer 1 (cache): data/massive_grouped.db (SQLite)                  │
│  ↑ read by                                                         │
│ bot rank, news-warm — cache-only on the Massive side               │
│                                                                    │
│ Layer 3 (fallback): CORE_LIQUID_TICKERS seed list — used only if   │
│ cache is fully empty (post-disk-loss / cold start)                 │
└────────────────────────────────────────────────────────────────────┘
```

### Layer 1: Disk-backed grouped cache

**File:** `src/trading_bot/massive_cache.py` (new)

- SQLite at `data/massive_grouped.db`
- Schema:
  ```sql
  CREATE TABLE grouped_bars (
    trade_date  DATE NOT NULL,
    ticker      TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL, vw REAL,
    cached_at   DATETIME NOT NULL,
    PRIMARY KEY (trade_date, ticker)
  );
  CREATE INDEX idx_trade_date ON grouped_bars(trade_date);
  ```
- API:
  - `MassiveGroupedCache.store(date, df)` — upsert all rows for a trading day. Idempotent.
  - `MassiveGroupedCache.has(date) -> bool`
  - `MassiveGroupedCache.latest(max_age_days=5) -> tuple[date, DataFrame] | None` — returns most recent cached day within window, or None
  - `MassiveGroupedCache.evict_older_than(days=30)` — keeps cache bounded
- Writes are upserts (re-running refresh on a date overwrites the row). Reads are by date.

### Layer 2: Refresh task

**New CLI:** `bot massive-refresh [--news] [--days N]`

- Walks back N (default 5) trading days from today; for each day not already in cache, calls `MassiveClient.daily_grouped(day)` and stores the result
- Idempotent: skips dates already cached
- `--news` flag also refreshes sentiment for the active universe (capped at top 50 symbols)
- Exits 0 on success; exits non-zero only if the cache ends up empty (i.e. zero cached days within the last 7 trading days)

**New cron:** `30 6 * * 1-5` — runs 90 minutes before `premarket-rank` at 08:00 ET. Worst case 5 days × ~15s = 75s wall time; budget is 90 minutes.

### Layer 3: Bounded seed-list fallback

**Constant:** `CORE_LIQUID_TICKERS` in `src/trading_bot/universe.py` — hardcoded list of ~200 well-known liquid US-equity tickers (mega-caps across SPY/QQQ holdings, FAANG, semis, financials, energy majors, biotech leaders, defensive names).

**Function:** `build_universe_from_seed_list(alpaca) -> list[LiquidAsset]`
- Pulls Alpaca tradable equities + crypto (existing call, fast — ~10k results in one Alpaca request)
- Intersects equities with `CORE_LIQUID_TICKERS`
- Returns same shape as `build_universe_from_grouped`, with `last_price=Decimal("0")` and `avg_dollar_volume=Decimal("0")` (downstream stage-1 doesn't use these for shortlisting; ADV is recomputed from per-symbol bars)

**Path used only when:** cache returns no data within `max_age_days=5`. This should be vanishingly rare — only on first install, post-disk-loss, or during a multi-day Massive outage.

### Other changes

#### `MassiveClient._get` — exponential backoff + per-instance throttle

Replace the single 65s retry with:
```python
BACKOFF_SCHEDULE = (10, 30, 60, 120, 300)  # seconds
MIN_CALL_INTERVAL_S = 13  # 5 calls/min ceiling = 12s; 13 buffers
```

- Track `_last_call_at` on the instance; sleep to enforce minimum spacing
- On 429: sleep for next backoff value; retry; raise after exhausting schedule (~9 minutes total)
- On success: continue, no extra delay

#### `news_sentiment.warm_for_symbols` — cache-aware

- Before calling `aggregate_sentiment(sym)`, check `cache.latest(sym, max_age_days=1)`. If a row exists from today, skip the Massive call entirely.
- Use a single `MassiveClient` instance across symbols (so the per-instance throttle applies)
- Cap input list at top 50 symbols

#### `bot rank` — cache-only

Replaces the current `daily_grouped(day)` loop + legacy fallback with:

```python
cache = MassiveGroupedCache()
result = cache.latest(max_age_days=5)
if result is not None:
    on_date, grouped_df = result
    universe = build_universe_from_grouped(alpaca, massive_grouped_loader=lambda: grouped_df, ...)
else:
    universe = build_universe_from_seed_list(alpaca)
```

#### `_legacy_build_universe` — deleted

Remove from `universe.py` along with the `build_universe = _legacy_build_universe` alias. Update `bot screen-universe` (the only other caller) to use `build_universe_from_seed_list` instead.

## Data Flow — Tomorrow Morning

| Time     | Task                  | Massive calls | Wall time | Outcome |
|----------|-----------------------|---------------|-----------|---------|
| TONIGHT  | Manual `massive-refresh` (one-shot, run once after build) | ~5 | ~90s | Cache primed before tomorrow's cron starts |
| 06:30 ET | `massive-refresh` (cron)              | 0–1 (most days cached) | <30s | Today's day cached |
| 08:00 ET | `premarket-rank`                      | 0  | <90s    | Reads cache, ranks 200, writes opportunities.md |
| 08:45 ET | `news-warm`                           | ≤25 (capped) | ~5 min | Cache populated for trading day |
| 09:00 ET | `intel-scan`                          | 0  | <60s    | Trades against opportunities.md |
| 11:45 ET | `news-warm`                           | 0–5 (mostly cache hits) | <90s | Refreshes any stale rows |

**Massive-down scenario:** if `massive-refresh` fails completely tomorrow morning, `bot rank` reads cached data from up to 3 days ago. Universe composition for the top 200 by ADV barely shifts in 3 days. Rank still works.

**Cold-start scenario** (cache empty AND Massive down): `bot rank` falls through to seed list. Universe is ~150 hardcoded names. Rank works in ~30s.

## Tests

- `tests/test_massive_cache.py` (new) — store/has/latest/evict, idempotency on re-store, max_age_days boundary, empty-cache behavior
- Update `tests/test_news_sentiment.py` — add a "skip if cached today" test
- Update `tests/test_universe.py` (or add new file) — `build_universe_from_seed_list` returns intersection with Alpaca tradable, `_legacy_build_universe` removal doesn't break callers
- Smoke test: run `bot massive-refresh` against live Massive once during implementation; verify cache populates and rank reads from it in <90s

## Files

**New:**
- `src/trading_bot/massive_cache.py`
- `tests/test_massive_cache.py`

**Modified:**
- `src/trading_bot/cli.py` — new `massive-refresh` command, rank rewired to read cache, screen-universe uses seed list
- `src/trading_bot/massive_client.py` — exponential backoff + per-instance throttle
- `src/trading_bot/news_sentiment.py` — cache-aware skip + shared client for throttle
- `src/trading_bot/universe.py` — add `CORE_LIQUID_TICKERS` and `build_universe_from_seed_list`; delete `_legacy_build_universe` and `build_universe` alias

**New scheduled task:** `trading-bot-massive-refresh` at `30 6 * * 1-5`

## Risks & Mitigations

- **Risk: SQLite write contention if refresh runs concurrent with rank.** Mitigation: refresh is at 06:30, rank at 08:00 — 90-minute gap. SQLite WAL mode handles concurrent reader/writer anyway.
- **Risk: Hardcoded seed list goes stale (delisted tickers).** Mitigation: list is intersected with current Alpaca-tradable list, so delisted names drop silently. Quarterly review of the list as a follow-up.
- **Risk: Cron `massive-refresh` fails silently in the morning.** Mitigation: the task exits non-zero on empty cache; scheduled-tasks runner surfaces failures. Also, `bot rank` logs which path it took (cache vs seed-list) so post-hoc investigation is trivial.
- **Risk: 5-day staleness covers most calendar gaps but a Thanksgiving-week scenario could exceed it.** Mitigation: `bot rank` logs the cache date used; if it's >2 trading days old AND today is a normal session, the next morning's run will re-attempt refresh — a single failed day is recoverable.

## Acceptance Criteria

1. `bot rank` completes in <2 minutes against an empty Alpaca position list
2. `bot rank` completes in <2 minutes when `data/massive_grouped.db` is deleted (uses seed list)
3. `bot massive-refresh` populates cache; subsequent `bot rank` reads from it (verified by log line `[rank] cache hit (date=YYYY-MM-DD)`)
4. `bot news-warm` run twice in a row makes ≥80% fewer Massive calls on the second run
5. New cron `trading-bot-massive-refresh` registered and visible in `bot status` / scheduled-tasks listing
6. All existing tests still pass; new tests pass
