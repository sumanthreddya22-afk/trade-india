# Plan 5a — Universe Expansion & Two-Stage Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded 7-symbol watchlist with a daily liquidity-screened universe (~3,000 names) feeding a two-stage funnel: stage-1 produces a 100-name shortlist; stage-2 runs four parallel strategy lanes over it, ranking candidates that the existing orchestrator consumes.

**Architecture:** Universe pull (`alpaca-py` `list_assets` + 20-day daily bars for ADV) → liquidity filter → sector tags → markdown snapshot. Stage-1 composite scoring → `opportunities.md` shortlist. Stage-2 lanes (momentum, mean-reversion, breakout) score in parallel via `ThreadPoolExecutor`, merge, dedupe, append lane attribution. Orchestrator reads the ranked file and runs its existing per-symbol decision flow. Every step is TDD; commit frequently.

**Tech Stack:** Python 3.11+, alpaca-py, pandas, ta (technical-analysis), pytest, pyyaml. No new dependencies.

---

## File Structure

**New files:**
- `src/trading_bot/universe.py` — universe fetch + liquidity filter + sector tagging + markdown writer (~250 lines)
- `src/trading_bot/screener.py` — stage-1 composite scoring + stage-2 orchestration (~250 lines)
- `src/trading_bot/strategy_lanes.py` — `Lane` Protocol + 3 concrete lanes (~250 lines)
- `tests/test_universe.py`
- `tests/test_screener.py`
- `tests/test_strategy_lanes.py`
- `tests/fixtures/bars/` — canned OHLCV CSVs for deterministic indicator tests

**Modified files:**
- `src/trading_bot/alpaca_client.py` — add `get_active_assets(asset_class)` method
- `src/trading_bot/orchestrator.py` — add `scan_from_opportunities()` entry point
- `src/trading_bot/cli.py` — add `bot screen-universe`, `bot rank`, `bot scan-ranked` commands
- `.gitignore` — ignore generated `strategy/latest_intelligence.md` and `strategy/opportunities.md`

**No file is touched by more than one task** unless explicitly noted. This keeps each task's diff isolated.

---

## Task 1: `LiquidAsset` data model

**Files:**
- Create: `src/trading_bot/universe.py`
- Test: `tests/test_universe.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe.py`:

```python
from decimal import Decimal

from trading_bot.universe import LiquidAsset


def test_liquid_asset_holds_screening_fields():
    asset = LiquidAsset(
        symbol="NVDA",
        name="NVIDIA Corp",
        asset_class="us_equity",
        exchange="NASDAQ",
        last_price=Decimal("450.00"),
        avg_dollar_volume=Decimal("8500000000"),
        fractionable=True,
        sector_tags=("ai", "semiconductors"),
    )
    assert asset.symbol == "NVDA"
    assert asset.avg_dollar_volume > Decimal("5000000")
    assert "ai" in asset.sector_tags
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_universe.py::test_liquid_asset_holds_screening_fields -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.universe'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/trading_bot/universe.py`:

```python
"""Universe expansion: fetch full Alpaca tradable universe, apply liquidity
screen, tag by sector, write markdown snapshot for downstream readers.

Inspired by trading-codex `intelligence.collect_market_universe` (which we
copied to .codex-inspiration/ for reference) but extended with ADV-based
liquidity filtering and a richer sector taxonomy.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LiquidAsset:
    symbol: str
    name: str
    asset_class: str  # "us_equity" | "crypto"
    exchange: str
    last_price: Decimal
    avg_dollar_volume: Decimal
    fractionable: bool
    sector_tags: tuple[str, ...]
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_universe.py::test_liquid_asset_holds_screening_fields -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/universe.py tests/test_universe.py
git commit -m "feat(universe): LiquidAsset data model"
```

---

## Task 2: Liquidity filter

**Files:**
- Modify: `src/trading_bot/universe.py`
- Test: `tests/test_universe.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_universe.py`:

```python
from decimal import Decimal

from trading_bot.universe import LiquidAsset, apply_liquidity_filter


def _asset(symbol, price, adv):
    return LiquidAsset(
        symbol=symbol,
        name=symbol,
        asset_class="us_equity",
        exchange="NASDAQ",
        last_price=Decimal(str(price)),
        avg_dollar_volume=Decimal(str(adv)),
        fractionable=True,
        sector_tags=(),
    )


def test_liquidity_filter_keeps_qualified_assets():
    assets = [
        _asset("NVDA", 450, 8_500_000_000),
        _asset("AMD", 100, 2_000_000_000),
    ]
    kept = apply_liquidity_filter(assets, min_price=Decimal("5"), min_adv=Decimal("5000000"))
    assert {a.symbol for a in kept} == {"NVDA", "AMD"}


def test_liquidity_filter_drops_low_price():
    assets = [_asset("PENNY", 2.50, 100_000_000)]
    kept = apply_liquidity_filter(assets, min_price=Decimal("5"), min_adv=Decimal("5000000"))
    assert kept == []


def test_liquidity_filter_drops_low_adv():
    assets = [_asset("ILLIQ", 50, 100_000)]
    kept = apply_liquidity_filter(assets, min_price=Decimal("5"), min_adv=Decimal("5000000"))
    assert kept == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_universe.py -v
```

Expected: 3 FAILs with `ImportError: cannot import name 'apply_liquidity_filter'`.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/universe.py`:

```python
from collections.abc import Iterable

DEFAULT_MIN_PRICE = Decimal("5")
DEFAULT_MIN_ADV = Decimal("5000000")  # $5M average daily dollar volume


def apply_liquidity_filter(
    assets: Iterable[LiquidAsset],
    *,
    min_price: Decimal = DEFAULT_MIN_PRICE,
    min_adv: Decimal = DEFAULT_MIN_ADV,
) -> list[LiquidAsset]:
    """Keep only assets whose last price >= min_price and avg dollar volume >= min_adv.

    Crypto assets bypass the equity-style price floor (BTC and ETH are always
    above $5; SOL/USD etc. are also fine), but ADV still applies.
    """
    out: list[LiquidAsset] = []
    for a in assets:
        if a.asset_class == "us_equity" and a.last_price < min_price:
            continue
        if a.avg_dollar_volume < min_adv:
            continue
        out.append(a)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_universe.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/universe.py tests/test_universe.py
git commit -m "feat(universe): liquidity filter (price + ADV)"
```

---

## Task 3: Alpaca tradable-universe fetcher

**Files:**
- Modify: `src/trading_bot/alpaca_client.py`
- Test: `tests/test_alpaca_client.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_alpaca_client.py`:

```python
from unittest.mock import MagicMock

from trading_bot.alpaca_client import AlpacaClient, TradableAsset


def test_get_active_assets_returns_tradable(monkeypatch):
    mock_asset_a = MagicMock(
        symbol="NVDA", name="NVIDIA", exchange="NASDAQ", status="active",
        tradable=True, fractionable=True, asset_class="us_equity",
    )
    mock_asset_b = MagicMock(
        symbol="HALT", name="Halted Inc", exchange="NYSE", status="inactive",
        tradable=False, fractionable=False, asset_class="us_equity",
    )
    client = MagicMock()
    client.get_all_assets.return_value = [mock_asset_a, mock_asset_b]

    wrapper = AlpacaClient.__new__(AlpacaClient)
    wrapper._client = client

    result = wrapper.get_active_assets("us_equity")
    assert len(result) == 1
    assert result[0].symbol == "NVDA"
    assert isinstance(result[0], TradableAsset)
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_alpaca_client.py::test_get_active_assets_returns_tradable -v
```

Expected: FAIL with `ImportError: cannot import name 'TradableAsset'`.

- [ ] **Step 3: Implement**

Add to `src/trading_bot/alpaca_client.py` (after the `Position` dataclass):

```python
@dataclass(frozen=True)
class TradableAsset:
    symbol: str
    name: str
    exchange: str
    asset_class: str
    tradable: bool
    fractionable: bool
```

Add this method to the `AlpacaClient` class:

```python
def get_active_assets(self, asset_class: str) -> list[TradableAsset]:
    """List all active+tradable assets for the given asset_class.

    asset_class: "us_equity" or "crypto"
    """
    from alpaca.trading.requests import GetAssetsRequest
    try:
        raw = self._client.get_all_assets(
            GetAssetsRequest(asset_class=asset_class, status="active")
        )
    except Exception as e:
        raise AlpacaClientError(f"get_all_assets failed: {e}") from e
    return [
        TradableAsset(
            symbol=str(a.symbol),
            name=str(a.name or ""),
            exchange=str(a.exchange or ""),
            asset_class=str(a.asset_class),
            tradable=bool(a.tradable),
            fractionable=bool(getattr(a, "fractionable", False)),
        )
        for a in raw
        if a.tradable
    ]
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_alpaca_client.py::test_get_active_assets_returns_tradable -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/alpaca_client.py tests/test_alpaca_client.py
git commit -m "feat(alpaca): get_active_assets returns tradable universe"
```

---

## Task 4: ADV (average dollar volume) computation

**Files:**
- Modify: `src/trading_bot/universe.py`
- Test: `tests/test_universe.py`
- Create: `tests/fixtures/bars/nvda_20d.csv`

- [ ] **Step 1: Create fixture file**

Create `tests/fixtures/bars/nvda_20d.csv`:

```
timestamp,open,high,low,close,volume
2026-04-01,440,445,438,442,30000000
2026-04-02,442,448,440,447,32000000
2026-04-03,447,450,444,448,28000000
2026-04-04,448,452,446,451,31000000
2026-04-05,451,455,449,453,29000000
2026-04-08,453,458,451,456,33000000
2026-04-09,456,460,454,459,30000000
2026-04-10,459,463,457,461,28000000
2026-04-11,461,464,458,462,27000000
2026-04-12,462,466,460,464,29000000
2026-04-15,464,468,462,466,31000000
2026-04-16,466,470,463,468,30000000
2026-04-17,468,472,465,470,32000000
2026-04-18,470,474,468,472,33000000
2026-04-19,472,476,470,474,31000000
2026-04-22,474,478,472,476,29000000
2026-04-23,476,480,473,478,30000000
2026-04-24,478,482,476,480,28000000
2026-04-25,480,484,478,482,29000000
2026-04-26,482,486,480,484,30000000
```

- [ ] **Step 2: Write failing test**

Append to `tests/test_universe.py`:

```python
from pathlib import Path
import pandas as pd

from trading_bot.universe import compute_adv


def test_compute_adv_returns_avg_dollar_volume():
    fixture = Path(__file__).parent / "fixtures" / "bars" / "nvda_20d.csv"
    bars = pd.read_csv(fixture, parse_dates=["timestamp"])
    adv = compute_adv(bars)
    # mean(close * volume) across 20 rows
    expected = (bars["close"] * bars["volume"]).mean()
    assert abs(float(adv) - float(expected)) < 1.0
```

- [ ] **Step 3: Run test to verify it fails**

```
pytest tests/test_universe.py::test_compute_adv_returns_avg_dollar_volume -v
```

Expected: FAIL with `ImportError: cannot import name 'compute_adv'`.

- [ ] **Step 4: Implement**

Append to `src/trading_bot/universe.py`:

```python
import pandas as pd


def compute_adv(bars: pd.DataFrame) -> Decimal:
    """Average daily dollar volume across the bar window.

    Expects a DataFrame with `close` and `volume` columns. Returns Decimal.
    """
    if bars.empty:
        return Decimal("0")
    dollar_volume = bars["close"] * bars["volume"]
    return Decimal(str(float(dollar_volume.mean())))
```

- [ ] **Step 5: Run test to verify it passes**

```
pytest tests/test_universe.py::test_compute_adv_returns_avg_dollar_volume -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```
git add src/trading_bot/universe.py tests/test_universe.py tests/fixtures/bars/nvda_20d.csv
git commit -m "feat(universe): ADV computation from daily bars"
```

---

## Task 5: Sector tagger (keyword-based bucketing)

**Files:**
- Modify: `src/trading_bot/universe.py`
- Test: `tests/test_universe.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_universe.py`:

```python
from trading_bot.universe import tag_sectors


def test_tag_sectors_finds_ai_semiconductors():
    tags = tag_sectors(symbol="NVDA", name="NVIDIA Corp - AI semiconductor leader")
    assert "ai" in tags
    assert "semiconductors" in tags


def test_tag_sectors_finds_energy():
    tags = tag_sectors(symbol="XLE", name="Energy Select Sector SPDR Fund")
    assert "energy" in tags


def test_tag_sectors_returns_empty_on_no_match():
    tags = tag_sectors(symbol="ZZZ", name="Generic Holdings Inc")
    assert tags == ()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_universe.py -v
```

Expected: 3 FAILs with `ImportError: cannot import name 'tag_sectors'`.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/universe.py`:

```python
import re

# Sector taxonomy: tag → keyword set. Word-boundary matching avoids false positives.
SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "artificial intelligence", "machine learning"),
    "semiconductors": ("semiconductor", "chip", "silicon", "fab", "foundry"),
    "biotech": ("biotech", "biopharma", "pharma", "therapeutics", "medical"),
    "energy": ("energy", "oil", "gas", "petroleum", "exploration"),
    "uranium": ("uranium", "nuclear"),
    "metals": ("gold", "silver", "copper", "mining", "miner", "metals"),
    "crypto_equity": ("bitcoin", "blockchain", "crypto", "digital asset"),
    "consumer": ("consumer", "retail", "apparel", "restaurant"),
    "financials": ("bank", "insurance", "financial", "mortgage"),
    "real_estate": ("reit", "real estate", "property"),
    "utilities": ("utility", "utilities", "electric", "water"),
    "transport": ("airline", "shipping", "trucking", "rail"),
}


def tag_sectors(*, symbol: str, name: str) -> tuple[str, ...]:
    """Return sorted unique tags inferred from symbol+name keywords.

    Word-boundary regex prevents 'gold' from matching 'goldman' etc.
    """
    text = f"{symbol} {name}".lower()
    matched: set[str] = set()
    for tag, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, text):
                matched.add(tag)
                break
    return tuple(sorted(matched))
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_universe.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/universe.py tests/test_universe.py
git commit -m "feat(universe): keyword-based sector tagging"
```

---

## Task 6: Universe builder (orchestrates fetch → filter → tag)

**Files:**
- Modify: `src/trading_bot/universe.py`
- Test: `tests/test_universe.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_universe.py`:

```python
from unittest.mock import MagicMock
from decimal import Decimal
import pandas as pd

from trading_bot.alpaca_client import TradableAsset
from trading_bot.universe import build_universe


def test_build_universe_filters_and_tags():
    alpaca = MagicMock()
    alpaca.get_active_assets.side_effect = [
        [
            TradableAsset(symbol="NVDA", name="NVIDIA semiconductor",
                          exchange="NASDAQ", asset_class="us_equity",
                          tradable=True, fractionable=True),
            TradableAsset(symbol="ILLIQ", name="Illiquid Inc",
                          exchange="NYSE", asset_class="us_equity",
                          tradable=True, fractionable=False),
        ],
        [
            TradableAsset(symbol="BTC/USD", name="Bitcoin USD",
                          exchange="CRYPTO", asset_class="crypto",
                          tradable=True, fractionable=True),
        ],
    ]

    def fake_bar_loader(symbol: str) -> pd.DataFrame:
        if symbol == "NVDA":
            return pd.DataFrame({"close": [450]*20, "volume": [30_000_000]*20})
        if symbol == "ILLIQ":
            return pd.DataFrame({"close": [10]*20, "volume": [10_000]*20})
        if symbol == "BTC/USD":
            return pd.DataFrame({"close": [70000]*20, "volume": [1_000]*20})
        return pd.DataFrame()

    universe = build_universe(alpaca, bar_loader=fake_bar_loader)
    symbols = {a.symbol for a in universe}
    assert "NVDA" in symbols
    assert "BTC/USD" in symbols
    assert "ILLIQ" not in symbols  # filtered out by ADV
    nvda = next(a for a in universe if a.symbol == "NVDA")
    assert "semiconductors" in nvda.sector_tags
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_universe.py::test_build_universe_filters_and_tags -v
```

Expected: FAIL with `ImportError: cannot import name 'build_universe'`.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/universe.py`:

```python
from collections.abc import Callable

from trading_bot.alpaca_client import AlpacaClient


def build_universe(
    alpaca: AlpacaClient,
    *,
    bar_loader: Callable[[str], pd.DataFrame],
    min_price: Decimal = DEFAULT_MIN_PRICE,
    min_adv: Decimal = DEFAULT_MIN_ADV,
) -> list[LiquidAsset]:
    """Pull tradable universe, score liquidity, tag sectors, return LiquidAssets.

    bar_loader is injected so tests can supply canned data without hitting Alpaca.
    """
    raw_equities = alpaca.get_active_assets("us_equity")
    raw_crypto = alpaca.get_active_assets("crypto")

    candidates: list[LiquidAsset] = []
    for asset in list(raw_equities) + list(raw_crypto):
        bars = bar_loader(asset.symbol)
        if bars.empty:
            continue
        last_price = Decimal(str(float(bars["close"].iloc[-1])))
        adv = compute_adv(bars)
        candidates.append(
            LiquidAsset(
                symbol=asset.symbol,
                name=asset.name,
                asset_class=asset.asset_class,
                exchange=asset.exchange,
                last_price=last_price,
                avg_dollar_volume=adv,
                fractionable=asset.fractionable,
                sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
            )
        )
    return apply_liquidity_filter(candidates, min_price=min_price, min_adv=min_adv)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_universe.py::test_build_universe_filters_and_tags -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/universe.py tests/test_universe.py
git commit -m "feat(universe): build_universe orchestrator (fetch+filter+tag)"
```

---

## Task 7: Markdown writer for universe snapshot

**Files:**
- Modify: `src/trading_bot/universe.py`
- Test: `tests/test_universe.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_universe.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.universe import LiquidAsset, render_universe_snapshot


def test_render_universe_snapshot_includes_counts_and_top_sectors():
    assets = [
        LiquidAsset(symbol="NVDA", name="NVIDIA",
                    asset_class="us_equity", exchange="NASDAQ",
                    last_price=Decimal("450"), avg_dollar_volume=Decimal("8e9"),
                    fractionable=True, sector_tags=("ai", "semiconductors")),
        LiquidAsset(symbol="GLD", name="Gold ETF",
                    asset_class="us_equity", exchange="NYSE",
                    last_price=Decimal("180"), avg_dollar_volume=Decimal("3e8"),
                    fractionable=True, sector_tags=("metals",)),
    ]
    md = render_universe_snapshot(assets, generated_at=datetime(2026, 4, 25, tzinfo=timezone.utc))
    assert "# Universe Snapshot" in md
    assert "Total liquid assets: 2" in md
    assert "NVDA" in md
    assert "semiconductors" in md
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_universe.py::test_render_universe_snapshot_includes_counts_and_top_sectors -v
```

Expected: FAIL with `ImportError: cannot import name 'render_universe_snapshot'`.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/universe.py`:

```python
from collections import Counter
from datetime import datetime
from pathlib import Path


def render_universe_snapshot(
    assets: list[LiquidAsset],
    *,
    generated_at: datetime,
    top_n_per_sector: int = 5,
) -> str:
    """Render a markdown snapshot summarizing the universe."""
    lines = [
        "# Universe Snapshot",
        "",
        f"Generated: {generated_at.isoformat(timespec='seconds')}",
        f"Total liquid assets: {len(assets)}",
        "",
    ]

    sector_counts: Counter[str] = Counter()
    for a in assets:
        for tag in a.sector_tags:
            sector_counts[tag] += 1

    lines.extend(["## Sector Breakdown", ""])
    for sector, count in sector_counts.most_common():
        lines.append(f"- {sector}: {count}")
    if not sector_counts:
        lines.append("- (no sector tags applied)")
    lines.append("")

    lines.extend(["## Top Names by ADV (per sector)", ""])
    by_sector: dict[str, list[LiquidAsset]] = {}
    for a in assets:
        for tag in a.sector_tags or ("untagged",):
            by_sector.setdefault(tag, []).append(a)
    for sector in sorted(by_sector):
        ranked = sorted(by_sector[sector], key=lambda x: x.avg_dollar_volume, reverse=True)[:top_n_per_sector]
        lines.append(f"### {sector}")
        for a in ranked:
            lines.append(
                f"- {a.symbol} ({a.exchange}) — ${a.last_price} — ADV ${a.avg_dollar_volume:,.0f}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_universe_snapshot(assets: list[LiquidAsset], path: Path, *, generated_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_universe_snapshot(assets, generated_at=generated_at))
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_universe.py::test_render_universe_snapshot_includes_counts_and_top_sectors -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/universe.py tests/test_universe.py
git commit -m "feat(universe): markdown snapshot writer"
```

---

## Task 8: CLI command `bot screen-universe`

**Files:**
- Modify: `src/trading_bot/cli.py`
- Modify: `.gitignore`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Update .gitignore**

Append to `.gitignore`:

```
strategy/latest_intelligence.md
strategy/opportunities.md
```

- [ ] **Step 2: Write failing test**

Append to `tests/test_cli.py`:

```python
from unittest.mock import patch

from click.testing import CliRunner

from trading_bot.cli import cli


def test_screen_universe_writes_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()

    with patch("trading_bot.cli.build_universe") as mock_build, \
         patch("trading_bot.cli.AlpacaClient") as mock_alpaca, \
         patch("trading_bot.cli.MarketDataClient"):
        from decimal import Decimal
        from trading_bot.universe import LiquidAsset
        mock_build.return_value = [
            LiquidAsset(symbol="NVDA", name="NVIDIA",
                        asset_class="us_equity", exchange="NASDAQ",
                        last_price=Decimal("450"), avg_dollar_volume=Decimal("8e9"),
                        fractionable=True, sector_tags=("ai", "semiconductors")),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["screen-universe"])

    assert result.exit_code == 0, result.output
    snapshot = (tmp_path / "strategy" / "latest_intelligence.md").read_text()
    assert "NVDA" in snapshot
```

- [ ] **Step 3: Run test to verify it fails**

```
pytest tests/test_cli.py::test_screen_universe_writes_snapshot -v
```

Expected: FAIL — command not registered.

- [ ] **Step 4: Implement**

Add to `src/trading_bot/cli.py` (top imports):

```python
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.universe import build_universe, write_universe_snapshot
```

Add this command (placed alongside other `@cli.command()` handlers):

```python
@cli.command("screen-universe")
def screen_universe() -> None:
    """Pull Alpaca tradable universe, apply liquidity screen, write snapshot."""
    settings = Settings.from_env()
    config = AppConfig.from_yaml(Path("strategy/config.yaml"))
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    def bar_loader(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=20)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    assets = build_universe(alpaca, bar_loader=bar_loader)
    write_universe_snapshot(
        assets,
        Path("strategy/latest_intelligence.md"),
        generated_at=datetime.now(timezone.utc),
    )
    click.echo(f"Wrote universe snapshot: {len(assets)} liquid assets")
```

- [ ] **Step 5: Run test to verify it passes**

```
pytest tests/test_cli.py::test_screen_universe_writes_snapshot -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```
git add src/trading_bot/cli.py tests/test_cli.py .gitignore
git commit -m "feat(cli): bot screen-universe writes liquidity-screened snapshot"
```

---

## Task 9: `RankedCandidate` and stage-1 composite scoring

**Files:**
- Create: `src/trading_bot/screener.py`
- Test: `tests/test_screener.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_screener.py`:

```python
from decimal import Decimal
import pandas as pd

from trading_bot.universe import LiquidAsset
from trading_bot.screener import RankedCandidate, score_candidate


def _asset(symbol):
    return LiquidAsset(
        symbol=symbol, name=symbol, asset_class="us_equity",
        exchange="NASDAQ", last_price=Decimal("100"),
        avg_dollar_volume=Decimal("1e9"), fractionable=True,
        sector_tags=("ai",),
    )


def test_score_candidate_combines_momentum_volume_and_sector():
    bars = pd.DataFrame({
        "close":  [100, 101, 102, 103, 104, 105],
        "volume": [1e6,  1.1e6, 1.2e6, 1.3e6, 1.5e6, 2e6],
    })
    benchmark_5d_pct = 0.5  # SPY up 0.5% over the same window
    cand = score_candidate(_asset("NVDA"), bars=bars, benchmark_5d_pct=benchmark_5d_pct)
    assert isinstance(cand, RankedCandidate)
    assert cand.symbol == "NVDA"
    # 5d return = (105-100)/100 = 5%, relative to benchmark = +4.5%
    assert cand.relative_5d_pct > 4.0
    # Volume ratio = last vol / mean(prior vols), well above 1.0
    assert cand.volume_ratio > 1.0
    assert cand.score > 0
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_screener.py::test_score_candidate_combines_momentum_volume_and_sector -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

Create `src/trading_bot/screener.py`:

```python
"""Two-stage screener: stage-1 composite scoring on the full filtered universe;
stage-2 strategy-lane scoring on the shortlist (delegated to strategy_lanes.py)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from trading_bot.universe import LiquidAsset


@dataclass(frozen=True)
class RankedCandidate:
    symbol: str
    asset_class: str
    sector_tags: tuple[str, ...]
    last_price: Decimal
    one_day_return_pct: float
    five_day_return_pct: float
    relative_5d_pct: float
    volume_ratio: float
    score: float


def score_candidate(
    asset: LiquidAsset,
    *,
    bars: pd.DataFrame,
    benchmark_5d_pct: float,
) -> RankedCandidate:
    """Composite score = 1d_return * 1.4 + relative_5d + min(vol_ratio, 3) * 2.

    Mirrors the codex scoring formula (validated empirically), with `relative_5d`
    as the SPY-relative 5-day move so a "rising tide" doesn't lift all candidates.
    """
    if len(bars) < 6:
        return RankedCandidate(
            symbol=asset.symbol, asset_class=asset.asset_class,
            sector_tags=asset.sector_tags, last_price=asset.last_price,
            one_day_return_pct=0.0, five_day_return_pct=0.0,
            relative_5d_pct=0.0, volume_ratio=1.0, score=-1e9,
        )

    last = float(bars["close"].iloc[-1])
    prev = float(bars["close"].iloc[-2])
    fifth = float(bars["close"].iloc[-6])
    one_day = ((last - prev) / prev) * 100 if prev else 0.0
    five_day = ((last - fifth) / fifth) * 100 if fifth else 0.0
    relative_5d = five_day - benchmark_5d_pct

    avg_vol = float(bars["volume"].iloc[-6:-1].mean())
    last_vol = float(bars["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    score = one_day * 1.4 + relative_5d + min(vol_ratio, 3.0) * 2.0

    return RankedCandidate(
        symbol=asset.symbol,
        asset_class=asset.asset_class,
        sector_tags=asset.sector_tags,
        last_price=asset.last_price,
        one_day_return_pct=one_day,
        five_day_return_pct=five_day,
        relative_5d_pct=relative_5d,
        volume_ratio=vol_ratio,
        score=score,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_screener.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/screener.py tests/test_screener.py
git commit -m "feat(screener): stage-1 composite scoring (momentum + relative + volume)"
```

---

## Task 10: Stage-1 shortlist generator

**Files:**
- Modify: `src/trading_bot/screener.py`
- Test: `tests/test_screener.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_screener.py`:

```python
from collections.abc import Callable

import pandas as pd

from trading_bot.screener import build_stage1_shortlist


def _make_bars(close_path: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "close": close_path,
        "volume": [1_000_000] * len(close_path),
    })


def test_build_stage1_shortlist_ranks_by_score_and_caps_size():
    universe = [
        _asset("AAA"),
        _asset("BBB"),
        _asset("CCC"),
    ]
    bar_paths = {
        "AAA": [100, 100, 100, 100, 100, 110],  # +10% 5d
        "BBB": [100, 100, 100, 100, 100, 105],  # +5% 5d
        "CCC": [100, 100, 100, 100, 100, 101],  # +1% 5d
        "SPY": [100, 100, 100, 100, 100, 100],  # 0% 5d
    }

    def loader(sym: str) -> pd.DataFrame:
        return _make_bars(bar_paths[sym])

    short = build_stage1_shortlist(universe, bar_loader=loader, top_n=2, benchmark_symbol="SPY")
    assert [c.symbol for c in short] == ["AAA", "BBB"]


def test_build_stage1_shortlist_handles_missing_bars():
    universe = [_asset("AAA"), _asset("EMPTY")]

    def loader(sym: str) -> pd.DataFrame:
        if sym == "EMPTY":
            return pd.DataFrame()
        return _make_bars([100, 100, 100, 100, 100, 110])

    short = build_stage1_shortlist(universe, bar_loader=loader, top_n=10, benchmark_symbol="SPY")
    assert "EMPTY" not in {c.symbol for c in short}
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_screener.py -v
```

Expected: 2 FAILs.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/screener.py`:

```python
from collections.abc import Callable


def build_stage1_shortlist(
    universe: list[LiquidAsset],
    *,
    bar_loader: Callable[[str], pd.DataFrame],
    top_n: int = 100,
    benchmark_symbol: str = "SPY",
) -> list[RankedCandidate]:
    """Score the entire universe and return top_n by composite score.

    Skips assets whose bars are unavailable or insufficient (< 6 rows).
    """
    benchmark_bars = bar_loader(benchmark_symbol)
    benchmark_5d = 0.0
    if len(benchmark_bars) >= 6:
        last = float(benchmark_bars["close"].iloc[-1])
        fifth = float(benchmark_bars["close"].iloc[-6])
        if fifth:
            benchmark_5d = ((last - fifth) / fifth) * 100

    candidates: list[RankedCandidate] = []
    for asset in universe:
        bars = bar_loader(asset.symbol)
        if len(bars) < 6:
            continue
        candidates.append(score_candidate(asset, bars=bars, benchmark_5d_pct=benchmark_5d))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_screener.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/screener.py tests/test_screener.py
git commit -m "feat(screener): build_stage1_shortlist with SPY-relative scoring"
```

---

## Task 11: Lane Protocol + `LaneCandidate` data model

**Files:**
- Create: `src/trading_bot/strategy_lanes.py`
- Test: `tests/test_strategy_lanes.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_strategy_lanes.py`:

```python
from decimal import Decimal

import pandas as pd

from trading_bot.screener import RankedCandidate
from trading_bot.strategy_lanes import Lane, LaneCandidate


def _ranked(symbol: str) -> RankedCandidate:
    return RankedCandidate(
        symbol=symbol, asset_class="us_equity", sector_tags=("ai",),
        last_price=Decimal("100"), one_day_return_pct=1.0,
        five_day_return_pct=5.0, relative_5d_pct=4.0, volume_ratio=1.5, score=10.0,
    )


class _PassThroughLane:
    name = "passthrough"

    def evaluate(self, ranked: list[RankedCandidate], bar_loader):
        return [
            LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=0.5,
                reason="passes through", source_score=c.score,
            )
            for c in ranked
        ]


def test_lane_protocol_accepts_passthrough():
    lane: Lane = _PassThroughLane()
    out = lane.evaluate([_ranked("NVDA"), _ranked("AMD")], bar_loader=lambda s: pd.DataFrame())
    assert len(out) == 2
    assert out[0].lane == "passthrough"
    assert 0.0 <= out[0].conviction <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_strategy_lanes.py::test_lane_protocol_accepts_passthrough -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

Create `src/trading_bot/strategy_lanes.py`:

```python
"""Strategy lanes: independent candidate-scoring strategies that run in parallel
over the stage-1 shortlist. Each lane returns LaneCandidate objects; the
stage-2 orchestrator merges and dedupes across lanes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from trading_bot.screener import RankedCandidate


@dataclass(frozen=True)
class LaneCandidate:
    symbol: str
    lane: str
    conviction: float  # 0.0–1.0
    reason: str
    source_score: float  # the underlying strategy-specific score


class Lane(Protocol):
    name: str

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        """Return the subset of ranked candidates this lane endorses, with conviction."""
        ...
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_strategy_lanes.py::test_lane_protocol_accepts_passthrough -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/strategy_lanes.py tests/test_strategy_lanes.py
git commit -m "feat(lanes): Lane Protocol and LaneCandidate model"
```

---

## Task 12: MomentumLane

**Files:**
- Modify: `src/trading_bot/strategy_lanes.py`
- Test: `tests/test_strategy_lanes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_strategy_lanes.py`:

```python
from trading_bot.strategy_lanes import MomentumLane


def _modest_uptrend(start: float = 100, n: int = 60) -> pd.DataFrame:
    """Alternating +0.6% / -0.4% — net up ~14% over 60d, RSI lands ~60."""
    closes = [start]
    for i in range(n - 1):
        change = 0.6 if i % 2 == 0 else -0.4
        closes.append(closes[-1] * (1 + change / 100))
    return pd.DataFrame({"close": closes, "volume": [1e6] * n})


def _modest_downtrend(start: float = 100, n: int = 60) -> pd.DataFrame:
    """Alternating -0.6% / +0.4% — net down, RSI lands ~40."""
    closes = [start]
    for i in range(n - 1):
        change = -0.6 if i % 2 == 0 else 0.4
        closes.append(closes[-1] * (1 + change / 100))
    return pd.DataFrame({"close": closes, "volume": [1e6] * n})


def _parabolic_uptrend(start: float = 100, n: int = 60) -> pd.DataFrame:
    """3 days +1.5% / 1 day -0.3% — RSI > 70, lane should reject as overbought."""
    closes = [start]
    for i in range(n - 1):
        change = 1.5 if i % 4 != 3 else -0.3
        closes.append(closes[-1] * (1 + change / 100))
    return pd.DataFrame({"close": closes, "volume": [1e6] * n})


def test_momentum_lane_accepts_modest_uptrend():
    lane = MomentumLane()
    bars = {"NVDA": _modest_uptrend()}
    cand = _ranked("NVDA")
    out = lane.evaluate([cand], bar_loader=lambda s: bars.get(s, pd.DataFrame()))
    assert len(out) == 1
    assert out[0].symbol == "NVDA"
    assert out[0].lane == "momentum"
    assert out[0].conviction > 0


def test_momentum_lane_rejects_downtrend():
    lane = MomentumLane()
    bars = {"DOWN": _modest_downtrend()}
    cand = _ranked("DOWN")
    out = lane.evaluate([cand], bar_loader=lambda s: bars.get(s, pd.DataFrame()))
    assert out == []


def test_momentum_lane_rejects_parabolic_overbought():
    lane = MomentumLane()
    bars = {"HOT": _parabolic_uptrend()}
    cand = _ranked("HOT")
    out = lane.evaluate([cand], bar_loader=lambda s: bars.get(s, pd.DataFrame()))
    assert out == []
```

> **Why these fixtures:** RSI is magnitude-weighted, not count-weighted, so a steady "+0.5%/day, no pullback" series produces RSI ≈ 100 (rejected). The 50/50 alternating pattern with ups slightly larger than downs produces RSI ≈ 60 — squarely inside the 55–70 acceptance band.

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_strategy_lanes.py -v
```

Expected: 3 FAILs — `MomentumLane` doesn't exist.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/strategy_lanes.py`:

```python
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD


class MomentumLane:
    """RSI 55–70, MACD bullish, price > 20-EMA, 5d return > 0. Mirrors the
    Plan-1 momentum rules but applied to ranked-shortlist input.
    """
    name = "momentum"

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        out: list[LaneCandidate] = []
        for c in ranked:
            bars = bar_loader(c.symbol)
            if len(bars) < 26:
                continue
            close = bars["close"]
            rsi = float(RSIIndicator(close=close, window=14).rsi().iloc[-1])
            macd_obj = MACD(close=close)
            macd_line = float(macd_obj.macd().iloc[-1])
            macd_signal = float(macd_obj.macd_signal().iloc[-1])
            ema20 = float(EMAIndicator(close=close, window=20).ema_indicator().iloc[-1])
            last = float(close.iloc[-1])

            if not (55 <= rsi <= 70):
                continue
            if macd_line <= macd_signal:
                continue
            if last <= ema20:
                continue
            if c.five_day_return_pct <= 0:
                continue

            # Conviction: how cleanly the trend signal lines up. Range 0.4–0.9.
            conviction = 0.4
            conviction += 0.2 * min((rsi - 55) / 15, 1.0)        # RSI position in band
            conviction += 0.2 * min((macd_line - macd_signal), 1.0)
            conviction += 0.1 * min(c.relative_5d_pct / 5.0, 1.0)
            conviction = max(0.0, min(conviction, 0.9))

            out.append(LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=conviction,
                reason=f"RSI {rsi:.0f}, MACD>signal, price>EMA20, 5d {c.five_day_return_pct:.1f}%",
                source_score=c.score,
            ))
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_strategy_lanes.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/strategy_lanes.py tests/test_strategy_lanes.py
git commit -m "feat(lanes): MomentumLane (RSI/MACD/EMA20/5d-return)"
```

---

## Task 13: MeanReversionLane

**Files:**
- Modify: `src/trading_bot/strategy_lanes.py`
- Test: `tests/test_strategy_lanes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_strategy_lanes.py`:

```python
from trading_bot.strategy_lanes import MeanReversionLane


def test_mean_reversion_lane_accepts_oversold_below_lower_band():
    lane = MeanReversionLane()
    # 30 days of stable price then a sharp drop
    closes = [100] * 25 + [90, 88, 86, 84, 82]
    bars = pd.DataFrame({"close": closes, "volume": [1e6] * len(closes)})
    cand = _ranked("DROP")
    out = lane.evaluate([cand], bar_loader=lambda s: bars)
    assert len(out) == 1
    assert out[0].lane == "mean_reversion"


def test_mean_reversion_lane_rejects_normal_market():
    lane = MeanReversionLane()
    closes = [100 + i * 0.1 for i in range(30)]  # gentle uptrend, no oversold
    bars = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    cand = _ranked("NORMAL")
    out = lane.evaluate([cand], bar_loader=lambda s: bars)
    assert out == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_strategy_lanes.py -v
```

Expected: 2 FAILs.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/strategy_lanes.py`:

```python
from ta.volatility import BollingerBands


class MeanReversionLane:
    """RSI < 30 (oversold) AND price < lower Bollinger Band (2σ, 20-day).
    Conviction higher when farther below the band.
    """
    name = "mean_reversion"

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        out: list[LaneCandidate] = []
        for c in ranked:
            bars = bar_loader(c.symbol)
            if len(bars) < 22:
                continue
            close = bars["close"]
            rsi = float(RSIIndicator(close=close, window=14).rsi().iloc[-1])
            bb = BollingerBands(close=close, window=20, window_dev=2)
            lower = float(bb.bollinger_lband().iloc[-1])
            last = float(close.iloc[-1])

            if rsi >= 30:
                continue
            if last >= lower:
                continue

            # Conviction grows with how much we're below the band, capped at 0.85.
            below_pct = (lower - last) / lower if lower else 0.0
            conviction = 0.4 + min(below_pct * 5.0, 0.45)
            out.append(LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=conviction,
                reason=f"RSI {rsi:.0f}, price ${last:.2f} < lower BB ${lower:.2f}",
                source_score=c.score,
            ))
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_strategy_lanes.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/strategy_lanes.py tests/test_strategy_lanes.py
git commit -m "feat(lanes): MeanReversionLane (RSI<30 + below lower Bollinger)"
```

---

## Task 14: BreakoutLane

**Files:**
- Modify: `src/trading_bot/strategy_lanes.py`
- Test: `tests/test_strategy_lanes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_strategy_lanes.py`:

```python
from trading_bot.strategy_lanes import BreakoutLane


def test_breakout_lane_accepts_new_high_with_volume():
    lane = BreakoutLane()
    # Flat range then break above high on volume spike
    closes = [100] * 20 + [102]  # bar 21 breaks the prior 20-day high (100)
    volumes = [1e6] * 20 + [3e6]  # volume spike on breakout
    bars = pd.DataFrame({"close": closes, "volume": volumes})
    cand = _ranked("BREAK")
    out = lane.evaluate([cand], bar_loader=lambda s: bars)
    assert len(out) == 1
    assert out[0].lane == "breakout"


def test_breakout_lane_rejects_breakout_without_volume():
    lane = BreakoutLane()
    closes = [100] * 20 + [102]
    volumes = [1e6] * 21  # no volume confirmation
    bars = pd.DataFrame({"close": closes, "volume": volumes})
    cand = _ranked("WEAK")
    out = lane.evaluate([cand], bar_loader=lambda s: bars)
    assert out == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_strategy_lanes.py -v
```

Expected: 2 FAILs.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/strategy_lanes.py`:

```python
class BreakoutLane:
    """Price closes above prior 20-day high AND volume > 1.5× 20-day avg."""
    name = "breakout"

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        out: list[LaneCandidate] = []
        for c in ranked:
            bars = bar_loader(c.symbol)
            if len(bars) < 21:
                continue
            close = bars["close"]
            volume = bars["volume"]
            prior_high = float(close.iloc[-21:-1].max())
            last = float(close.iloc[-1])
            avg_vol = float(volume.iloc[-21:-1].mean())
            last_vol = float(volume.iloc[-1])

            if last <= prior_high:
                continue
            if avg_vol <= 0 or last_vol < 1.5 * avg_vol:
                continue

            vol_ratio = last_vol / avg_vol
            conviction = 0.5 + min((vol_ratio - 1.5) * 0.2, 0.4)
            out.append(LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=conviction,
                reason=f"Close ${last:.2f} > 20d high ${prior_high:.2f}, vol {vol_ratio:.1f}× avg",
                source_score=c.score,
            ))
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_strategy_lanes.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/strategy_lanes.py tests/test_strategy_lanes.py
git commit -m "feat(lanes): BreakoutLane (20d-high + 1.5x volume)"
```

---

## Task 15: Stage-2 orchestrator (parallel lanes + merge + opportunities.md)

**Files:**
- Modify: `src/trading_bot/screener.py`
- Test: `tests/test_screener.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_screener.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.screener import (
    RankedCandidate,
    Stage2Result,
    run_stage2,
    write_opportunities_snapshot,
)
from trading_bot.strategy_lanes import LaneCandidate


class _FakeLane:
    def __init__(self, name: str, accepted: list[str]) -> None:
        self.name = name
        self._accepted = set(accepted)

    def evaluate(self, ranked, bar_loader):
        return [
            LaneCandidate(symbol=c.symbol, lane=self.name, conviction=0.6,
                          reason=f"{self.name} pick", source_score=c.score)
            for c in ranked if c.symbol in self._accepted
        ]


def _ranked2(symbol, score=10.0):
    return RankedCandidate(
        symbol=symbol, asset_class="us_equity", sector_tags=(),
        last_price=Decimal("100"), one_day_return_pct=1.0,
        five_day_return_pct=5.0, relative_5d_pct=4.0, volume_ratio=1.5,
        score=score,
    )


def test_run_stage2_merges_lane_outputs_and_dedupes():
    short = [_ranked2("AAA"), _ranked2("BBB"), _ranked2("CCC")]
    lanes = [
        _FakeLane("momentum", ["AAA", "BBB"]),
        _FakeLane("breakout", ["BBB", "CCC"]),
    ]
    res: Stage2Result = run_stage2(short, lanes=lanes, bar_loader=lambda s: pd.DataFrame())
    assert isinstance(res, Stage2Result)
    syms = {c.symbol for c in res.candidates}
    assert syms == {"AAA", "BBB", "CCC"}
    bbb = next(c for c in res.candidates if c.symbol == "BBB")
    assert set(bbb.lane_attribution) == {"momentum", "breakout"}


def test_write_opportunities_snapshot_renders_lanes(tmp_path: Path):
    short = [_ranked2("AAA")]
    lanes = [_FakeLane("momentum", ["AAA"])]
    res = run_stage2(short, lanes=lanes, bar_loader=lambda s: pd.DataFrame())
    path = tmp_path / "opportunities.md"
    write_opportunities_snapshot(res, path, generated_at=datetime(2026, 4, 25, tzinfo=timezone.utc))
    text = path.read_text()
    assert "AAA" in text
    assert "momentum" in text
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_screener.py -v
```

Expected: 2 FAILs — `Stage2Result`, `run_stage2`, `write_opportunities_snapshot` don't exist.

- [ ] **Step 3: Implement**

Append to `src/trading_bot/screener.py`:

```python
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import field
from datetime import datetime
from pathlib import Path

from trading_bot.strategy_lanes import Lane, LaneCandidate


@dataclass(frozen=True)
class MergedCandidate:
    symbol: str
    asset_class: str
    sector_tags: tuple[str, ...]
    last_price: Decimal
    score: float
    conviction: float  # max conviction across lanes that endorsed it
    lane_attribution: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Stage2Result:
    candidates: list[MergedCandidate]
    lane_counts: dict[str, int]


def run_stage2(
    shortlist: list[RankedCandidate],
    *,
    lanes: list[Lane],
    bar_loader: Callable[[str], pd.DataFrame],
) -> Stage2Result:
    """Run all lanes in parallel over the shortlist, merge, dedupe by symbol.

    Conviction takes the max across lanes; lane attribution preserves which lanes
    endorsed each name; reasons concatenate.
    """
    with ThreadPoolExecutor(max_workers=max(1, len(lanes))) as ex:
        results = list(ex.map(lambda lane: lane.evaluate(shortlist, bar_loader), lanes))

    by_symbol: dict[str, list[LaneCandidate]] = {}
    lane_counts: dict[str, int] = {}
    for lane_out, lane in zip(results, lanes, strict=False):
        lane_counts[lane.name] = len(lane_out)
        for lc in lane_out:
            by_symbol.setdefault(lc.symbol, []).append(lc)

    short_by_sym = {c.symbol: c for c in shortlist}
    merged: list[MergedCandidate] = []
    for symbol, lane_cands in by_symbol.items():
        ref = short_by_sym.get(symbol)
        if ref is None:
            continue
        max_conviction = max(lc.conviction for lc in lane_cands)
        merged.append(MergedCandidate(
            symbol=symbol,
            asset_class=ref.asset_class,
            sector_tags=ref.sector_tags,
            last_price=ref.last_price,
            score=ref.score,
            conviction=max_conviction,
            lane_attribution=tuple(sorted({lc.lane for lc in lane_cands})),
            reasons=tuple(lc.reason for lc in lane_cands),
        ))

    merged.sort(key=lambda m: (m.conviction, m.score), reverse=True)
    return Stage2Result(candidates=merged, lane_counts=lane_counts)


def render_opportunities_snapshot(result: Stage2Result, *, generated_at: datetime) -> str:
    lines = [
        "# Opportunities (Stage-2)",
        "",
        f"Generated: {generated_at.isoformat(timespec='seconds')}",
        f"Total endorsed: {len(result.candidates)}",
        "",
        "## Lane Counts",
        "",
    ]
    for lane, count in result.lane_counts.items():
        lines.append(f"- {lane}: {count}")
    lines.extend(["", "## Ranked Candidates", ""])
    for idx, c in enumerate(result.candidates, start=1):
        lines.append(f"### {idx}. {c.symbol} ({c.asset_class})")
        lines.append("")
        lines.append(f"- Lanes: {', '.join(c.lane_attribution)}")
        lines.append(f"- Conviction: {c.conviction:.2f}")
        lines.append(f"- Stage-1 score: {c.score:.2f}")
        lines.append(f"- Last price: ${c.last_price}")
        if c.sector_tags:
            lines.append(f"- Sectors: {', '.join(c.sector_tags)}")
        for r in c.reasons:
            lines.append(f"- Why: {r}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_opportunities_snapshot(result: Stage2Result, path: Path, *, generated_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_opportunities_snapshot(result, generated_at=generated_at))
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_screener.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```
git add src/trading_bot/screener.py tests/test_screener.py
git commit -m "feat(screener): stage-2 orchestrator (parallel lanes + merge + opportunities.md)"
```

---

## Task 16: CLI command `bot rank` and orchestrator integration

**Files:**
- Modify: `src/trading_bot/cli.py`
- Modify: `src/trading_bot/orchestrator.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test for CLI**

Append to `tests/test_cli.py`:

```python
def test_rank_writes_opportunities(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()

    from decimal import Decimal
    from trading_bot.universe import LiquidAsset

    with patch("trading_bot.cli.build_universe") as mock_build, \
         patch("trading_bot.cli.AlpacaClient"), \
         patch("trading_bot.cli.MarketDataClient") as mock_md:
        mock_build.return_value = [
            LiquidAsset(symbol="AAA", name="AAA", asset_class="us_equity",
                        exchange="NASDAQ", last_price=Decimal("100"),
                        avg_dollar_volume=Decimal("1e9"), fractionable=True,
                        sector_tags=("ai",)),
        ]
        # Inject bars where AAA looks bullish on momentum
        import pandas as pd
        def fake_bars(symbol, lookback_days=60):
            closes = [100 + i * 0.5 for i in range(60)]
            return pd.DataFrame({"close": closes, "volume": [1e6] * 60})
        mock_md.return_value.get_daily_bars.side_effect = fake_bars

        runner = CliRunner()
        result = runner.invoke(cli, ["rank"])

    assert result.exit_code == 0, result.output
    text = (tmp_path / "strategy" / "opportunities.md").read_text()
    assert "AAA" in text
```

- [ ] **Step 2: Write failing test for orchestrator**

Append to `tests/test_orchestrator.py`:

```python
from pathlib import Path

from trading_bot.orchestrator import load_ranked_watchlist


def test_load_ranked_watchlist_reads_opportunities(tmp_path: Path):
    md = tmp_path / "opportunities.md"
    md.write_text(
        "# Opportunities (Stage-2)\n\n"
        "## Ranked Candidates\n\n"
        "### 1. NVDA (us_equity)\n\n"
        "- Lanes: momentum\n"
        "- Conviction: 0.75\n\n"
        "### 2. BTC/USD (crypto)\n\n"
        "- Lanes: breakout\n"
        "- Conviction: 0.60\n"
    )
    entries = load_ranked_watchlist(md)
    syms = [e.symbol for e in entries]
    assert syms == ["NVDA", "BTC/USD"]
    assert entries[0].asset_class == "us_equity"
    assert entries[1].asset_class == "crypto"
```

- [ ] **Step 3: Run tests to verify they fail**

```
pytest tests/test_cli.py::test_rank_writes_opportunities tests/test_orchestrator.py::test_load_ranked_watchlist_reads_opportunities -v
```

Expected: 2 FAILs.

- [ ] **Step 4: Implement CLI command**

Add to `src/trading_bot/cli.py` (top of imports):

```python
from trading_bot.screener import build_stage1_shortlist, run_stage2, write_opportunities_snapshot
from trading_bot.strategy_lanes import BreakoutLane, MeanReversionLane, MomentumLane
```

Add this command:

```python
@cli.command("rank")
def rank_command() -> None:
    """Run stage-1 + stage-2 screener; write strategy/opportunities.md."""
    settings = Settings.from_env()
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

    universe = build_universe(alpaca, bar_loader=bar_loader_short)
    shortlist = build_stage1_shortlist(universe, bar_loader=bar_loader_short, top_n=100)

    lanes = [MomentumLane(), MeanReversionLane(), BreakoutLane()]
    result = run_stage2(shortlist, lanes=lanes, bar_loader=bar_loader_long)
    write_opportunities_snapshot(
        result,
        Path("strategy/opportunities.md"),
        generated_at=datetime.now(timezone.utc),
    )
    click.echo(f"Stage-2 ranked {len(result.candidates)} candidates across {len(lanes)} lanes")
```

- [ ] **Step 5: Implement orchestrator loader**

Add to `src/trading_bot/orchestrator.py` (top of imports):

```python
import re
from pathlib import Path

from trading_bot.state import WatchlistEntry
```

Add this function (top-level, after the existing dataclasses):

```python
def load_ranked_watchlist(path: Path) -> list[WatchlistEntry]:
    """Parse strategy/opportunities.md and return WatchlistEntry list in rank order.

    Entries look like:
        ### 1. NVDA (us_equity)
        ### 2. BTC/USD (crypto)
    """
    if not path.exists():
        return []
    text = path.read_text()
    out: list[WatchlistEntry] = []
    pattern = re.compile(r"^###\s+\d+\.\s+(\S+)\s+\(([^)]+)\)\s*$", re.MULTILINE)
    for match in pattern.finditer(text):
        symbol = match.group(1)
        asset_class_raw = match.group(2)
        asset_class = "crypto" if "crypto" in asset_class_raw.lower() else "stock"
        out.append(WatchlistEntry(symbol=symbol, asset_class=asset_class, notes=""))
    return out
```

- [ ] **Step 6: Run tests to verify they pass**

```
pytest tests/test_cli.py tests/test_orchestrator.py -v
```

Expected: all PASS.

- [ ] **Step 7: Run the full test suite**

```
pytest -q
```

Expected: all PASS, no regressions.

- [ ] **Step 8: Commit**

```
git add src/trading_bot/cli.py src/trading_bot/orchestrator.py tests/test_cli.py tests/test_orchestrator.py
git commit -m "feat(screener): bot rank command + orchestrator load_ranked_watchlist"
```

---

## Self-review

Before declaring done:

1. **Spec coverage:** Plan 5a covers spec sections 6.1 (universe.py, screener.py, strategy_lanes.py), 6.2 (alpaca_client + orchestrator + cli changes), 6.3 (latest_intelligence.md, opportunities.md writers). Sentiment lanes / event lanes deferred to Plan 5d as planned.
2. **Placeholder scan:** No "TBD" / "implement later" / "similar to task N" in any step. Every code block is complete.
3. **Type consistency:** `RankedCandidate`, `LaneCandidate`, `MergedCandidate`, `Stage2Result` defined exactly once each; method signatures match across `Lane` Protocol and concrete lanes.
4. **Tests run after every task** with explicit expected outcomes.

## Open follow-ups (deferred to later sub-plans)

- **Plan 5b:** Dynamic risk multipliers consume `MergedCandidate.conviction` for `conviction_mult`.
- **Plan 5c:** Cadence runner triggers `bot screen-universe` daily 08:30 ET and `bot rank` on tier schedule.
- **Plan 5d:** EventLane (sentiment-driven) added once tweet pipeline ships.
- **Plan 5e:** Rich emails read `opportunities.md` for the "Top 5 next cycle" section.
