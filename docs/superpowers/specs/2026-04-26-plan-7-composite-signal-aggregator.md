# Plan 7: Composite Signal Aggregator — Design

**Date:** 2026-04-26
**Status:** Approved, ready for implementation plan
**Predecessors:**
- [2026-04-26-plan-6-massive-first-architecture.md](2026-04-26-plan-6-massive-first-architecture.md)
- [2026-04-26-massive-rate-limit-design.md](2026-04-26-massive-rate-limit-design.md)

## Problem

The bot collects rich news/intel from six sources today (Polygon sentiment, Alpaca news, GDELT, EDGAR Form 4, Truth Social VIP, RSS) but only **one** of them — Polygon's per-ticker sentiment — actually reaches the trade decision path. The rest are surfaced in human-eyeball email reports.

The Plan 6c sentiment gate (`sentiment_floor=-0.5`, just shipped) uses Polygon alone. That's a defensive single-source gate. It misses material events flagged elsewhere: an 8-K filed today, a Trump tweet tagging the ticker, a GDELT-detected protest near the company's main facility — none of these influence the entry decision.

Plan 7 builds a **composite signal aggregator** that blends multiple sources into a single per-symbol decision. The orchestrator gates entries on the composite, not on any single source.

## Goals

- Every news source we collect can influence a trade decision (none stay email-only)
- Single in-code interface: `composite_signal_for(symbol) -> CompositeSignal`
- Hard blockers (8-K filed last 3d, VIP mention last 24h) are absolute — never overridden by sentiment positivity
- Backtest-able for at least the GDELT + EDGAR components (Polygon stays live-only due to rate-limit infeasibility)
- Cache-first reads at scan time (same pattern as Plan 6 grouped cache); cron does the writing
- Reversible at every layer: floor knob, blocker enable flags, kill-switch composite back to single-source via config

## Non-Goals

- Alpaca news headlines (no built-in sentiment; NLP classifier is its own project)
- EDGAR Form 4 insider trades (signal interpretation is genuinely ambiguous — insider buying ≠ always bullish)
- Position-size scaling based on score (gate is binary; risk manager sizes)
- Live training/test parity (backfill only covers GDELT + EDGAR; Polygon stays live-only)
- Real-time intra-bar reaction (cron-driven, max 1-hour staleness during market hours)
- Composite signal for crypto (sources are equity-focused; crypto bypasses the gate)

## Architecture

Three-layer pattern, mirroring Plan 6's grouped-cache shape:

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 (sources): per-symbol API queries                       │
│   • Polygon sentiment   → score in [-1, +1]    (already cached) │
│   • EDGAR 8-K (last 3d) → bool blocker         (new)            │
│   • GDELT GKG tone (3d) → score in [-1, +1]    (new per-symbol) │
│   • VIP mention (24h)   → bool blocker         (new tagging)    │
│              ↓                                                  │
│ Layer 2 (aggregator): SignalAggregator.compute(symbol)          │
│   → CompositeSignal(symbol, score, blockers, components)        │
│              ↓ writes to                                        │
│ Layer 3 (cache): data/composite_signals.db (SQLite, hourly TTL) │
│              ↑ read by                                          │
│ Orchestrator gate: pass if score≥floor AND no blockers          │
└─────────────────────────────────────────────────────────────────┘
```

Writer: new `bot composite-warm` CLI, hourly cron at :55.
Reader: orchestrator's existing entry path, replacing the `score_for(symbol)` call.

## Components

### 1. `src/trading_bot/gdelt_per_symbol.py` (NEW)

Per-ticker GDELT GKG query. Free, no rate limit, deep history available.

API:
```python
@dataclass(frozen=True)
class GdeltSymbolSignal:
    symbol: str
    avg_tone: float | None       # -1..+1, None if no records found
    article_count: int
    lookback_days: int

def gdelt_tone_for_symbol(
    symbol: str, *, lookback_days: int = 3,
    company_name: str | None = None,  # disambiguates ticker collisions
) -> GdeltSymbolSignal: ...
```

Implementation: query GDELT 2.0 DOC API (`https://api.gdeltproject.org/api/v2/doc/doc`) with `query=$SYMBOL OR "$company_name"` filtered by date range. Tone field is GDELT's [-100, +100] scale; normalize to [-1, +1].

### 2. `src/trading_bot/edgar_8k.py` (NEW)

Per-ticker EDGAR 8-K filing query. Material events: earnings, M&A, lawsuits, restructuring.

API:
```python
@dataclass(frozen=True)
class Edgar8KFiling:
    cik: str
    ticker: str
    filed_at: datetime
    form_type: str              # "8-K" or "8-K/A"
    accession: str
    url: str
    items: list[str]            # e.g., ["1.01", "2.02"]

def recent_8k_filings(
    symbol: str, *, lookback_days: int = 3,
) -> list[Edgar8KFiling]: ...

def has_recent_8k(symbol: str, *, lookback_days: int = 3) -> bool: ...
```

Implementation: needs ticker→CIK mapping. EDGAR publishes a free `https://www.sec.gov/files/company_tickers.json` covering ~13k companies. Cached locally in `data/edgar_ticker_map.json`, refreshed weekly. Then queries `https://data.sec.gov/submissions/CIK<10-digit-cik>.json` for filing history.

### 3. `src/trading_bot/vip_mentions.py` (NEW)

Extracts `$TICKER` mentions from cached VIP posts (Plan 4's `vip_tweets.py` already pulls Truth Social RSS into a local store).

API:
```python
@dataclass(frozen=True)
class VipMention:
    symbol: str
    handle: str
    posted_at: datetime
    severity: str               # "high" | "med" | "low"
    text_excerpt: str

def vip_mentions_for_symbol(
    symbol: str, *, lookback_hours: int = 24,
) -> list[VipMention]: ...

def has_vip_mention(
    symbol: str, *, lookback_hours: int = 24, min_severity: str = "med",
) -> bool: ...
```

Implementation: scan cached VIP posts using `re.compile(r"\$" + symbol + r"\b", re.IGNORECASE)` plus a fallback that matches the company name. Severity threshold defaults to "med" — only meaningful posts trigger the blocker.

### 4. `src/trading_bot/signal_aggregator.py` (NEW)

The composite logic. Pure function — no IO except via the four source modules.

API:
```python
@dataclass(frozen=True)
class SignalComponents:
    polygon_score: float | None       # from news_sentiment.score_for
    gdelt_score: float | None         # from gdelt_tone_for_symbol
    has_8k: bool
    has_vip_mention: bool

@dataclass(frozen=True)
class CompositeSignal:
    symbol: str
    computed_at: datetime
    score: float | None               # weighted average; None if no source data
    has_blocker: bool
    blocker_reason: str               # "" if has_blocker is False
    components: SignalComponents

def compute(
    symbol: str, *,
    polygon_weight: float = 0.5,
    gdelt_weight: float = 0.5,
    blocker_8k_lookback_days: int = 3,
    blocker_vip_lookback_hours: int = 24,
) -> CompositeSignal: ...
```

Composite math:
```
if has_8k:                    blocker_reason = "8-K filed last 3 days"
elif has_vip_mention:         blocker_reason = "VIP mention last 24h"
else:                         blocker_reason = ""

scores_present = [s for s in (polygon_score, gdelt_score) if s is not None]
weights_present = [w for s, w in zip(scores, weights) if s is not None]
score = sum(s*w for s,w in zip(scores_present, weights_present)) / sum(weights_present)
       if scores_present else None
```

Missing-component fallback: if Polygon has no data, GDELT-only score is used (full weight on GDELT). If both missing, score is None and the gate passes (no-data ≠ negative — same principle as `passes_filter(None, ...)` returns True today).

### 5. `src/trading_bot/composite_cache.py` (NEW)

SQLite-backed cache. Pattern matches `MassiveGroupedCache`.

Schema:
```sql
CREATE TABLE composite_signals (
  symbol         TEXT NOT NULL,
  computed_at    DATETIME NOT NULL,
  score          REAL,           -- nullable
  polygon_score  REAL,
  gdelt_score    REAL,
  has_8k         BOOLEAN NOT NULL,
  has_vip        BOOLEAN NOT NULL,
  blocker_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (symbol, computed_at)
);
CREATE INDEX idx_symbol_computed ON composite_signals(symbol, computed_at DESC);
```

API:
```python
class CompositeSignalCache:
    def write(self, sig: CompositeSignal) -> None: ...
    def latest(
        self, symbol: str, *, max_age_minutes: int = 120,
    ) -> CompositeSignal | None: ...
    def evict_older_than(self, *, days: int = 7) -> int: ...
```

## Refresh — `bot composite-warm` CLI + cron

New CLI:
```
bot composite-warm [--symbols A,B,C] [--include-cache-skip] [--verbose]
```

Default behavior: pulls active stock universe from `_load_active_universe`, computes composite for each, writes to cache. Skips symbols already computed within `max_age_minutes=30` (so a manual rerun in the same window is cheap).

New cron: `trading-bot-composite-warm` at:
- `35 6 * * 1-5` (premarket, after `massive-refresh` at 06:30 and before `premarket-rank` at 08:00)
- `55 9-15 * * 1-5` (every market hour at :55, 5 minutes before `intel-scan` at :00)

Worst-case runtime per run: 25 symbols × ~1.5s/symbol (GDELT + EDGAR + cache reads, parallelizable) = ~40s.

## Orchestrator integration

In `src/trading_bot/orchestrator.py`, replace the current sentiment-gate block (currently calling `score_for` + `passes_filter`):

```python
# Plan 7: composite signal gate
if entry.asset_class != "crypto":
    from trading_bot.composite_cache import CompositeSignalCache
    cache = CompositeSignalCache()
    sig = cache.latest(symbol, max_age_minutes=120)

    if sig is not None and sig.has_blocker:
        decisions.append(Decision(
            symbol=symbol, action="skipped_composite_blocker",
            reason=sig.blocker_reason,
        ))
        continue

    floor = self._cfg.strategy.composite_floor
    if sig is not None and sig.score is not None and floor is not None:
        if sig.score < floor:
            decisions.append(Decision(
                symbol=symbol, action="skipped_composite_score",
                reason=f"composite {sig.score:.2f} < floor {floor:.2f}",
            ))
            continue
```

If cache returns None (warm hasn't run, or stale beyond 2h), the gate passes — same no-data-doesn't-block principle as today.

The legacy `sentiment_floor` path is removed; `score_for` and `passes_filter` stay in `news_sentiment.py` (still used by the aggregator's Polygon component) but are no longer called by the orchestrator directly.

## Configuration

`strategy/config.yaml` gains a `composite` section under `strategy`:

```yaml
strategy:
  composite_floor: -0.3            # tighter than legacy -0.5 (more sources = stronger signal)
  composite_polygon_weight: 0.5
  composite_gdelt_weight: 0.5
  composite_blocker_8k_lookback_days: 3
  composite_blocker_vip_lookback_hours: 24
  sentiment_floor: null            # legacy single-source path retired
```

`StrategyConfig` in `config.py` gains the matching fields with the same defaults. Setting `composite_floor: null` disables the score gate entirely (blockers still fire). Setting all blocker lookbacks to 0 disables blockers (score gate still fires).

## Backfill + sweep (Phase C — the hybrid validation)

New CLI: `bot composite-backfill --from YYYY-MM-DD --to YYYY-MM-DD --symbols A,B,...`

For each (symbol, day) in the window:
- Pull GDELT GKG records (free, deep history)
- Pull EDGAR 8-K filings (free, deep history)
- Skip Polygon component (no historical data; rate-limit makes backfill infeasible)
- Compute composite with `polygon_score=None`, write to `composite_signals.db` with `computed_at` synthetic (set to noon UTC of that day)

Backtester (`bot backtest`) gains a `--composite-floor` knob:
- For each candidate trade, query `CompositeSignalCache.latest(symbol, max_age_minutes=24*60)` keyed by the simulated trade date
- Apply the same blocker + floor logic as the live orchestrator
- Sweep floor across `[-0.7, -0.5, -0.3, -0.1, 0.0, +0.1]`; weights across `[(0.7, 0.3), (0.5, 0.5), (0.3, 0.7)]` (Polygon is None during backtest, so weights collapse to GDELT-only — included for completeness)
- Pick the floor that maximizes PF without cutting >20% of trades

**Acceptance criteria for the sweep:** the chosen floor must improve PF by ≥0.1 over the no-composite baseline (PF 1.25), AND not cut more than 20% of trades. If neither is achievable, retain the live default of `-0.3` and flag for re-evaluation.

Train/test mismatch caveat: backtest uses GDELT-only composite; live uses GDELT + Polygon composite. Slight mismatch is accepted. If the chosen floor is robust across multiple values (e.g., `-0.3` and `-0.5` both produce similar PF lift), it'll generalize.

## Tests

New test files:
- `tests/test_signal_aggregator.py` — composite math (weighted average, missing-component fallback), blocker precedence (8-K beats VIP beats score gate), no-data passes
- `tests/test_gdelt_per_symbol.py` — query construction, tone normalization [-100,+100] → [-1,+1], empty-result handling
- `tests/test_edgar_8k.py` — ticker→CIK mapping cache, date filter boundary, has_recent_8k boolean
- `tests/test_vip_mentions.py` — `$TICKER` regex (case-insensitive, word-boundary), severity filter, lookback window
- `tests/test_composite_cache.py` — write/read roundtrip, max_age_minutes filter, eviction
- `tests/test_composite_warm_cli.py` — CLI happy path with mocked sources

Updates:
- `tests/test_orchestrator.py` — composite-gate behaviors (passes if cache empty, blocks on 8-K, blocks below floor, passes above floor)

## Files

**New (8):**
- `src/trading_bot/gdelt_per_symbol.py`
- `src/trading_bot/edgar_8k.py`
- `src/trading_bot/vip_mentions.py`
- `src/trading_bot/signal_aggregator.py`
- `src/trading_bot/composite_cache.py`
- 6 new test files (listed above)

**Modified:**
- `src/trading_bot/cli.py` — `bot composite-warm` and `bot composite-backfill` commands
- `src/trading_bot/orchestrator.py` — replace sentiment gate with composite gate
- `src/trading_bot/config.py` — `composite_*` fields in `StrategyConfig`
- `strategy/config.yaml` — `composite_*` settings, `sentiment_floor: null`
- `src/trading_bot/backtest/simulator.py` — `--composite-floor` knob

**New scheduled tasks:**
- `trading-bot-composite-warm-premarket` at `35 6 * * 1-5`
- `trading-bot-composite-warm-hourly` at `55 9-15 * * 1-5`

## Risks & Mitigations

- **Risk: GDELT API throttles or schema changes.** Mitigation: aggregator's missing-component fallback keeps the gate functional with Polygon-only if GDELT is unreachable.
- **Risk: Ticker→CIK mapping has false positives** (e.g., `F` for Ford collides with Form 4 fillers). Mitigation: EDGAR's official mapping is authoritative; collisions are rare; spot-check during impl.
- **Risk: VIP mention regex false positives** (e.g., `$5` in a tweet about money, not a ticker). Mitigation: regex requires `$[A-Z]{2,5}\b`, plus the company-name fallback for context.
- **Risk: Composite gate blocks too many good entries** (false positive rate too high). Mitigation: Phase C sweep tunes the floor empirically; logged `skipped_composite_*` decisions surface in `last_scan` and dashboard for human review.
- **Risk: Cache schema collision with prior runs.** Mitigation: `composite_signals.db` is a new file; `news_sentiment.db` stays untouched.
- **Risk: Cron scheduling collisions across the 5–6 trading-bot tasks.** Mitigation: composite-warm at :55 is offset from intel-scan at :00 and portfolio-watch at :15/:45.

## Acceptance Criteria

1. `bot composite-warm` populates `composite_signals.db` for every symbol in active universe; runtime <60s
2. `composite_signal_for(symbol)` (live) returns a `CompositeSignal` with all four components attempted; missing components don't error
3. Orchestrator skips entries on 8-K blocker, VIP mention blocker, or score below floor — each decision visible in `result.decisions` with reason
4. `bot composite-backfill` populates 24 months of GDELT + EDGAR data for the 15 backtest symbols in <2 hours wall time
5. Backtester runs cleanly with `--composite-floor` knob; sweep produces a chosen floor value
6. All existing tests pass; 6 new test modules pass; total test count grows by ≥30
7. New cron tasks registered and visible in scheduled-tasks listing
8. Disabling each component via config (set weight to 0, set lookback to 0, set floor to null) works as a kill switch without code changes
