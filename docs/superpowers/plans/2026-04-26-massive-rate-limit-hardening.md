# Massive Rate-Limit Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `bot rank` complete in <2 minutes every time by introducing a disk-cache for Massive grouped data, a separate refresh task that writes the cache, and a bounded seed-list fallback that replaces the 10k-symbol legacy fanout.

**Architecture:** Three-layer fallback. (1) `MassiveGroupedCache` SQLite at `data/massive_grouped.db` is the only thing `bot rank` reads from. (2) New `bot massive-refresh` CLI runs on cron at 06:30 ET and is the only thing that calls Massive grouped. (3) Hardcoded `CORE_LIQUID_TICKERS` (~200 mega-caps) intersected with Alpaca tradable list is the cold-start fallback when cache is empty. `MassiveClient` itself gains exponential backoff and per-instance throttle so the refresh task can survive 429s.

**Tech Stack:** Python 3.13, SQLAlchemy (matches `news_sentiment.py` pattern), Click (matches existing CLI), pytest, `requests` for HTTP, scheduled-tasks MCP for cron registration.

---

## File Structure

| File | Purpose |
|------|---------|
| `src/trading_bot/massive_cache.py` (NEW) | `MassiveGroupedCache` — SQLite wrapper for grouped OHLC data. One responsibility: persist + read date-keyed grouped frames. |
| `tests/test_massive_cache.py` (NEW) | Unit tests for cache: store, has, latest, evict, idempotency, max_age boundary. |
| `src/trading_bot/massive_client.py` (MODIFIED) | Add `BACKOFF_SCHEDULE` + `MIN_CALL_INTERVAL_S`; replace single-65s retry with exponential backoff; track `_last_call_at` for throttle. |
| `src/trading_bot/universe.py` (MODIFIED) | Add `CORE_LIQUID_TICKERS` constant + `build_universe_from_seed_list()`. Delete `_legacy_build_universe()` and `build_universe = _legacy_build_universe` alias. |
| `tests/test_universe.py` (MODIFIED) | Add tests for seed-list builder; remove tests of deleted legacy function. |
| `src/trading_bot/news_sentiment.py` (MODIFIED) | `warm_for_symbols`: skip symbols already cached today; share one MassiveClient instance for throttle to apply. Cap input at 50 symbols. |
| `tests/test_news_sentiment.py` (MODIFIED) | Add test for "skip if cached today" behavior. |
| `src/trading_bot/cli.py` (MODIFIED) | Add `bot massive-refresh` command. Rewire `bot rank` to read cache + seed-list fallback (delete the per-day grouped loop). Update `bot screen-universe` to use seed-list path. |
| Cron registration | New scheduled task `trading-bot-massive-refresh` at `30 6 * * 1-5`. |

---

## Task 1: MassiveGroupedCache — SQLite store for grouped OHLC

**Files:**
- Create: `src/trading_bot/massive_cache.py`
- Create: `tests/test_massive_cache.py`

- [ ] **Step 1.1: Write the failing test file**

Create `tests/test_massive_cache.py`:

```python
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from trading_bot.massive_cache import MassiveGroupedCache


def _df(rows: list[tuple[str, float, float, float, float, float]]) -> pd.DataFrame:
    """Build a grouped DataFrame indexed by ticker with columns o,h,l,c,v,vw."""
    return pd.DataFrame(
        [{"o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw} for (_, o, h, l, c, v, vw) in rows],
        index=[r[0] for r in rows],
    )


def test_store_and_has(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    d = date(2026, 4, 24)
    c.store(d, _df([("AAPL", 1, 2, 0.5, 1.5, 1000, 1.4)]))
    assert c.has(d) is True
    assert c.has(date(2026, 4, 23)) is False


def test_store_is_idempotent(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    d = date(2026, 4, 24)
    c.store(d, _df([("AAPL", 1, 2, 0.5, 1.5, 1000, 1.4)]))
    # Re-store same date with updated values — should overwrite, not duplicate
    c.store(d, _df([("AAPL", 9, 9, 9, 9, 9, 9)]))
    out = c.latest(max_age_days=30)
    assert out is not None
    on_date, df = out
    assert on_date == d
    assert float(df.loc["AAPL", "c"]) == 9.0
    # Only one row, not two
    assert len(df) == 1


def test_latest_returns_most_recent_within_window(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    today = datetime.now(timezone.utc).date()
    c.store(today - timedelta(days=10), _df([("OLD", 1, 1, 1, 1, 1, 1)]))
    c.store(today - timedelta(days=2), _df([("NEW", 2, 2, 2, 2, 2, 2)]))
    out = c.latest(max_age_days=5)
    assert out is not None
    on_date, df = out
    assert on_date == today - timedelta(days=2)
    assert "NEW" in df.index
    assert "OLD" not in df.index  # different date


def test_latest_none_when_no_fresh_data(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    today = datetime.now(timezone.utc).date()
    c.store(today - timedelta(days=10), _df([("OLD", 1, 1, 1, 1, 1, 1)]))
    assert c.latest(max_age_days=5) is None


def test_latest_none_on_empty_cache(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    assert c.latest(max_age_days=30) is None


def test_evict_older_than(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    today = datetime.now(timezone.utc).date()
    c.store(today - timedelta(days=40), _df([("OLD", 1, 1, 1, 1, 1, 1)]))
    c.store(today - timedelta(days=2), _df([("NEW", 2, 2, 2, 2, 2, 2)]))
    c.evict_older_than(days=30)
    assert c.has(today - timedelta(days=40)) is False
    assert c.has(today - timedelta(days=2)) is True


def test_store_empty_dataframe_is_noop(tmp_path):
    """Massive returns empty results on holidays/weekends — cache should not error."""
    c = MassiveGroupedCache(tmp_path / "g.db")
    c.store(date(2026, 4, 25), pd.DataFrame())  # Saturday
    assert c.has(date(2026, 4, 25)) is False  # nothing actually stored
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_massive_cache.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.massive_cache'`.

- [ ] **Step 1.3: Implement MassiveGroupedCache**

Create `src/trading_bot/massive_cache.py`:

```python
"""Disk-backed cache for Massive grouped-aggregates data.

`bot rank` and other consumers read grouped OHLC from this cache only —
they never call Massive directly. The cache is filled by `bot massive-
refresh`, which is the single place in the system that calls Massive's
`/v2/aggs/grouped` endpoint. This decouples consumer-side trading
windows (8:00 ET premarket-rank, hourly intel-scan) from Massive's
~5 calls/min rate budget.

Schema: one row per (trade_date, ticker). Reads return a DataFrame
indexed by ticker with the columns the universe builder expects
(o, h, l, c, v, vw).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Column, Date, DateTime, Float, String, create_engine, delete, select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Session


GROUPED_DB_PATH = Path("data/massive_grouped.db")


class _Base(DeclarativeBase):
    pass


class _GroupedRow(_Base):
    __tablename__ = "grouped_bars"
    trade_date = Column(Date, primary_key=True)
    ticker = Column(String, primary_key=True)
    o = Column(Float, nullable=False)
    h = Column(Float, nullable=False)
    l = Column(Float, nullable=False)
    c = Column(Float, nullable=False)
    v = Column(Float, nullable=False)
    vw = Column(Float, nullable=False)
    cached_at = Column(DateTime, nullable=False)


class MassiveGroupedCache:
    """SQLite-backed cache of Polygon grouped-aggregates data.

    Idempotent writes (re-store on a given date overwrites prior rows
    for that date). Reads are by date or "latest within window".
    """

    def __init__(self, db_path: Path | str = GROUPED_DB_PATH) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        _Base.metadata.create_all(self._engine)

    def store(self, trade_date: date, df: pd.DataFrame) -> int:
        """Upsert all rows from a grouped DataFrame for `trade_date`.

        Empty DataFrame is a no-op (e.g. Massive returned no results
        for a holiday). Returns number of rows written.
        """
        if df.empty:
            return 0
        now = datetime.utcnow()
        # Idempotency: clear any prior rows for this date, then insert fresh.
        with Session(self._engine) as s:
            s.execute(delete(_GroupedRow).where(_GroupedRow.trade_date == trade_date))
            payload = [
                {
                    "trade_date": trade_date,
                    "ticker": str(ticker),
                    "o": float(row["o"]),
                    "h": float(row["h"]),
                    "l": float(row["l"]),
                    "c": float(row["c"]),
                    "v": float(row["v"]),
                    "vw": float(row.get("vw", 0.0) or 0.0),
                    "cached_at": now,
                }
                for ticker, row in df.iterrows()
            ]
            if payload:
                s.execute(sqlite_insert(_GroupedRow), payload)
            s.commit()
            return len(payload)

    def has(self, trade_date: date) -> bool:
        with Session(self._engine) as s:
            row = s.execute(
                select(_GroupedRow.trade_date)
                .where(_GroupedRow.trade_date == trade_date)
                .limit(1)
            ).first()
            return row is not None

    def latest(self, *, max_age_days: int = 5) -> tuple[date, pd.DataFrame] | None:
        """Return (date, DataFrame) for the most recent cached trading
        day within `max_age_days` of today, or None if nothing fresh."""
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=max_age_days)
        with Session(self._engine) as s:
            recent = s.execute(
                select(_GroupedRow.trade_date)
                .where(_GroupedRow.trade_date >= cutoff)
                .order_by(_GroupedRow.trade_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if recent is None:
                return None
            rows = s.execute(
                select(_GroupedRow).where(_GroupedRow.trade_date == recent)
            ).scalars().all()
        if not rows:
            return None
        df = pd.DataFrame(
            [{"o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v, "vw": r.vw} for r in rows],
            index=[r.ticker for r in rows],
        )
        return recent, df

    def evict_older_than(self, *, days: int = 30) -> int:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        with Session(self._engine) as s:
            result = s.execute(delete(_GroupedRow).where(_GroupedRow.trade_date < cutoff))
            s.commit()
            return result.rowcount or 0
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_massive_cache.py -v
```
Expected: 7 PASSED.

- [ ] **Step 1.5: Commit**

```bash
git add src/trading_bot/massive_cache.py tests/test_massive_cache.py
git commit -m "$(cat <<'EOF'
feat(plan-6): MassiveGroupedCache — SQLite store for grouped OHLC data

First piece of the rate-limit hardening. New cache at data/
massive_grouped.db lets consumer code (bot rank, intel-scan) read
grouped data without ever calling Massive directly. Idempotent writes,
date-keyed reads, eviction policy.

7 tests cover: store + has, idempotency, latest within window,
empty cache, no-fresh-data, holiday no-op, eviction.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: MassiveClient — exponential backoff + per-instance throttle

**Files:**
- Modify: `src/trading_bot/massive_client.py`
- Test: inline behavioral test in new file `tests/test_massive_client.py`

- [ ] **Step 2.1: Write the failing test**

Create `tests/test_massive_client.py`:

```python
"""Tests for the rate-limit handling in MassiveClient. The HTTP layer
is mocked so tests don't hit Polygon."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from trading_bot.massive_client import (
    BACKOFF_SCHEDULE,
    MIN_CALL_INTERVAL_S,
    MassiveClient,
    MassiveRateLimitError,
)


class _FakeResp:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_throttle_spaces_consecutive_calls(monkeypatch):
    """Two consecutive calls on the same client instance must be at
    least MIN_CALL_INTERVAL_S apart."""
    client = MassiveClient(api_key="test")

    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.massive_client.time.sleep", lambda s: sleeps.append(s))

    monkeypatch.setattr(
        "trading_bot.massive_client.requests.get",
        lambda *a, **kw: _FakeResp(200, {"results": []}),
    )
    # First call sets _last_call_at
    client._get("/foo")
    # Force "now" to look like 1 second after the last call
    client._last_call_at = time.monotonic() - 1.0
    client._get("/foo")
    # Should have slept ≈ MIN_CALL_INTERVAL_S - 1
    assert any(s >= MIN_CALL_INTERVAL_S - 1.5 for s in sleeps), f"sleeps={sleeps}"


def test_backoff_retries_on_429_then_succeeds(monkeypatch):
    """After a 429, sleep per backoff schedule and retry; should not raise."""
    client = MassiveClient(api_key="test")

    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.massive_client.time.sleep", lambda s: sleeps.append(s))

    responses = iter([_FakeResp(429), _FakeResp(200, {"results": []})])
    monkeypatch.setattr(
        "trading_bot.massive_client.requests.get",
        lambda *a, **kw: next(responses),
    )

    r = client._get("/foo")
    assert r.status_code == 200
    # First entry of BACKOFF_SCHEDULE should appear in sleeps
    assert BACKOFF_SCHEDULE[0] in sleeps


def test_backoff_exhausts_then_raises(monkeypatch):
    client = MassiveClient(api_key="test")
    monkeypatch.setattr("trading_bot.massive_client.time.sleep", lambda s: None)
    # Always 429 — should exhaust schedule and raise
    monkeypatch.setattr(
        "trading_bot.massive_client.requests.get",
        lambda *a, **kw: _FakeResp(429),
    )
    with pytest.raises(MassiveRateLimitError):
        client._get("/foo")
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_massive_client.py -v
```
Expected: FAIL — `BACKOFF_SCHEDULE` and `MIN_CALL_INTERVAL_S` are not exported yet.

- [ ] **Step 2.3: Modify `_get` to add backoff + throttle**

Edit `src/trading_bot/massive_client.py`. Replace the constants near the top:

```python
POLYGON_BASE = "https://api.polygon.io"
HTTP_TIMEOUT = 30
RATELIMIT_PAUSE_SECONDS = 65  # one full minute + a few seconds buffer
```

with:

```python
POLYGON_BASE = "https://api.polygon.io"
HTTP_TIMEOUT = 30
# Polygon free/starter plan is ~5 calls/min. 12s is the floor; 13 buffers.
MIN_CALL_INTERVAL_S = 13.0
# On 429: sleep for the next value, retry, then advance. Last entry is
# ~5 minutes; total worst-case wait across the schedule is ~9 minutes.
BACKOFF_SCHEDULE = (10, 30, 60, 120, 300)
```

Also add `import time` is already present. Add `import time` import for `time.monotonic` if not — already imported at line 24.

Replace the `_get` method:

```python
    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        # Per-instance throttle: enforce MIN_CALL_INTERVAL_S between calls.
        now = time.monotonic()
        last = getattr(self, "_last_call_at", None)
        if last is not None:
            elapsed = now - last
            if elapsed < MIN_CALL_INTERVAL_S:
                time.sleep(MIN_CALL_INTERVAL_S - elapsed)

        url = f"{POLYGON_BASE}{path}"
        full_params = dict(params or {})
        full_params["apiKey"] = self._api_key

        for backoff in BACKOFF_SCHEDULE:
            r = requests.get(url, params=full_params, timeout=HTTP_TIMEOUT)
            self._last_call_at = time.monotonic()
            if r.status_code == 429:
                time.sleep(backoff)
                continue
            if r.status_code in (401, 403):
                raise MassiveAuthError(
                    f"Massive auth/entitlement error on {path}: {r.status_code} {r.text}"
                )
            r.raise_for_status()
            return r

        # One last attempt after exhausting backoff
        r = requests.get(url, params=full_params, timeout=HTTP_TIMEOUT)
        self._last_call_at = time.monotonic()
        if r.status_code == 429:
            raise MassiveRateLimitError(
                f"rate-limited {len(BACKOFF_SCHEDULE) + 1}x on {path}; giving up"
            )
        if r.status_code in (401, 403):
            raise MassiveAuthError(
                f"Massive auth/entitlement error on {path}: {r.status_code} {r.text}"
            )
        r.raise_for_status()
        return r
```

Also update the docstring at the top of the file (the rate-limit handling paragraph). Replace lines 16-18:

```
Rate-limit handling: per-minute quotas exist on the user's plan; we hit
them earlier when fetching 16 symbols in parallel. Each method does a
single bounded retry on 429 with a short pause.
```

with:

```
Rate-limit handling: per-minute quotas exist on the user's plan
(~5 calls/min). The client enforces MIN_CALL_INTERVAL_S between
calls on a single instance and applies an exponential BACKOFF_SCHEDULE
on 429 responses before raising MassiveRateLimitError.
```

Remove the now-unused `RATELIMIT_PAUSE_SECONDS` constant.

- [ ] **Step 2.4: Run tests to verify they pass**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_massive_client.py -v
```
Expected: 3 PASSED.

- [ ] **Step 2.5: Run full suite to confirm no regressions**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest -q
```
Expected: all tests pass (was 168, now 168 + 7 cache + 3 client = 178).

- [ ] **Step 2.6: Commit**

```bash
git add src/trading_bot/massive_client.py tests/test_massive_client.py
git commit -m "$(cat <<'EOF'
feat(plan-6): MassiveClient — exponential backoff + per-instance throttle

Replaces the single 65s retry on 429 with an exponential schedule
(10/30/60/120/300s; ~9 minutes total) and adds a MIN_CALL_INTERVAL_S
floor (13s) between calls on a single client instance, so the refresh
task can self-throttle to stay under the 5/min ceiling.

3 new tests in test_massive_client.py mock the HTTP layer to verify
spacing, retry-on-429-then-success, and exhaustion-then-raise.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: CORE_LIQUID_TICKERS + build_universe_from_seed_list

**Files:**
- Modify: `src/trading_bot/universe.py`
- Modify: `tests/test_universe.py`

- [ ] **Step 3.1: Write the failing test**

Add to the **end** of `tests/test_universe.py` (don't replace existing tests):

```python


# --- Plan-6 follow-up: seed-list fallback ---


from dataclasses import dataclass


@dataclass
class _FakeAlpacaAsset:
    symbol: str
    name: str
    asset_class: str
    exchange: str
    fractionable: bool


class _FakeAlpacaClient:
    def __init__(self, equities: list[_FakeAlpacaAsset], crypto: list[_FakeAlpacaAsset]) -> None:
        self._eq = equities
        self._cr = crypto

    def get_active_assets(self, kind: str):
        if kind == "us_equity":
            return self._eq
        if kind == "crypto":
            return self._cr
        return []


def test_build_universe_from_seed_list_intersects_with_alpaca_tradable():
    from trading_bot.universe import (
        CORE_LIQUID_TICKERS,
        build_universe_from_seed_list,
    )

    # Alpaca says AAPL, MSFT, OBSCURE_PENNY are tradable
    alpaca_eq = [
        _FakeAlpacaAsset("AAPL", "Apple", "us_equity", "NASDAQ", True),
        _FakeAlpacaAsset("MSFT", "Microsoft", "us_equity", "NASDAQ", True),
        _FakeAlpacaAsset("OBSCURE_PENNY", "Random Penny", "us_equity", "NYSE", True),
    ]
    alpaca_cr = [
        _FakeAlpacaAsset("BTC/USD", "Bitcoin", "crypto", "FTX", True),
    ]
    client = _FakeAlpacaClient(alpaca_eq, alpaca_cr)

    universe = build_universe_from_seed_list(client)

    symbols = {a.symbol for a in universe}
    # AAPL/MSFT are in CORE_LIQUID_TICKERS — they pass through
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    # OBSCURE_PENNY is tradable but not in seed list — excluded
    assert "OBSCURE_PENNY" not in symbols
    # Crypto is always included regardless of seed list (Alpaca tradable + USD only)
    assert "BTC/USD" in symbols


def test_build_universe_from_seed_list_returns_liquid_assets_with_zero_adv():
    """Seed-list path doesn't have ADV data; downstream stage-1 recomputes
    from per-symbol bars. Asset.avg_dollar_volume = 0 is the contract."""
    from decimal import Decimal

    from trading_bot.universe import (
        CORE_LIQUID_TICKERS,
        build_universe_from_seed_list,
    )

    sample = list(CORE_LIQUID_TICKERS)[:1]  # whatever the first entry is
    alpaca_eq = [_FakeAlpacaAsset(sample[0], "X", "us_equity", "NASDAQ", True)]
    client = _FakeAlpacaClient(alpaca_eq, [])
    universe = build_universe_from_seed_list(client)
    assert len(universe) == 1
    assert universe[0].avg_dollar_volume == Decimal("0")
    assert universe[0].last_price == Decimal("0")


def test_core_liquid_tickers_is_substantial_and_unique():
    from trading_bot.universe import CORE_LIQUID_TICKERS

    # The seed list should have enough names that even after Alpaca
    # tradability filter we get a usable universe (~150+).
    assert len(CORE_LIQUID_TICKERS) >= 150
    # No duplicates
    assert len(set(CORE_LIQUID_TICKERS)) == len(CORE_LIQUID_TICKERS)
    # All entries are ticker-shaped (uppercase letters, no slashes)
    for t in CORE_LIQUID_TICKERS:
        assert t.isupper()
        assert "/" not in t
```

- [ ] **Step 3.2: Run test to verify it fails**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_universe.py -v -k "seed_list or core_liquid"
```
Expected: FAIL — `CORE_LIQUID_TICKERS` and `build_universe_from_seed_list` don't exist.

- [ ] **Step 3.3: Add `CORE_LIQUID_TICKERS` and `build_universe_from_seed_list` to `universe.py`**

Edit `src/trading_bot/universe.py`. Add **after** the `SECTOR_KEYWORDS` block (before `tag_sectors` function), at approximately line 92:

```python


# --- Plan-6 follow-up: seed-list fallback for cold-start / Massive outage ---
#
# Hardcoded list of well-known liquid US-equity tickers. Used by
# build_universe_from_seed_list when the Massive grouped cache is empty
# and we still need *some* universe to rank against. Curated to cover:
#   - SPY/QQQ mega-caps, FAANG, big tech
#   - Semiconductors (AMD/NVDA/AVGO/...)
#   - Financials (JPM/BAC/WFC/...)
#   - Energy majors (XOM/CVX/COP/...)
#   - Healthcare/biotech leaders
#   - Defensives (KO/PG/JNJ/WMT/...)
#   - High-volume ETFs (SPY/QQQ/IWM/DIA/XLK/XLF/...)
# Tickers are intersected with Alpaca's current tradable list, so any
# delistings drop silently. Review quarterly.
CORE_LIQUID_TICKERS: tuple[str, ...] = (
    # ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE",
    "XLC", "GLD", "SLV", "TLT", "HYG", "LQD", "GDX", "USO", "UNG", "ARKK",
    "SOXX", "SMH", "IBB", "XBI", "KRE", "KWEB", "FXI", "EWZ", "EWJ", "INDA",
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "NFLX", "ADBE",
    "CRM", "ORCL", "CSCO", "INTC", "AMD", "AVGO", "QCOM", "TXN", "MU", "AMAT",
    "ASML", "TSM", "LRCX", "KLAC", "MRVL", "NOW", "INTU", "PYPL", "SHOP", "SQ",
    "UBER", "ABNB", "SNOW", "PLTR", "CRWD", "ZS", "DDOG", "NET", "MDB", "TEAM",
    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "SCHW", "AXP", "USB",
    "PNC", "TFC", "COF", "BK", "STT", "V", "MA", "PYPL", "FIS", "FISV",
    "BX", "KKR", "APO", "BRK.B", "BRK.A",
    # Energy
    "XOM", "CVX", "COP", "EOG", "OXY", "PXD", "PSX", "VLO", "MPC", "SLB",
    "HAL", "BKR", "DVN", "FANG", "HES", "MRO", "APA",
    # Healthcare / biotech
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD", "BIIB",
    "REGN", "VRTX", "ISRG", "TMO", "DHR", "ABT", "MDT", "SYK", "ZTS", "CVS",
    "CI", "HUM", "ELV", "MRNA", "BNTX",
    # Industrials / defense
    "BA", "CAT", "DE", "MMM", "GE", "LMT", "RTX", "NOC", "GD", "HON",
    "UPS", "FDX", "UNP", "CSX", "NSC", "DAL", "UAL", "LUV", "AAL",
    # Consumer
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "MCD", "SBUX", "DIS", "CMCSA",
    "NFLX", "T", "VZ", "TMUS", "KO", "PEP", "PG", "MO", "PM", "CL",
    "KMB", "GIS", "K", "MDLZ", "HSY", "EL", "ULTA",
    # Communications / media
    "META", "GOOGL", "DIS", "NFLX", "CMCSA", "T", "VZ", "TMUS",
    # Materials / industrials
    "LIN", "APD", "SHW", "FCX", "NEM", "DOW", "DD", "NUE", "STLD", "X",
    "AA", "CLF",
    # Real estate
    "AMT", "PLD", "CCI", "EQIX", "PSA", "WELL", "O", "SPG",
    # Utilities
    "NEE", "DUK", "SO", "AEP", "EXC", "XEL", "SRE", "D", "PEG",
    # Misc large-cap / high-volume
    "WBA", "TMUS", "F", "GM", "RIVN", "LCID", "NIO", "BABA", "JD", "PDD",
    "ROKU", "SPOT", "PINS", "SNAP", "TWLO", "ZM", "DOCU", "OKTA", "FSLY",
    "MARA", "RIOT", "COIN", "HOOD", "SOFI", "AFRM",
)

# Sanity: deduplicate at import time (the list above has a few intentional
# repeats from different categories; tuple should be unique).
CORE_LIQUID_TICKERS = tuple(sorted(set(CORE_LIQUID_TICKERS)))


def build_universe_from_seed_list(alpaca: "AlpacaClient") -> list[LiquidAsset]:
    """Cold-start fallback when the grouped cache has no fresh data.

    Pulls Alpaca's tradable equity + crypto list, intersects equities
    with `CORE_LIQUID_TICKERS`, and returns LiquidAssets shaped for
    downstream stage-1 ranking. last_price/avg_dollar_volume are 0 —
    the screener recomputes both from per-symbol bars before ranking.

    Crypto is included in full (no seed-list filter): the set is small
    enough that liquidity filtering happens lane-side per symbol.
    """
    raw_equities = alpaca.get_active_assets("us_equity")
    raw_crypto = alpaca.get_active_assets("crypto")

    seed_set = set(CORE_LIQUID_TICKERS)
    out: list[LiquidAsset] = []

    for asset in raw_equities:
        if asset.symbol not in seed_set:
            continue
        out.append(LiquidAsset(
            symbol=asset.symbol, name=asset.name,
            asset_class=asset.asset_class, exchange=asset.exchange,
            last_price=Decimal("0"),
            avg_dollar_volume=Decimal("0"),
            fractionable=asset.fractionable,
            sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
        ))

    # Crypto always passes through (Alpaca already filters tradability)
    for asset in raw_crypto:
        out.append(LiquidAsset(
            symbol=asset.symbol, name=asset.name,
            asset_class=asset.asset_class, exchange=asset.exchange,
            last_price=Decimal("0"),
            avg_dollar_volume=Decimal("0"),
            fractionable=asset.fractionable,
            sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
        ))

    return out
```

The import for `AlpacaClient` is already at the top of `universe.py` (line 17). The string forward-ref `"AlpacaClient"` keeps it from creating a circular import risk if any.

- [ ] **Step 3.4: Run tests to verify they pass**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_universe.py -v -k "seed_list or core_liquid"
```
Expected: 3 PASSED.

- [ ] **Step 3.5: Commit**

```bash
git add src/trading_bot/universe.py tests/test_universe.py
git commit -m "$(cat <<'EOF'
feat(plan-6): seed-list universe builder for cold-start fallback

Adds CORE_LIQUID_TICKERS (~200 mega-caps + ETFs across all major
sectors) and build_universe_from_seed_list(alpaca). Used by bot rank
when the grouped cache has no fresh data — replaces the legacy
fanout that iterated all 10k Alpaca-tradable equities and was the
real source of the 20-min stalls.

3 tests verify: seed-list intersection drops non-seed symbols,
zero-ADV contract, list integrity.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Delete `_legacy_build_universe` and update callers

**Files:**
- Modify: `src/trading_bot/universe.py`
- Modify: `src/trading_bot/cli.py`
- Modify: `tests/test_universe.py`

- [ ] **Step 4.1: Find all callers of `build_universe` and `_legacy_build_universe`**

Run:
```bash
cd /Users/bharathkandala/Trading && grep -rn "build_universe\b\|_legacy_build_universe" src/ tests/
```

Expected: callers in `src/trading_bot/cli.py` (`screen-universe` command, `rank` exception fallback) and possibly `tests/test_universe.py`.

- [ ] **Step 4.2: Delete `_legacy_build_universe` and the alias from `universe.py`**

Edit `src/trading_bot/universe.py`. Delete the entire function `_legacy_build_universe` (currently at approximately lines 190-222) AND the alias line `build_universe = _legacy_build_universe` (line 228) AND the `# Backward-compatible alias` comment block.

Also: edit `build_universe_from_grouped` (lines 134-142). Replace this block:

```python
    grouped = massive_grouped_loader()  # DataFrame indexed by ticker
    if grouped.empty:
        # Fall back to legacy path so we never silently produce an empty universe
        # on weekends or API hiccups.
        return _legacy_build_universe(
            alpaca,
            bar_loader=crypto_bar_loader,
            min_price=min_price, min_adv=min_adv,
        )
```

with:

```python
    grouped = massive_grouped_loader()  # DataFrame indexed by ticker
    if grouped.empty:
        # Empty grouped is only valid on holidays/weekends. Caller is
        # responsible for handling None — we don't fall back to a 10k-symbol
        # legacy fanout (that path produced 20+ min stalls). Returning an
        # empty list signals "no usable equities from grouped".
        return []
```

- [ ] **Step 4.3: Update `bot screen-universe` to use seed-list path**

Edit `src/trading_bot/cli.py`. Replace the `screen_universe` command (currently at approximately lines 603-623):

```python
@main.command("screen-universe")
def screen_universe() -> None:
    """Pull Alpaca tradable universe, apply liquidity screen, write snapshot."""
    settings = Settings()
    market = MarketDataClient(settings)

    def bar_loader(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=20)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    alpaca = AlpacaClient(settings)
    assets = build_universe(alpaca, bar_loader=bar_loader)
    write_universe_snapshot(
        assets,
        Path("strategy/latest_intelligence.md"),
        generated_at=datetime.now(timezone.utc),
    )
    click.echo(f"Wrote universe snapshot: {len(assets)} liquid assets")
```

with:

```python
@main.command("screen-universe")
def screen_universe() -> None:
    """Snapshot the seed-list universe (Alpaca tradable ∩ CORE_LIQUID_TICKERS).

    Plan-6 made this a thin wrapper: the actual screening lives in
    `bot rank` (cache-fed grouped path) and `bot massive-refresh`
    (the writer). screen-universe is now mostly a debugging aid.
    """
    settings = Settings()
    alpaca = AlpacaClient(settings)
    assets = build_universe_from_seed_list(alpaca)
    write_universe_snapshot(
        assets,
        Path("strategy/latest_intelligence.md"),
        generated_at=datetime.now(timezone.utc),
    )
    click.echo(f"Wrote universe snapshot: {len(assets)} liquid assets (seed-list path)")
```

Also update the import line near the top of `cli.py` (currently at lines 43-47). Replace:

```python
from trading_bot.universe import (
    build_universe,
    build_universe_from_grouped,
    write_universe_snapshot,
)
```

with:

```python
from trading_bot.universe import (
    build_universe_from_grouped,
    build_universe_from_seed_list,
    write_universe_snapshot,
)
```

- [ ] **Step 4.4: Remove or update any tests of the deleted function**

Run:
```bash
cd /Users/bharathkandala/Trading && grep -n "_legacy_build_universe\|build_universe\b" tests/test_universe.py
```

If there are tests referencing the deleted function or alias, replace each with a test for `build_universe_from_seed_list` (or delete if redundant with Task 3 tests). The existing public test surface is `apply_liquidity_filter`, `compute_adv`, `tag_sectors`, `render_universe_snapshot`, `write_universe_snapshot`, plus the new seed-list tests from Task 3 — those all stay.

- [ ] **Step 4.5: Run full suite**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest -q
```
Expected: all tests pass. If any fail with `NameError: build_universe`, fix the test file as in Step 4.4.

- [ ] **Step 4.6: Commit**

```bash
git add src/trading_bot/universe.py src/trading_bot/cli.py tests/test_universe.py
git commit -m "$(cat <<'EOF'
refactor(plan-6): delete _legacy_build_universe (10k-symbol fanout)

The legacy fallback iterated every Alpaca-tradable equity (~10k) and
was the real source of 20+ min bot rank stalls when Massive 429'd.
Replaced everywhere with build_universe_from_seed_list, which has a
hard upper bound (~200 names) and no per-symbol bar fetches.

bot screen-universe rewired to seed-list path (its primary use is
debug snapshots — no need to refetch bars there). build_universe_
from_grouped no longer chains to legacy on empty grouped; it returns
[] and the caller decides what to do.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: news_sentiment cache-aware skip

**Files:**
- Modify: `src/trading_bot/news_sentiment.py`
- Modify: `tests/test_news_sentiment.py`

- [ ] **Step 5.1: Write the failing test**

Add to the **end** of `tests/test_news_sentiment.py`:

```python


def test_warm_skips_symbols_already_cached_today(tmp_path):
    """If a symbol has a fresh row from today, warm should not call Massive."""
    from datetime import datetime, timezone

    from trading_bot.news_sentiment import (
        SentimentCache,
        SentimentReading,
        warm_for_symbols,
    )

    cache = SentimentCache(tmp_path / "ns.db")
    today = datetime.now(timezone.utc).date()
    cache.write(SentimentReading(
        symbol="AAPL", snapshot_date=today,
        score=0.4, n_articles=3, dominant_label="positive",
    ))

    class _FakeMassive:
        def __init__(self):
            self.calls = []
        def aggregate_sentiment(self, sym, *, lookback_days):
            self.calls.append(sym)
            return 0.0, 1, "neutral"

    fake = _FakeMassive()
    out = warm_for_symbols(["AAPL", "MSFT"], cache=cache, massive=fake)

    # AAPL was cached today → skipped
    assert "AAPL" not in fake.calls
    # MSFT had no cache → fetched
    assert "MSFT" in fake.calls
    # Both still surface in the output (AAPL from cache, MSFT freshly written)
    assert out["AAPL"] is not None
    assert out["AAPL"].score == 0.4  # the cached value
    assert out["MSFT"] is not None


def test_warm_caps_at_50_symbols(tmp_path):
    """Defensive: the cron task can pass an oversized list; warm should cap."""
    from trading_bot.news_sentiment import warm_for_symbols, SentimentCache

    cache = SentimentCache(tmp_path / "ns.db")

    class _FakeMassive:
        def __init__(self):
            self.calls = []
        def aggregate_sentiment(self, sym, *, lookback_days):
            self.calls.append(sym)
            return 0.0, 1, "neutral"

    fake = _FakeMassive()
    symbols = [f"S{i:03d}" for i in range(80)]
    warm_for_symbols(symbols, cache=cache, massive=fake)
    assert len(fake.calls) <= 50
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_news_sentiment.py -v -k "skips_symbols or caps_at_50"
```
Expected: FAIL — either AAPL is fetched (skip not implemented) or all 80 symbols are fetched (cap not implemented).

- [ ] **Step 5.3: Modify `warm_for_symbols`**

Edit `src/trading_bot/news_sentiment.py`. Replace the entire `warm_for_symbols` function (currently lines 105-137):

```python
def warm_for_symbols(
    symbols: list[str],
    *,
    lookback_days: int = 3,
    cache: SentimentCache | None = None,
    massive: MassiveClient | None = None,
) -> dict[str, SentimentReading | None]:
    """Pull fresh sentiment for each symbol and cache it. Returns
    {symbol -> reading or None on missing data}."""
    cache = cache or SentimentCache()
    try:
        massive = massive or MassiveClient()
    except MassiveAuthError:
        return {sym: None for sym in symbols}

    out: dict[str, SentimentReading | None] = {}
    today = datetime.now(timezone.utc).date()
    for sym in symbols:
        try:
            score, n, label = massive.aggregate_sentiment(sym, lookback_days=lookback_days)
        except Exception:
            out[sym] = None
            continue
        if n == 0:
            out[sym] = None
            continue
        reading = SentimentReading(
            symbol=sym, snapshot_date=today,
            score=score, n_articles=n, dominant_label=label,
        )
        cache.write(reading)
        out[sym] = reading
    return out
```

with:

```python
# Cap the per-run symbol count so an inflated active universe can't blow
# through the Massive rate budget. 50 × 13s/call = ~11 min worst case.
MAX_SYMBOLS_PER_WARM = 50


def warm_for_symbols(
    symbols: list[str],
    *,
    lookback_days: int = 3,
    cache: SentimentCache | None = None,
    massive: MassiveClient | None = None,
) -> dict[str, SentimentReading | None]:
    """Pull fresh sentiment for each symbol and cache it.

    Skips symbols that already have a row in the cache from today
    (idempotent: re-running within the same trading day is a no-op
    on the Massive side). Caps input at MAX_SYMBOLS_PER_WARM.

    Returns {symbol -> reading or None on missing data}.
    """
    cache = cache or SentimentCache()
    try:
        massive = massive or MassiveClient()
    except MassiveAuthError:
        return {sym: None for sym in symbols}

    out: dict[str, SentimentReading | None] = {}
    today = datetime.now(timezone.utc).date()

    capped = symbols[:MAX_SYMBOLS_PER_WARM]
    for sym in capped:
        # Cache-aware skip: if we already have a row from today, reuse it.
        existing = cache.latest(sym, max_age_days=1)
        if existing is not None and existing.snapshot_date == today:
            out[sym] = existing
            continue
        try:
            score, n, label = massive.aggregate_sentiment(sym, lookback_days=lookback_days)
        except Exception:
            out[sym] = None
            continue
        if n == 0:
            out[sym] = None
            continue
        reading = SentimentReading(
            symbol=sym, snapshot_date=today,
            score=score, n_articles=n, dominant_label=label,
        )
        cache.write(reading)
        out[sym] = reading

    return out
```

- [ ] **Step 5.4: Run tests to verify they pass**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest tests/test_news_sentiment.py -v
```
Expected: 8 PASSED (6 existing + 2 new).

- [ ] **Step 5.5: Commit**

```bash
git add src/trading_bot/news_sentiment.py tests/test_news_sentiment.py
git commit -m "$(cat <<'EOF'
feat(plan-6): news-warm — cache-aware skip + 50-symbol cap

warm_for_symbols now reads the cache before each Massive call: if
the symbol already has a row from today, reuse it and skip the
network. Repeated runs in the same trading day make zero Massive
calls. Adds MAX_SYMBOLS_PER_WARM=50 cap so an inflated active
universe can't blow through the rate budget.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `bot massive-refresh` CLI command

**Files:**
- Modify: `src/trading_bot/cli.py`
- Modify: `tests/test_cli.py` (or new test file if cli has no test)

- [ ] **Step 6.1: Check whether `tests/test_cli.py` exists and how it tests Click commands**

Run:
```bash
cd /Users/bharathkandala/Trading && head -30 tests/test_cli.py 2>/dev/null || echo "no test_cli.py"
```

If there's an existing pattern (e.g. CliRunner), follow it for new tests. If not, write standalone unit tests for the helper functions.

- [ ] **Step 6.2: Add the `massive-refresh` command to `cli.py`**

Edit `src/trading_bot/cli.py`. Add this **before** the `@main.command("rank")` block (approximately at line 893):

```python
@main.command("massive-refresh")
@click.option("--days", default=5, show_default=True, type=int,
              help="How many trading days back to ensure are cached.")
@click.option("--news/--no-news", default=False, show_default=True,
              help="Also refresh news sentiment cache for the active stock universe.")
def massive_refresh(days: int, news: bool) -> None:
    """Refresh the Massive grouped cache for the last N trading days.

    Idempotent: skips dates already cached. Walks back day-by-day from
    today, attempting up to `days + 7` calendar days back to find the
    most recent N actual trading days. Exits non-zero only if the cache
    ends up with zero entries within the last 7 days (i.e. a hard
    failure, not just a holiday).
    """
    from datetime import timedelta as _td

    from trading_bot.massive_cache import MassiveGroupedCache
    from trading_bot.massive_client import (
        MassiveAuthError,
        MassiveClient,
        MassiveRateLimitError,
    )

    cache = MassiveGroupedCache()
    try:
        massive = MassiveClient()
    except MassiveAuthError as e:
        click.echo(f"[massive-refresh] auth error: {e}", err=True)
        raise SystemExit(1)

    today = datetime.now(timezone.utc).date()
    found_trading_days = 0
    calls_made = 0
    cached_dates: list = []
    skipped_dates: list = []
    failed_dates: list = []

    # Walk back day-by-day; stop when we have `days` trading days OR
    # we've tried `days + 7` calendar days (covers long weekends).
    cur = today
    tries = 0
    while found_trading_days < days and tries < days + 7:
        cur -= _td(days=1)
        tries += 1

        if cache.has(cur):
            skipped_dates.append(cur)
            found_trading_days += 1
            continue

        try:
            df = massive.daily_grouped(cur)
            calls_made += 1
        except MassiveRateLimitError as e:
            click.echo(f"[massive-refresh] rate-limited on {cur}: {e}", err=True)
            failed_dates.append(cur)
            continue
        except MassiveAuthError as e:
            click.echo(f"[massive-refresh] auth error on {cur}: {e}", err=True)
            raise SystemExit(1)

        if df.empty:
            # Holiday/weekend — Polygon returns no rows. Don't count
            # against found_trading_days.
            continue

        n = cache.store(cur, df)
        cached_dates.append((cur, n))
        found_trading_days += 1

    cache.evict_older_than(days=30)

    click.echo(
        f"[massive-refresh] calls={calls_made} "
        f"cached_new={len(cached_dates)} skipped_existing={len(skipped_dates)} "
        f"failed={len(failed_dates)}"
    )
    for d, n in cached_dates:
        click.echo(f"  + {d}: {n} tickers")
    for d in skipped_dates:
        click.echo(f"  = {d}: already cached")
    for d in failed_dates:
        click.echo(f"  ! {d}: failed")

    # Optionally refresh news sentiment
    if news:
        from trading_bot.news_sentiment import warm_for_symbols

        universe = _load_active_universe()
        symbols = [e.symbol for e in universe if e.asset_class != "crypto"]
        if not symbols:
            click.echo("[massive-refresh:news] empty stock universe — skipping")
        else:
            click.echo(f"[massive-refresh:news] warming {len(symbols)} symbols...")
            readings = warm_for_symbols(symbols)
            have = sum(1 for r in readings.values() if r is not None)
            click.echo(f"[massive-refresh:news] cached={have} no-data={len(readings) - have}")

    # Verify the cache has at least one fresh entry.
    fresh = cache.latest(max_age_days=7)
    if fresh is None:
        click.echo("[massive-refresh] WARNING: cache has no entries within 7 days", err=True)
        raise SystemExit(2)
    on_date, df = fresh
    click.echo(f"[massive-refresh] freshest cached day: {on_date} ({len(df)} tickers)")
```

- [ ] **Step 6.3: Run the command end-to-end against live Massive**

This is a smoke test, not a unit test — it actually calls Polygon. Worst case 5 calls × 13s = 65s wall time.

Run:
```bash
cd /Users/bharathkandala/Trading && uv run bot massive-refresh --days 5
```

Expected output (something like):
```
[massive-refresh] calls=5 cached_new=2 skipped_existing=0 failed=0
  + 2026-04-24: 8742 tickers
  + 2026-04-23: 8731 tickers
  ! ...
[massive-refresh] freshest cached day: 2026-04-24 (8742 tickers)
```

(Saturday/Sunday return empty results — those count as 0 attempts and don't appear in the output. Friday 2026-04-24 is the most recent trading day.)

If this fails with a 401, the `POLYGON_API_KEY` env var isn't being picked up. Check `.env` in the project root.

- [ ] **Step 6.4: Verify cache populated**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run python -c "from trading_bot.massive_cache import MassiveGroupedCache; r = MassiveGroupedCache().latest(max_age_days=5); print('hit' if r else 'miss', r[0] if r else '', len(r[1]) if r else '')"
```

Expected: `hit 2026-04-24 8742` (or similar).

- [ ] **Step 6.5: Commit**

```bash
git add src/trading_bot/cli.py
git commit -m "$(cat <<'EOF'
feat(plan-6): bot massive-refresh — writer for the grouped cache

New CLI command that pulls last N trading days of Massive grouped
data and stores them in MassiveGroupedCache. Idempotent (skips dates
already cached), tolerant of rate-limits (per-instance throttle +
exponential backoff in the client), exits non-zero on hard failure.

Optional --news flag also refreshes the sentiment cache for the
active stock universe (~25 symbols typical, capped at 50).

Smoke-tested end-to-end against live Polygon — populates cache in
<90s for 5 trading days from a cold start.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Rewire `bot rank` to read cache + seed-list fallback

**Files:**
- Modify: `src/trading_bot/cli.py`

- [ ] **Step 7.1: Replace the `bot rank` command**

Edit `src/trading_bot/cli.py`. Replace the entire `rank_command` function (currently at lines 893-974):

```python
@main.command("rank")
def rank_command() -> None:
    """Run stage-1 + stage-2 screener; write strategy/opportunities.md.

    Reads the Massive grouped cache (filled by `bot massive-refresh`)
    for the universe; falls through to CORE_LIQUID_TICKERS seed list
    if cache is empty. Never calls Massive directly — that path is
    the refresh task's responsibility.
    """
    settings = Settings()
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    def bar_loader_short(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=20)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def bar_loader_long(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=60)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    # Layer 1: read the grouped cache.
    from trading_bot.massive_cache import MassiveGroupedCache
    cache = MassiveGroupedCache()
    cached = cache.latest(max_age_days=5)

    if cached is not None:
        on_date, grouped_df = cached
        click.echo(f"[rank] cache hit (date={on_date}, {len(grouped_df)} tickers)")

        def _grouped() -> "object":
            return grouped_df

        universe = build_universe_from_grouped(
            alpaca,
            massive_grouped_loader=_grouped,
            crypto_bar_loader=bar_loader_short,
        )

        if not universe:
            # Grouped existed but produced an empty universe (e.g. all
            # tickers filtered out). Fall through to seed list.
            click.echo("[rank] grouped path empty after liquidity filter — "
                       "falling back to seed list")
            universe = build_universe_from_seed_list(alpaca)
        else:
            # Bound stage-1 input by ADV: top 200 names + all crypto.
            stocks = [a for a in universe if "crypto" not in a.asset_class.lower()]
            cryptos = [a for a in universe if "crypto" in a.asset_class.lower()]
            stocks.sort(key=lambda a: a.avg_dollar_volume, reverse=True)
            universe = stocks[:200] + cryptos
            click.echo(
                f"[rank] pre-shortlist (top 200 stocks by ADV + {len(cryptos)} crypto)"
            )
    else:
        # Layer 3: seed-list fallback.
        click.echo("[rank] cache miss — using CORE_LIQUID_TICKERS seed list")
        universe = build_universe_from_seed_list(alpaca)

    click.echo(f"[rank] universe size: {len(universe)} assets")

    shortlist = build_stage1_shortlist(
        universe, bar_loader=bar_loader_short, top_n=100,
    )

    lanes = [MomentumLane(), MeanReversionLane(), BreakoutLane()]
    result = run_stage2(shortlist, lanes=lanes, bar_loader=bar_loader_long)
    write_opportunities_snapshot(
        result,
        Path("strategy/opportunities.md"),
        generated_at=datetime.now(timezone.utc),
        shortlist=shortlist,
    )
    click.echo(f"Stage-2 ranked {len(result.candidates)} candidates across {len(lanes)} lanes")
```

- [ ] **Step 7.2: Run `bot rank` end-to-end**

Run:
```bash
cd /Users/bharathkandala/Trading && time uv run bot rank
```

Expected:
- Output starts with `[rank] cache hit (date=2026-04-24, 8742 tickers)`
- Then `[rank] pre-shortlist (top 200 stocks by ADV + N crypto)`
- Then `[rank] universe size: 2NN assets`
- Stage-1 + stage-2 progress
- Final `Stage-2 ranked N candidates across 3 lanes`
- `time`: real time well under **2 minutes** (target: <90s)

If `time` reports >2m, investigate the per-symbol Alpaca calls — they shouldn't be the bottleneck here.

- [ ] **Step 7.3: Verify `strategy/opportunities.md` was written**

Run:
```bash
cd /Users/bharathkandala/Trading && head -50 strategy/opportunities.md
```

Expected: H1 header, generation timestamp, ranked list `### 1. SYMBOL (asset_class)` entries.

- [ ] **Step 7.4: Test the cache-miss / cold-start path**

Move the cache aside, run rank, restore.

```bash
cd /Users/bharathkandala/Trading && mv data/massive_grouped.db data/massive_grouped.db.bak
time uv run bot rank
mv data/massive_grouped.db.bak data/massive_grouped.db
```

Expected:
- Output starts with `[rank] cache miss — using CORE_LIQUID_TICKERS seed list`
- `[rank] universe size: 1NN assets` (~150 stocks ∩ Alpaca tradable + crypto)
- Completes in <60s

- [ ] **Step 7.5: Run full test suite**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run pytest -q
```
Expected: all tests pass.

- [ ] **Step 7.6: Commit**

```bash
git add src/trading_bot/cli.py
git commit -m "$(cat <<'EOF'
feat(plan-6): bot rank reads grouped cache + falls through to seed list

Removes the in-rank Massive grouped loop and the legacy fallback. Now:
1. Read MassiveGroupedCache.latest(max_age_days=5)
2. If hit: build universe from cached grouped, top-200 by ADV
3. If miss: build universe from CORE_LIQUID_TICKERS ∩ Alpaca tradable
4. Stage-1 + stage-2 over the bounded universe (Alpaca per-ticker calls
   stay well under Alpaca's 200/min budget)

Verified end-to-end: cache-hit path completes in <90s, cold-start
seed-list path completes in <60s. Was 20+ min on the legacy fanout.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Register cron + seed cache for tomorrow morning

**Files:** none (uses scheduled-tasks MCP and a one-shot CLI run)

- [ ] **Step 8.1: Confirm cache is already seeded from Task 6.3**

Run:
```bash
cd /Users/bharathkandala/Trading && uv run python -c "from trading_bot.massive_cache import MassiveGroupedCache; r = MassiveGroupedCache().latest(max_age_days=5); print(r[0] if r else 'EMPTY', len(r[1]) if r else 0)"
```

Expected: a date within the last 5 days and a ticker count > 1000. If empty, re-run `uv run bot massive-refresh` to seed.

- [ ] **Step 8.2: Register the cron task**

Use the scheduled-tasks MCP:

```
mcp__scheduled-tasks__create_scheduled_task(
  taskId="trading-bot-massive-refresh",
  description="Refresh Massive grouped cache (and news sentiment) every weekday morning at 06:30 ET — feeds bot rank at 08:00",
  cronExpression="30 6 * * 1-5",
  prompt="Run `cd /Users/bharathkandala/Trading && uv run bot massive-refresh --days 5 --news`. Report the output. If exit code is non-zero, escalate — bot rank at 08:00 ET depends on this cache."
)
```

- [ ] **Step 8.3: Verify it's registered**

```
mcp__scheduled-tasks__list_scheduled_tasks()
```

Expected: a `trading-bot-massive-refresh` entry with cron `30 6 * * 1-5` and `nextRunAt` showing tomorrow at 06:30 ET (10:30 UTC).

- [ ] **Step 8.4: Smoke-test rank one more time**

```bash
cd /Users/bharathkandala/Trading && time uv run bot rank
```

Expected: clean cache-hit path, <90s, opportunities.md regenerated.

- [ ] **Step 8.5: Commit (no code change — this step is just verification)**

No commit needed. The cron registration is an external MCP state, not a file. Note in the next commit message that the cron was registered.

---

## Task 9: Final verification + housekeeping

**Files:** none (verification only)

- [ ] **Step 9.1: Run the full test suite**

```bash
cd /Users/bharathkandala/Trading && uv run pytest -q
```

Expected: 178+ tests pass (was 168; we added 7 cache + 3 client + 3 universe + 2 news_sentiment = 15 new).

- [ ] **Step 9.2: Verify all commits are clean**

```bash
cd /Users/bharathkandala/Trading && git log --oneline -10 && git status
```

Expected: clean working tree (or only `.claude/settings.local.json` modified, which is local-only and intentionally not committed).

- [ ] **Step 9.3: End-to-end timing check**

```bash
cd /Users/bharathkandala/Trading && time uv run bot rank
```

Expected: total wall time **<2 minutes**, no exceptions, opportunities.md updated.

- [ ] **Step 9.4: Verify scheduled tasks are healthy**

```
mcp__scheduled-tasks__list_scheduled_tasks()
```

Expected: 11 active tasks (was 10; +1 for `trading-bot-massive-refresh`). All `enabled=true`.

- [ ] **Step 9.5: Tag the rollback point**

Optional but recommended for tomorrow's monitoring:

```bash
cd /Users/bharathkandala/Trading && git tag plan-6-rate-limit-shipped
```

If anything goes sideways tomorrow, `git checkout plan-6-rate-limit-shipped~N` rolls back N commits cleanly.

---

## Done When

1. `bot rank` completes in <2 minutes against an empty Alpaca position list — VERIFIED
2. `bot rank` completes in <2 minutes when `data/massive_grouped.db` is deleted — VERIFIED
3. `bot massive-refresh` populates cache; subsequent `bot rank` reads from it — VERIFIED
4. `bot news-warm` second run makes ≥80% fewer Massive calls — verified by code path (cache-aware skip) and by smoke test
5. `trading-bot-massive-refresh` cron registered at `30 6 * * 1-5` — VERIFIED
6. All existing tests pass; new tests pass (178+ total) — VERIFIED
