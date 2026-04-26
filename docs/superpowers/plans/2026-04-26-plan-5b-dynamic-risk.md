# Plan 5b — Dynamic Position Sizing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a multiplier-based sizing layer between `strategy.evaluate` and `risk_manager.check`, driven by signal conviction, per-asset-class ATR, and sector-overlap correlation.

**Architecture:** New module `position_sizer.py` consumes ranked watchlist entries (extended with `conviction` + `sector_tags`) and a sector map; outputs a `SizingResult` with the final qty and multiplier breakdown. Risk manager remains an unmodified pass/fail gate. Orchestrator gains one call site.

**Tech Stack:** Python 3.11+, pydantic, pandas, pytest. No new dependencies.

**Reference spec:** [2026-04-26-plan-5b-dynamic-risk-design.md](../specs/2026-04-26-plan-5b-dynamic-risk-design.md)

**Implementation note (deviation from spec):** The spec mentioned a new `sizing_breakdown` field on `TradeRecord`. The journal is SQLAlchemy-backed and adding a column requires a schema migration, which is out of proportion for an observability nice-to-have. This plan instead **prepends the sizing breakdown into the existing `notes` field** of `TradeRecord` (e.g. `"[size: conv=1.32×vol=0.95×corr=0.77→0.97] strategy reason"`). The structured fields still live on `Decision` for in-memory inspection. If a column proves needed later, it gets a dedicated migration plan.

**File map:**

- Create: `src/trading_bot/position_sizer.py`
- Create: `tests/test_position_sizer.py`
- Modify: `src/trading_bot/state.py` — extend `WatchlistEntry`
- Modify: `tests/test_state.py` — back-compat assertions
- Modify: `src/trading_bot/config.py` — add `SizingConfig`
- Modify: `tests/test_config.py` — defaults validate
- Modify: `src/trading_bot/market_data.py` — add `atr()`
- Modify: `tests/test_market_data.py` — atr tests
- Modify: `src/trading_bot/orchestrator.py` — extend `Decision`, parse conviction+sectors in `load_ranked_watchlist`, wire sizer
- Modify: `tests/test_orchestrator.py` — integration assertions

---

## Task 1 — Extend `WatchlistEntry` with `conviction` + `sector_tags`

**Files:**
- Modify: `src/trading_bot/state.py:10-14`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state.py`:

```python
def test_watchlist_entry_defaults_preserve_back_compat():
    from trading_bot.state import WatchlistEntry
    e = WatchlistEntry(symbol="AAPL", asset_class="us_equity", notes="")
    assert e.conviction is None
    assert e.sector_tags == ()


def test_watchlist_entry_accepts_conviction_and_sectors():
    from trading_bot.state import WatchlistEntry
    e = WatchlistEntry(
        symbol="AAPL", asset_class="us_equity", notes="",
        conviction=0.75, sector_tags=("tech", "ai"),
    )
    assert e.conviction == 0.75
    assert e.sector_tags == ("tech", "ai")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_state.py::test_watchlist_entry_defaults_preserve_back_compat tests/test_state.py::test_watchlist_entry_accepts_conviction_and_sectors -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'conviction'` (or AttributeError reading the field).

- [ ] **Step 3: Add the fields**

Edit `src/trading_bot/state.py` lines 10-14, replacing the dataclass body:

```python
@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    asset_class: str
    notes: str
    conviction: float | None = None
    sector_tags: tuple[str, ...] = ()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_state.py -v`

Expected: PASS — including all existing state tests (back-compat).

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/state.py tests/test_state.py
git commit -m "feat(state): extend WatchlistEntry with optional conviction + sector_tags"
```

---

## Task 2 — Parse `conviction` + `sector_tags` in `load_ranked_watchlist`

**Files:**
- Modify: `src/trading_bot/orchestrator.py:41-62`
- Modify: `tests/test_orchestrator.py`

The current parser only captures the symbol and asset_class header. We need it to also capture the `- Conviction: 0.85` and `- Sectors: tech, ai` lines that follow each `### N. SYMBOL (CLASS)` block.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py` (use `tmp_path` fixture):

```python
def test_load_ranked_watchlist_parses_conviction_and_sectors(tmp_path):
    from trading_bot.orchestrator import load_ranked_watchlist
    md = tmp_path / "opportunities.md"
    md.write_text(
        "# Opportunities\n\n"
        "## Ranked Candidates\n\n"
        "### 1. NVDA (us_equity)\n\n"
        "- Lanes: momentum\n"
        "- Conviction: 0.85\n"
        "- Stage-1 score: 12.3\n"
        "- Last price: $500.00\n"
        "- Sectors: tech, ai\n"
        "- Why: rsi=62\n\n"
        "### 2. BTC/USD (crypto)\n\n"
        "- Lanes: breakout\n"
        "- Conviction: 0.40\n"
        "- Stage-1 score: 8.0\n"
        "- Last price: $60000\n\n"
    )
    wl = load_ranked_watchlist(md)
    assert len(wl) == 2
    assert wl[0].symbol == "NVDA"
    assert wl[0].conviction == 0.85
    assert wl[0].sector_tags == ("tech", "ai")
    assert wl[1].symbol == "BTC/USD"
    assert wl[1].asset_class == "crypto"
    assert wl[1].conviction == 0.40
    assert wl[1].sector_tags == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_load_ranked_watchlist_parses_conviction_and_sectors -v`

Expected: FAIL — `wl[0].conviction == 0.85` is `None`.

- [ ] **Step 3: Update the parser**

Replace `load_ranked_watchlist` in `src/trading_bot/orchestrator.py` (lines 41-62) with:

```python
def load_ranked_watchlist(path: Path) -> list[WatchlistEntry]:
    """Parse strategy/opportunities.md and return WatchlistEntry list in rank order.

    Captures the symbol/asset_class from each ### N. SYMBOL (CLASS) header and
    the trailing `- Conviction:` / `- Sectors:` lines that belong to that block.
    """
    if not path.exists():
        return []
    text = path.read_text()
    out: list[WatchlistEntry] = []

    header_re = re.compile(r"^###\s+\d+\.\s+(\S+)\s+\(([^)]+)\)\s*$", re.MULTILINE)
    headers = list(header_re.finditer(text))
    for i, m in enumerate(headers):
        symbol = m.group(1)
        asset_class_raw = m.group(2)
        asset_class = "crypto" if "crypto" in asset_class_raw.lower() else asset_class_raw
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[m.end():block_end]

        conviction: float | None = None
        sector_tags: tuple[str, ...] = ()
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("- Conviction:"):
                try:
                    conviction = float(line.split(":", 1)[1].strip())
                except ValueError:
                    conviction = None
            elif line.startswith("- Sectors:"):
                payload = line.split(":", 1)[1].strip()
                if payload:
                    sector_tags = tuple(t.strip() for t in payload.split(",") if t.strip())

        out.append(WatchlistEntry(
            symbol=symbol, asset_class=asset_class, notes="",
            conviction=conviction, sector_tags=sector_tags,
        ))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`

Expected: PASS — including the new test and all existing orchestrator tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): parse conviction + sectors from opportunities.md"
```

---

## Task 3 — Add `atr()` helper to `market_data.py`

**Files:**
- Modify: `src/trading_bot/market_data.py` (append after `compute_indicators`)
- Modify: `tests/test_market_data.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_market_data.py`:

```python
import pandas as pd
import pytest


def _make_bars(highs, lows, closes):
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes,
                         "volume": [1.0] * len(closes)})


def test_atr_constant_range_equals_that_range():
    from trading_bot.market_data import atr
    # Every bar has high-low = 2.0; prev close inside the range; TR = 2.0 always.
    bars = _make_bars(highs=[12.0] * 20, lows=[10.0] * 20, closes=[11.0] * 20)
    assert atr(bars, n=14) == pytest.approx(2.0, abs=1e-6)


def test_atr_zero_range_returns_zero():
    from trading_bot.market_data import atr
    bars = _make_bars(highs=[10.0] * 20, lows=[10.0] * 20, closes=[10.0] * 20)
    assert atr(bars, n=14) == pytest.approx(0.0, abs=1e-9)


def test_atr_too_few_bars_returns_zero():
    from trading_bot.market_data import atr
    bars = _make_bars(highs=[12.0] * 5, lows=[10.0] * 5, closes=[11.0] * 5)
    assert atr(bars, n=14) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_market_data.py -v -k atr`

Expected: FAIL with `ImportError: cannot import name 'atr'`.

- [ ] **Step 3: Implement `atr`**

Append to `src/trading_bot/market_data.py`:

```python
def atr(bars: pd.DataFrame, n: int = 14) -> float:
    """Average True Range over the last `n` bars. Returns 0.0 if insufficient data.

    True Range = max(high-low, |high - prev_close|, |low - prev_close|).
    Uses simple mean (not Wilder's smoothing) — sufficient for sizing decisions.
    """
    if len(bars) < n + 1:
        return 0.0
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    prev_close = bars["close"].astype(float).shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.iloc[-n:].mean())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_market_data.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/market_data.py tests/test_market_data.py
git commit -m "feat(market_data): add atr() helper for sizing layer"
```

---

## Task 4 — Add `SizingConfig` to `config.py`

**Files:**
- Modify: `src/trading_bot/config.py` (after `RiskConfig`, before `AppConfig`)
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_sizing_config_defaults():
    from trading_bot.config import SizingConfig
    s = SizingConfig()
    assert s.conviction_floor == 0.3
    assert s.atr_lookback_days == 14
    assert s.target_atr_pct == {"stock": 0.02, "crypto": 0.05, "option": 0.04}
    assert s.mult_floor == 0.25
    assert s.mult_ceiling == 2.0
    assert s.correlation_per_match == 0.3


def test_sizing_config_rejects_invalid_floor():
    import pytest as _pt
    from pydantic import ValidationError
    from trading_bot.config import SizingConfig
    with _pt.raises(ValidationError):
        SizingConfig(conviction_floor=1.5)


def test_app_config_includes_default_sizing(tmp_path):
    """AppConfig validates without a `sizing:` block and supplies defaults."""
    from trading_bot.config import load_config
    cfg_yaml = tmp_path / "c.yaml"
    cfg_yaml.write_text("""
risk:
  daily_loss_limit_pct: 3
  weekly_loss_limit_pct: 7
  per_trade_risk_pct: 1
  max_position_pct: 10
  max_symbol_concentration_pct: 15
  max_consecutive_losing_days: 3
allocation:
  stocks_max_pct: 70
  crypto_max_pct: 20
  options_max_pct: 10
  cash_floor_pct: 10
regime_allocations:
  trending_up:
    stocks: 70
    crypto: 20
    options: 10
    cash: 0
email:
  to: a@b.c
  daily_summary_time_et: "16:30"
  weekly_summary_day: "fri"
storage:
  trade_journal_path: "/tmp/x.db"
""")
    cfg = load_config(cfg_yaml)
    assert cfg.sizing.conviction_floor == 0.3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v -k sizing`

Expected: FAIL with `ImportError: cannot import name 'SizingConfig'`.

- [ ] **Step 3: Add `SizingConfig` and wire into `AppConfig`**

Edit `src/trading_bot/config.py`. Insert after the `RiskConfig` class (after line 38):

```python
class SizingConfig(BaseModel):
    conviction_floor: float = Field(default=0.3, ge=0, le=1)
    atr_lookback_days: int = Field(default=14, ge=5, le=60)
    target_atr_pct: dict[str, float] = Field(
        default_factory=lambda: {"stock": 0.02, "crypto": 0.05, "option": 0.04}
    )
    mult_floor: float = Field(default=0.25, ge=0.1, le=1.0)
    mult_ceiling: float = Field(default=2.0, ge=1.0, le=5.0)
    correlation_per_match: float = Field(default=0.3, ge=0.0, le=1.0)
```

Then update `AppConfig` (line 65):

```python
class AppConfig(BaseModel):
    risk: RiskConfig
    allocation: AllocationConfig
    regime_allocations: dict[str, RegimeAllocation]
    email: EmailConfig
    storage: StorageConfig
    sizing: SizingConfig = Field(default_factory=SizingConfig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`

Expected: PASS — both new tests and all existing config tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/config.py tests/test_config.py
git commit -m "feat(config): add SizingConfig with defaults; wire into AppConfig"
```

---

## Task 5 — `position_sizer.size`: conviction-only behavior

**Files:**
- Create: `src/trading_bot/position_sizer.py`
- Create: `tests/test_position_sizer.py`

This task establishes the module skeleton, the `SizingResult` dataclass, and the conviction multiplier branch. Volatility and correlation are stubbed at 1.0 in this task and filled in by Tasks 6 and 7.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_position_sizer.py`:

```python
import pandas as pd
import pytest

from trading_bot.alpaca_client import Position
from trading_bot.config import SizingConfig
from trading_bot.state import WatchlistEntry


def _bars(n=30, close=100.0, hl_spread=2.0):
    rows = [{"open": close, "high": close + hl_spread / 2, "low": close - hl_spread / 2,
             "close": close, "volume": 1.0} for _ in range(n)]
    return pd.DataFrame(rows)


def _entry(conviction=0.7, asset_class="us_equity", sectors=("tech",)):
    return WatchlistEntry(
        symbol="NVDA", asset_class=asset_class, notes="",
        conviction=conviction, sector_tags=sectors,
    )


def test_size_low_conviction_returns_zero():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    res = size(_entry(conviction=0.2), base_qty=10, bars=_bars(),
               positions=[], sector_map={}, cfg=cfg)
    assert res.final_qty == 0
    assert res.skip_reason == "low_conviction"


def test_size_none_conviction_returns_zero():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    res = size(_entry(conviction=None), base_qty=10, bars=_bars(),
               positions=[], sector_map={}, cfg=cfg)
    assert res.final_qty == 0
    assert res.skip_reason == "low_conviction"


def test_size_conviction_at_floor_passes_with_mult_0_8():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    res = size(_entry(conviction=0.3), base_qty=100, bars=_bars(),
               positions=[], sector_map={}, cfg=cfg)
    assert res.skip_reason is None
    assert res.conviction_mult == pytest.approx(0.8)


def test_size_conviction_one_yields_max_conviction_mult():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    res = size(_entry(conviction=1.0), base_qty=100, bars=_bars(),
               positions=[], sector_map={}, cfg=cfg)
    assert res.conviction_mult == pytest.approx(1.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_position_sizer.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.position_sizer'`.

- [ ] **Step 3: Create the module with conviction logic only**

Create `src/trading_bot/position_sizer.py`:

```python
"""Dynamic position sizing layer between strategy.evaluate and risk_manager.check.

Multiplies the strategy's base_qty by three independent factors — conviction,
volatility, correlation — then clamps and rounds. Returns 0 (skip) when conviction
is missing/below the floor or when rounding floors the qty to zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from trading_bot.alpaca_client import Position
from trading_bot.config import SizingConfig
from trading_bot.state import WatchlistEntry


@dataclass(frozen=True)
class SizingResult:
    final_qty: int
    conviction_mult: float
    volatility_mult: float
    correlation_penalty: float
    combined_mult: float
    skip_reason: str | None  # "low_conviction" | "rounded_to_zero" | None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def size(
    entry: WatchlistEntry,
    base_qty: int,
    *,
    bars: pd.DataFrame,
    positions: list[Position],
    sector_map: dict[str, tuple[str, ...]],
    cfg: SizingConfig,
) -> SizingResult:
    # 1. Conviction gate
    if entry.conviction is None or entry.conviction < cfg.conviction_floor:
        return SizingResult(
            final_qty=0, conviction_mult=0.0, volatility_mult=1.0,
            correlation_penalty=1.0, combined_mult=0.0,
            skip_reason="low_conviction",
        )
    conviction_mult = _clamp(0.5 + entry.conviction, 0.5, 1.5)

    # Tasks 6 + 7 will fill these in; for now they are no-ops.
    volatility_mult = 1.0
    correlation_penalty = 1.0

    combined = conviction_mult * volatility_mult * correlation_penalty
    combined = _clamp(combined, cfg.mult_floor, cfg.mult_ceiling)
    final_qty = int(math.floor(base_qty * combined))
    if final_qty <= 0:
        return SizingResult(
            final_qty=0, conviction_mult=conviction_mult,
            volatility_mult=volatility_mult, correlation_penalty=correlation_penalty,
            combined_mult=combined, skip_reason="rounded_to_zero",
        )
    return SizingResult(
        final_qty=final_qty, conviction_mult=conviction_mult,
        volatility_mult=volatility_mult, correlation_penalty=correlation_penalty,
        combined_mult=combined, skip_reason=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_position_sizer.py -v`

Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/position_sizer.py tests/test_position_sizer.py
git commit -m "feat(sizer): position_sizer.size with conviction multiplier"
```

---

## Task 6 — Volatility multiplier

**Files:**
- Modify: `src/trading_bot/position_sizer.py`
- Modify: `tests/test_position_sizer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_position_sizer.py`:

```python
def test_volatility_mult_one_when_atr_at_class_target():
    """Stock target ATR/price = 2%. Bars with 2% ATR/price → mult = 1.0."""
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=2.0)  # ATR ≈ 2.0, atr/price = 0.02
    res = size(_entry(conviction=0.5), base_qty=100, bars=bars,
               positions=[], sector_map={}, cfg=cfg)
    assert res.volatility_mult == pytest.approx(1.0, abs=1e-6)


def test_volatility_mult_floors_at_0_5_when_atr_double_target():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=4.0)  # 4% ATR → ratio 0.5
    res = size(_entry(conviction=0.5), base_qty=100, bars=bars,
               positions=[], sector_map={}, cfg=cfg)
    assert res.volatility_mult == pytest.approx(0.5)


def test_volatility_mult_ceilings_at_1_5_when_atr_below_half_target():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=0.5)  # 0.5% ATR → ratio 4.0 → clamp 1.5
    res = size(_entry(conviction=0.5), base_qty=100, bars=bars,
               positions=[], sector_map={}, cfg=cfg)
    assert res.volatility_mult == pytest.approx(1.5)


def test_volatility_mult_uses_per_class_target_for_crypto():
    """Crypto target = 5%. 5% ATR → mult = 1.0 (no haircut)."""
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=5.0)
    res = size(_entry(conviction=0.5, asset_class="crypto"), base_qty=100,
               bars=bars, positions=[], sector_map={}, cfg=cfg)
    assert res.volatility_mult == pytest.approx(1.0, abs=1e-6)


def test_volatility_mult_one_when_atr_zero():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=0.0)  # high == low → ATR = 0
    res = size(_entry(conviction=0.5), base_qty=100, bars=bars,
               positions=[], sector_map={}, cfg=cfg)
    assert res.volatility_mult == pytest.approx(1.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_position_sizer.py -v -k volatility`

Expected: FAIL — current sizer always returns `volatility_mult=1.0`, so the 0.5 / 1.5 clamp tests fail.

- [ ] **Step 3: Wire ATR into the sizer**

Edit `src/trading_bot/position_sizer.py`. Add to imports:

```python
from trading_bot.market_data import atr
```

Replace the `volatility_mult = 1.0` line with:

```python
    # 2. Volatility multiplier — per-asset-class ATR/price target.
    last_close = float(bars["close"].iloc[-1]) if len(bars) > 0 else 0.0
    atr_value = atr(bars, n=cfg.atr_lookback_days)
    atr_pct = (atr_value / last_close) if last_close > 0 else 0.0
    target_key = "crypto" if entry.asset_class == "crypto" else (
        "option" if entry.asset_class == "us_option" else "stock"
    )
    target = cfg.target_atr_pct.get(target_key, 0.02)
    if atr_pct > 0:
        volatility_mult = _clamp(target / atr_pct, 0.5, 1.5)
    else:
        volatility_mult = 1.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_position_sizer.py -v`

Expected: PASS — all 9 tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/position_sizer.py tests/test_position_sizer.py
git commit -m "feat(sizer): per-asset-class ATR-based volatility multiplier"
```

---

## Task 7 — Correlation penalty

**Files:**
- Modify: `src/trading_bot/position_sizer.py`
- Modify: `tests/test_position_sizer.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_position_sizer.py`:

```python
def _pos(symbol, asset_class="us_equity"):
    from decimal import Decimal
    return Position(symbol=symbol, qty=Decimal("1"), market_value=Decimal("100"),
                    avg_entry_price=Decimal("100"), asset_class=asset_class)


def test_correlation_penalty_one_when_no_overlap():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    res = size(_entry(conviction=0.5, sectors=("tech",)), base_qty=100, bars=_bars(),
               positions=[_pos("XOM")], sector_map={"XOM": ("energy",)}, cfg=cfg)
    assert res.correlation_penalty == pytest.approx(1.0)


def test_correlation_penalty_with_three_same_sector_positions():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    sector_map = {"AAPL": ("tech",), "MSFT": ("tech",), "GOOG": ("tech", "ai")}
    res = size(_entry(conviction=0.5, sectors=("tech",)), base_qty=100, bars=_bars(),
               positions=[_pos("AAPL"), _pos("MSFT"), _pos("GOOG")],
               sector_map=sector_map, cfg=cfg)
    # 1 / (1 + 0.3 * 3) = 1 / 1.9 ≈ 0.526316
    assert res.correlation_penalty == pytest.approx(1.0 / 1.9, abs=1e-6)


def test_correlation_penalty_held_symbol_not_in_map_contributes_zero():
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    res = size(_entry(conviction=0.5, sectors=("tech",)), base_qty=100, bars=_bars(),
               positions=[_pos("DELISTED")], sector_map={}, cfg=cfg)
    assert res.correlation_penalty == pytest.approx(1.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_position_sizer.py -v -k correlation`

Expected: FAIL — `correlation_penalty` is always 1.0; the three-overlap test fails.

- [ ] **Step 3: Implement correlation logic**

Replace the `correlation_penalty = 1.0` line in `src/trading_bot/position_sizer.py` with:

```python
    # 3. Correlation penalty — count held positions sharing any sector tag.
    entry_sectors = set(entry.sector_tags)
    overlap = 0
    if entry_sectors:
        for p in positions:
            held_sectors = sector_map.get(p.symbol, ())
            if entry_sectors.intersection(held_sectors):
                overlap += 1
    correlation_penalty = 1.0 / (1.0 + cfg.correlation_per_match * overlap)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_position_sizer.py -v`

Expected: PASS — all 12 tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/position_sizer.py tests/test_position_sizer.py
git commit -m "feat(sizer): sector-overlap correlation penalty"
```

---

## Task 8 — Combined-multiplier clamp + rounded-to-zero

**Files:**
- Modify: `tests/test_position_sizer.py`

The clamp + rounding logic is already in place from Task 5. This task adds the integration tests that exercise both clamp boundaries and the rounded-to-zero skip.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_position_sizer.py`:

```python
def test_combined_clamps_to_ceiling():
    """High conviction (1.5×) + low vol (1.5×) + no correlation (1.0×) = 2.25× → clamp 2.0×."""
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=0.5)  # quiet → vol_mult = 1.5
    res = size(_entry(conviction=1.0, sectors=("tech",)), base_qty=100, bars=bars,
               positions=[], sector_map={}, cfg=cfg)
    assert res.combined_mult == pytest.approx(2.0)
    assert res.final_qty == 200


def test_combined_clamps_to_floor():
    """Low-passing conviction (0.8×) + high vol (0.5×) + heavy correlation: → floor 0.25×."""
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=4.0)  # vol_mult = 0.5
    sector_map = {f"S{i}": ("tech",) for i in range(5)}
    positions = [_pos(f"S{i}") for i in range(5)]
    # raw: 0.8 * 0.5 * (1/(1+0.3*5)) = 0.8 * 0.5 * 0.4 = 0.16 → floor 0.25
    res = size(_entry(conviction=0.3, sectors=("tech",)), base_qty=100, bars=bars,
               positions=positions, sector_map=sector_map, cfg=cfg)
    assert res.combined_mult == pytest.approx(0.25)
    assert res.final_qty == 25


def test_rounded_to_zero_returns_skip():
    """base_qty=1, multiplier 0.4 → final_qty floor = 0 → skip."""
    from trading_bot.position_sizer import size
    cfg = SizingConfig()
    bars = _bars(close=100.0, hl_spread=4.0)  # vol=0.5
    sector_map = {"AAPL": ("tech",)}
    # 0.8 * 0.5 * (1/(1+0.3)) ≈ 0.308 → final = floor(1 * 0.308) = 0
    res = size(_entry(conviction=0.3, sectors=("tech",)), base_qty=1, bars=bars,
               positions=[_pos("AAPL")], sector_map=sector_map, cfg=cfg)
    assert res.final_qty == 0
    assert res.skip_reason == "rounded_to_zero"
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `pytest tests/test_position_sizer.py -v -k "combined or rounded"`

Expected: PASS — clamp + rounding logic was implemented in Task 5; these tests confirm the combined behavior. (If a test fails, this is a red flag — fix the underlying logic, do not skip ahead.)

- [ ] **Step 3: No implementation needed**

The math from Tasks 5-7 already covers this. If Step 2 passed, proceed to commit.

- [ ] **Step 4: Re-run the full sizer suite**

Run: `pytest tests/test_position_sizer.py -v`

Expected: PASS — 15 tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_position_sizer.py
git commit -m "test(sizer): clamp + rounded-to-zero coverage"
```

---

## Task 9 — Extend `Decision` with sizing fields

**Files:**
- Modify: `src/trading_bot/orchestrator.py:26-32`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
def test_decision_carries_optional_sizing_fields():
    from trading_bot.orchestrator import Decision
    d = Decision(
        symbol="NVDA", action="placed_order",
        base_qty=100, final_qty=132,
        conviction_mult=1.32, volatility_mult=1.0, correlation_penalty=1.0,
    )
    assert d.base_qty == 100
    assert d.final_qty == 132
    assert d.conviction_mult == 1.32
    # Defaults still work for non-sized actions.
    d2 = Decision(symbol="NVDA", action="hold", reason="rsi too low")
    assert d2.base_qty is None
    assert d2.final_qty is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_decision_carries_optional_sizing_fields -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'base_qty'`.

- [ ] **Step 3: Extend `Decision`**

Edit `src/trading_bot/orchestrator.py` lines 26-32:

```python
@dataclass(frozen=True)
class Decision:
    symbol: str
    action: str
    reason: str = ""
    entry_order_id: str = ""
    stop_loss_order_id: str = ""
    base_qty: int | None = None
    final_qty: int | None = None
    conviction_mult: float | None = None
    volatility_mult: float | None = None
    correlation_penalty: float | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`

Expected: PASS — new test plus all existing orchestrator tests.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): Decision carries optional sizing breakdown"
```

---

## Task 10 — Wire the sizer into the orchestrator scan loop

**Files:**
- Modify: `src/trading_bot/orchestrator.py` (constructor signature + `scan` method)
- Modify: `tests/test_orchestrator.py`

This task does three things:
1. Build a `sector_map: dict[str, tuple[str, ...]]` once per scan from the loaded watchlist (so held symbols that were ranked recently still get tagged).
2. Call `position_sizer.size(...)` between `strategy.evaluate` and `risk.check`.
3. Route `final_qty` (not `sig.qty`) into the `OrderRequest`. Log decisions with the multiplier breakdown. Prepend the sizing breakdown to `TradeRecord.notes`.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_orchestrator.py`. Use whatever fakes the existing tests in this file already use (look for `FakeAlpaca`, `FakeMarket`, etc. — patterns are already present in this file). Sketch:

```python
def test_scan_skips_low_conviction_with_sized_decision():
    """Entry with conviction below floor → skipped_low_conviction; risk.check never called."""
    # Arrange: fakes for alpaca/market/journal, a ranked watchlist entry with conviction=0.2,
    # a strategy that would BUY (so we know the sizer is what stopped it).
    # Assert: result.decisions[0].action == "skipped_low_conviction"
    #         result.decisions[0].conviction_mult == 0.0  (or whatever the sizer reported)
    ...


def test_scan_passes_final_qty_to_risk_and_order():
    """Entry with high conviction + low correlation → final_qty differs from base_qty,
    and the OrderRequest sent to risk/place_order uses final_qty."""
    # Arrange: conviction=1.0, base_qty from strategy = 100, expect final_qty around 150-200.
    # Assert: captured order.qty == result.decisions[0].final_qty
    #         result.decisions[0].base_qty == 100
    ...


def test_scan_records_sizing_breakdown_in_journal_notes():
    """TradeRecord.notes for a placed order starts with `[size: ...]`."""
    ...
```

Look at the existing tests in `tests/test_orchestrator.py` for the exact fake pattern used; copy it. The existing scan tests in this file already cover successful BUY paths and rejection paths — clone the closest matching one and adapt the watchlist entry's conviction.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator.py -v -k "skips_low_conviction or final_qty_to_risk or sizing_breakdown"`

Expected: FAIL — sizer not yet wired in.

- [ ] **Step 3: Wire the sizer into the orchestrator**

Edit `src/trading_bot/orchestrator.py`. Add the imports near the existing imports:

```python
from trading_bot.position_sizer import SizingResult, size as size_position
```

Modify the constructor (around line 65-86) — add `sector_map` plumbing. The map is built per-scan from the ranked watchlist itself (so it's always fresh and limited to scope):

In the `scan` method, **right after** `state = self._build_state()` and the `decisions: list[Decision] = []` initialization (around line 105-106), add:

```python
        # Build sector_map for the correlation penalty: every entry on the ranked
        # watchlist contributes its sector tags. Held symbols not on today's ranked
        # list contribute zero overlap (handled by sector_map.get default).
        sector_map: dict[str, tuple[str, ...]] = {
            e.symbol: e.sector_tags for e in watchlist if e.sector_tags
        }
```

Then, **between** the existing `sig = self._strategy.evaluate(...)` block and the `OrderRequest` construction (around line 132-145), insert:

```python
            sizing = size_position(
                entry, base_qty=sig.qty,
                bars=bars, positions=positions,
                sector_map=sector_map, cfg=self._cfg.sizing,
            )
            if sizing.final_qty == 0:
                decisions.append(Decision(
                    symbol=symbol, action="skipped_low_conviction",
                    reason=sizing.skip_reason or "",
                    base_qty=sig.qty, final_qty=0,
                    conviction_mult=sizing.conviction_mult,
                    volatility_mult=sizing.volatility_mult,
                    correlation_penalty=sizing.correlation_penalty,
                ))
                continue
```

Replace the `OrderRequest(... qty=sig.qty ...)` (line 138-145) construction so it uses `sizing.final_qty`:

```python
            order = OrderRequest(
                symbol=symbol,
                qty=sizing.final_qty,
                side=OrderSide.BUY,
                asset_class=asset_class,
                limit_price=sig.entry_price,
                stop_loss_price=sig.stop_loss_price,
            )
```

Update the `TradeRecord(...)` call (around line 161-173) to prepend the sizing breakdown into `notes`:

```python
            sizing_note = (
                f"[size: conv={sizing.conviction_mult:.2f}"
                f"×vol={sizing.volatility_mult:.2f}"
                f"×corr={sizing.correlation_penalty:.2f}"
                f"→{sizing.combined_mult:.2f}]"
            )
            self._journal.append(TradeRecord(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                side="buy",
                qty=sizing.final_qty,
                price=sig.entry_price,
                asset_class=asset_class.value,
                strategy="momentum",
                regime=self._regime,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
                notes=f"{sizing_note} {sig.reason}",
            ))
```

Update the final `placed_order` Decision (around line 174-178) so it also carries the multiplier breakdown:

```python
            decisions.append(Decision(
                symbol=symbol, action="placed_order", reason=sig.reason,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
                base_qty=sig.qty, final_qty=sizing.final_qty,
                conviction_mult=sizing.conviction_mult,
                volatility_mult=sizing.volatility_mult,
                correlation_penalty=sizing.correlation_penalty,
            ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`

Expected: PASS — new tests plus all existing orchestrator tests. If existing tests fail, the most likely cause is that they construct `WatchlistEntry` without `conviction`, so the sizer skips them with `skipped_low_conviction`. Update those tests to pass `conviction=0.7` (or higher) to keep the BUY path.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): wire position_sizer between strategy and risk gate"
```

---

## Task 11 — End-to-end sweep

**Files:** none (validation only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`

Expected: PASS — all tests across the project, including the original 121 from Plan 5a plus the ~17 new tests from this plan.

- [ ] **Step 2: Spot-check `bot scan` smoke run if practical**

If a working `.env` is available locally, run a single scan with a small ranked watchlist to confirm the orchestrator picks up the new fields end-to-end. If not, skip — the unit + integration coverage is sufficient for this plan.

- [ ] **Step 3: Commit nothing (no code changes)**

This is a verification gate, not a code-producing task. Move to summary.

---

## Self-Review Notes

**Spec coverage:**
- WatchlistEntry extension → Task 1 ✓
- load_ranked_watchlist parser → Task 2 ✓
- atr() helper in indicators → Task 3 ✓
- SizingConfig → Task 4 ✓
- position_sizer.size — conviction → Task 5; volatility → Task 6; correlation → Task 7; clamp+round → Task 8 ✓
- Decision extension → Task 9 ✓
- Orchestrator wiring + sector_map + final_qty routing + notes prefix → Task 10 ✓
- Spec's `TradeRecord.sizing_breakdown` → handled via `notes` prefix (deviation documented at top) ✓
- 15 unit tests from spec → covered across Tasks 5-8 ✓
- Integration tests (low_conviction skip, final_qty routing, journal breakdown) → Task 10 ✓

**Type consistency:** `SizingResult` fields are referenced identically across Tasks 5-10. `Decision`'s new optional fields are referenced consistently. `WatchlistEntry.conviction: float | None` and `sector_tags: tuple[str, ...]` are stable from Task 1 onward.

**No placeholders:** Every code-producing step shows code. Task 10's tests are sketched (not full code) only because they must mirror the existing fake-orchestrator-test pattern in `tests/test_orchestrator.py`, which is the cheapest accurate guidance — the implementer reads that file to clone the closest existing test, then adapts it. If this proves too vague during execution, a follow-up edit can inline the full fakes once the existing test pattern is confirmed.
