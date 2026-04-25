# Trading Bot — Strategy Engine + First Paper Trade (Plan 2 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the strategy engine and orchestrator on top of Plan 1's foundation, place the bot's first paper trade, and email a daily P&L report.

**Architecture:** Layered modules — `market_data.py` fetches bars + computes indicators, `strategy.py` applies the momentum entry rule, `state.py` reconciles positions with Alpaca + the journal, `orchestrator.py` chains everything through the existing `RiskManager` and `AlpacaClient`. CLI commands `bot scan` (place trades) and `bot daily-report` (send summary email) make it runnable today; Plan 3 wraps these in scheduled routines + an MCP server.

**Tech Stack:** Python 3.11+, alpaca-py, pandas, ta (technical indicators), Jinja2 (email templates), pytest. Adds `pandas`, `ta`, `numpy` to dependencies.

**This plan produces:**
- A `bot scan` CLI command that fetches current bars, computes signals, and places a real paper trade if rules + risk checks pass
- A `bot daily-report` CLI command that emails a P&L vs SPY summary
- A `strategy/watchlist.md` and `strategy/rules.md` documenting current rules in plain English
- Unit tests for every module (no live API calls in unit tests)
- A manual run that lands the bot's first paper trade

**Spec reference:** `docs/superpowers/specs/2026-04-25-trading-bot-design.md` Sections 4 (Strategy Framework), 3.6 (Reporting). MCP server abstraction (3.2) deferred to Plan 3.

---

## File Structure

```
/Users/bharathkandala/Trading/
├── pyproject.toml                          # Add pandas, ta, numpy
├── strategy/
│   ├── config.yaml                         # (existing)
│   ├── watchlist.md                        # NEW: human-readable symbol list
│   ├── rules.md                            # NEW: strategy rules in plain English
│   └── watchlist.yaml                      # NEW: machine-readable symbol metadata
├── src/trading_bot/
│   ├── (existing files)
│   ├── market_data.py                      # NEW: bars + indicators
│   ├── strategy.py                         # NEW: momentum rule + Signal type
│   ├── state.py                            # NEW: position reconciliation, P&L state
│   ├── orchestrator.py                     # NEW: scan→signal→risk→execute→journal
│   ├── reports.py                          # NEW: HTML daily report builder
│   └── cli.py                              # MODIFY: add `scan`, `daily-report` cmds
└── tests/
    ├── test_market_data.py                 # NEW
    ├── test_strategy.py                    # NEW
    ├── test_state.py                       # NEW
    ├── test_orchestrator.py                # NEW
    ├── test_reports.py                     # NEW
    └── test_cli.py                         # MODIFY: add scan + daily-report tests
```

**Watchlist (initial, calibrated for $15k paper account):**
- ETFs: `SPY`, `QQQ` (broad market exposure)
- Stocks: `AAPL`, `MSFT`, `AMD` (large-cap, mid-priced, liquid)
- Crypto: `BTC/USD`, `ETH/USD` (fractional shares allow tiny positions)

This gives 7 symbols, all under or near the $750 concentration cap (5% of $15k) per single-share or fractional buy.

---

## Task 1: Add Dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Update dependencies block**

In `pyproject.toml` `dependencies` list, ADD: `"pandas>=2.1"`, `"numpy>=1.26"`, `"ta>=0.11"`.

The block should now read:

```toml
dependencies = [
    "alpaca-py>=0.30.0",
    "pydantic>=2.5",
    "pydantic-settings>=2.1",
    "pyyaml>=6.0",
    "sqlalchemy>=2.0",
    "jinja2>=3.1",
    "click>=8.1",
    "python-dotenv>=1.0",
    "pandas>=2.1",
    "numpy>=1.26",
    "ta>=0.11",
]
```

- [ ] **Step 2: Install**

```bash
.venv/bin/pip install -e ".[dev]"
```
Expected: pandas, numpy, ta installed without errors.

- [ ] **Step 3: Smoke import**

```bash
.venv/bin/python -c "import pandas, numpy, ta; print(pandas.__version__, numpy.__version__, ta.__version__)"
```
Expected: prints three version numbers.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add pandas, numpy, ta for indicator computation"
```

---

## Task 2: Watchlist Configuration

**Files:**
- Create: `strategy/watchlist.yaml`
- Create: `strategy/watchlist.md`

- [ ] **Step 1: Create `strategy/watchlist.yaml`**

```yaml
# strategy/watchlist.yaml — symbols the bot may trade
# Calibrated for ~$15k paper account, 5% concentration cap → ~$750 per symbol.

symbols:
  - symbol: SPY
    asset_class: stock
    notes: S&P 500 ETF, broad market exposure
  - symbol: QQQ
    asset_class: stock
    notes: Nasdaq 100 ETF, tech-heavy growth
  - symbol: AAPL
    asset_class: stock
    notes: Apple, large-cap, highly liquid
  - symbol: MSFT
    asset_class: stock
    notes: Microsoft, large-cap, highly liquid
  - symbol: AMD
    asset_class: stock
    notes: AMD, mid-priced, semiconductor exposure
  - symbol: BTC/USD
    asset_class: crypto
    notes: Bitcoin, fractional shares
  - symbol: ETH/USD
    asset_class: crypto
    notes: Ethereum, fractional shares
```

- [ ] **Step 2: Create `strategy/watchlist.md`**

```markdown
# Watchlist

Symbols the bot may trade in Phase 1. Calibrated for the current paper account size.

## Stocks / ETFs
- **SPY** — S&P 500 ETF, broad market exposure
- **QQQ** — Nasdaq 100 ETF, tech-heavy growth
- **AAPL** — Apple, large-cap, highly liquid
- **MSFT** — Microsoft, large-cap, highly liquid
- **AMD** — AMD, mid-priced, semiconductor exposure

## Crypto
- **BTC/USD** — Bitcoin (fractional shares)
- **ETH/USD** — Ethereum (fractional shares)

## Sizing
- 5% concentration cap per symbol (~$750 at current $15k equity)
- 10% max position cap (~$1,500)
- 1% per-trade risk cap (~$150)

## Adding/Removing Symbols
Edit `watchlist.yaml`. The bot will pick up changes at the next scan.
```

- [ ] **Step 3: Commit**

```bash
git add strategy/watchlist.yaml strategy/watchlist.md
git commit -m "docs(strategy): initial watchlist with 7 symbols calibrated for $15k account"
```

---

## Task 3: Strategy Rules Document

**Files:**
- Create: `strategy/rules.md`

- [ ] **Step 1: Author `strategy/rules.md`**

```markdown
# Strategy Rules — Phase 1

**Last updated:** 2026-04-25
**Phase:** 1 (rule-based momentum)

## Active Strategies

### 1. Momentum Entry (long only)

**When to enter a BUY:**
- 14-day RSI is between 55 and 70 (rising but not overbought)
- MACD line is above the signal line (bullish momentum)
- Current price is above the 20-day EMA
- 5-day return is positive

**When to skip:**
- RSI > 70 (already overbought, late to the party)
- RSI < 50 (no momentum, would be mean-reversion territory)
- Price below 20-day EMA (downtrend)

**Position sizing:**
- Risk 0.5% of equity per trade (target half of the 1% per-trade cap to leave room for stop slippage)
- Stop-loss at the 20-day EMA OR 5% below entry, whichever is closer
- Position size = (risk dollars) / (entry - stop)

**Exit (stop-loss + monitoring; managed in Plan 3):**
- Hard stop-loss at the calculated price (set atomically with entry)
- Trail stop to entry + 2% once unrealized gain ≥ 5%

## Inactive Strategies (added in later plans)

- Mean Reversion (Plan 3)
- Sentiment overlay (Plan 3 — will use Alpaca news + GDELT/SEC EDGAR/FRED feeds via MCP)
- Options (Plan 4 — covered calls, protective puts only)

## Performance Targets (12-month rolling)

- Sharpe ratio > 1.0
- Max drawdown < 15%
- Annualized return > S&P 500
- Win rate > 50%
- Profit factor > 1.5

## Evolution Log

(Claude updates this section when rules change. Each entry lists date + reason + rule diff.)

- 2026-04-25 — initial rules authored.
```

- [ ] **Step 2: Commit**

```bash
git add strategy/rules.md
git commit -m "docs(strategy): initial Phase 1 momentum rules"
```

---

## Task 4: Market Data Module

**Files:**
- Create: `src/trading_bot/market_data.py`
- Create: `tests/test_market_data.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market_data.py
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.market_data import (
    Bar,
    Indicators,
    MarketDataClient,
    compute_indicators,
)


def _make_bars_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=pd.date_range("2026-04-01", periods=n, freq="D", tz="UTC"),
    )


def test_compute_indicators_returns_expected_keys():
    df = _make_bars_df([100 + i * 0.5 for i in range(40)])
    ind = compute_indicators(df)
    assert isinstance(ind, Indicators)
    assert isinstance(ind.rsi_14, float)
    assert isinstance(ind.macd, float)
    assert isinstance(ind.macd_signal, float)
    assert isinstance(ind.ema_20, float)
    assert isinstance(ind.return_5d, float)
    assert isinstance(ind.last_close, float)


def test_compute_indicators_rsi_high_for_uptrend():
    df = _make_bars_df([100 + i for i in range(40)])
    ind = compute_indicators(df)
    assert ind.rsi_14 > 70


def test_compute_indicators_rsi_low_for_downtrend():
    df = _make_bars_df([100 - i for i in range(40)])
    ind = compute_indicators(df)
    assert ind.rsi_14 < 30


def test_compute_indicators_handles_short_series():
    df = _make_bars_df([100.0, 101.0, 102.0])
    with pytest.raises(ValueError, match="at least"):
        compute_indicators(df)


def test_market_data_client_get_bars(monkeypatch):
    fake_settings = MagicMock(
        alpaca_api_key="k",
        alpaca_api_secret="s",
        alpaca_base_url="https://paper-api.alpaca.markets/v2",
    )

    fake_bar = MagicMock()
    fake_bar.timestamp = datetime(2026, 4, 25, tzinfo=timezone.utc)
    fake_bar.open = 195.0
    fake_bar.high = 196.0
    fake_bar.low = 194.0
    fake_bar.close = 195.5
    fake_bar.volume = 1_000_000

    fake_response = MagicMock()
    fake_response.data = {"AAPL": [fake_bar] * 30}

    with patch("trading_bot.market_data.StockHistoricalDataClient") as MockData:
        MockData.return_value.get_stock_bars.return_value = fake_response
        client = MarketDataClient(fake_settings)
        df = client.get_daily_bars("AAPL", lookback_days=30)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 30
        assert "close" in df.columns
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_market_data.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/market_data.py
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

from trading_bot.config import Settings
from trading_bot.exceptions import AlpacaClientError


MIN_BARS_FOR_INDICATORS = 26  # MACD needs 26 periods of history


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Indicators:
    last_close: float
    rsi_14: float
    macd: float
    macd_signal: float
    ema_20: float
    return_5d: float


def compute_indicators(bars: pd.DataFrame) -> Indicators:
    """Compute all indicators used by Phase 1 strategy.

    Expects a DataFrame with at least a `close` column and >= 26 rows.
    """
    if len(bars) < MIN_BARS_FOR_INDICATORS:
        raise ValueError(
            f"compute_indicators requires at least {MIN_BARS_FOR_INDICATORS} bars; got {len(bars)}"
        )
    close = bars["close"]
    rsi = RSIIndicator(close=close, window=14).rsi().iloc[-1]
    macd_obj = MACD(close=close)
    macd_val = macd_obj.macd().iloc[-1]
    macd_sig = macd_obj.macd_signal().iloc[-1]
    ema20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
    ret_5d = (close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 else 0.0
    return Indicators(
        last_close=float(close.iloc[-1]),
        rsi_14=float(rsi),
        macd=float(macd_val),
        macd_signal=float(macd_sig),
        ema_20=float(ema20),
        return_5d=float(ret_5d),
    )


class MarketDataClient:
    """Fetch historical bars from Alpaca."""

    def __init__(self, settings: Settings) -> None:
        self._client = StockHistoricalDataClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
        )

    def get_daily_bars(self, symbol: str, lookback_days: int = 60) -> pd.DataFrame:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(timezone.utc) - timedelta(days=lookback_days * 2),
                limit=lookback_days,
            )
            resp = self._client.get_stock_bars(req)
        except Exception as e:
            raise AlpacaClientError(f"get_daily_bars({symbol}) failed: {e}") from e
        bars = resp.data.get(symbol, [])
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(
            [
                {
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
                for b in bars
            ],
            index=pd.DatetimeIndex([b.timestamp for b in bars], name="timestamp"),
        )
        return df.tail(lookback_days)
```

- [ ] **Step 4: Run to confirm pass**

```bash
.venv/bin/pytest tests/test_market_data.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/market_data.py tests/test_market_data.py
git commit -m "feat(market_data): bar fetching + RSI/MACD/EMA indicator computation"
```

---

## Task 5: Strategy Module

**Files:**
- Create: `src/trading_bot/strategy.py`
- Create: `tests/test_strategy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_strategy.py
from decimal import Decimal

import pytest

from trading_bot.market_data import Indicators
from trading_bot.strategy import MomentumStrategy, Signal, SignalAction


def _ind(rsi: float, macd: float, macd_sig: float, ema: float, ret5: float, close: float) -> Indicators:
    return Indicators(
        last_close=close,
        rsi_14=rsi,
        macd=macd,
        macd_signal=macd_sig,
        ema_20=ema,
        return_5d=ret5,
    )


def test_momentum_emits_buy_when_all_rules_pass():
    s = MomentumStrategy()
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.BUY
    assert sig.symbol == "AAPL"
    assert sig.entry_price == Decimal("195")
    assert sig.stop_loss_price < sig.entry_price


def test_momentum_holds_when_rsi_too_high():
    s = MomentumStrategy()
    ind = _ind(rsi=75, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD


def test_momentum_holds_when_macd_bearish():
    s = MomentumStrategy()
    ind = _ind(rsi=60, macd=0.1, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD


def test_momentum_holds_when_below_ema():
    s = MomentumStrategy()
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=200, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD


def test_momentum_position_size_respects_risk_budget():
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.5"))
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    # risk budget = 0.5% of 15000 = $75
    # stop is min(EMA=190, close*0.95=185.25) = 185.25
    # per-share risk = 195 - 185.25 = 9.75
    # qty = 75 / 9.75 ≈ 7.69 → integer floor = 7
    assert sig.qty == Decimal("7")
    assert sig.stop_loss_price == Decimal("185.25")


def test_momentum_skips_when_qty_zero():
    """If risk math yields qty < 1 (e.g., huge stop distance vs tiny budget), no signal."""
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.01"))  # only $1.50 risk
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_strategy.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/strategy.py
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import Enum

from trading_bot.market_data import Indicators


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: SignalAction
    qty: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    reason: str


class MomentumStrategy:
    """Phase 1 momentum entry rule. Long-only, integer share quantities."""

    def __init__(
        self,
        rsi_lower: float = 55.0,
        rsi_upper: float = 70.0,
        per_trade_risk_pct: Decimal = Decimal("0.5"),
        stop_pct: Decimal = Decimal("0.05"),
    ) -> None:
        self._rsi_lower = rsi_lower
        self._rsi_upper = rsi_upper
        self._risk_pct = per_trade_risk_pct
        self._stop_pct = stop_pct

    def evaluate(self, symbol: str, ind: Indicators, equity: Decimal) -> Signal:
        # Rule checks (HOLD if any fail)
        if not (self._rsi_lower <= ind.rsi_14 <= self._rsi_upper):
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"rsi {ind.rsi_14:.1f} outside [{self._rsi_lower}, {self._rsi_upper}]")
        if ind.macd <= ind.macd_signal:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"macd {ind.macd:.3f} not above signal {ind.macd_signal:.3f}")
        if ind.last_close <= ind.ema_20:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"close {ind.last_close:.2f} not above EMA20 {ind.ema_20:.2f}")
        if ind.return_5d <= 0:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"5d return {ind.return_5d:.4f} not positive")

        # Sizing: stop = max(EMA20, close*(1 - stop_pct))  ← whichever is CLOSER to entry
        entry = Decimal(str(ind.last_close))
        ema_stop = Decimal(str(ind.ema_20))
        pct_stop = entry * (Decimal("1") - self._stop_pct)
        stop = max(ema_stop, pct_stop)  # closer to entry = larger value
        per_share_risk = entry - stop
        if per_share_risk <= 0:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          "stop not below entry — anomaly")

        risk_budget = (equity * self._risk_pct / Decimal("100")).quantize(Decimal("0.01"))
        raw_qty = risk_budget / per_share_risk
        qty = raw_qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if qty < 1:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"calculated qty {raw_qty:.4f} < 1 share")

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            qty=qty,
            entry_price=entry,
            stop_loss_price=stop.quantize(Decimal("0.01")),
            reason=f"rsi={ind.rsi_14:.1f} macd>{ind.macd_signal:.3f} close>EMA20",
        )
```

- [ ] **Step 4: Run to confirm pass**

```bash
.venv/bin/pytest tests/test_strategy.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/strategy.py tests/test_strategy.py
git commit -m "feat(strategy): momentum entry rule with risk-budget position sizing"
```

---

## Task 6: Position State + Watchlist Loader

**Files:**
- Create: `src/trading_bot/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.alpaca_client import Position
from trading_bot.state import (
    WatchlistEntry,
    has_open_position,
    load_watchlist,
)


def test_load_watchlist_parses_yaml(tmp_path: Path):
    p = tmp_path / "watchlist.yaml"
    p.write_text(
        """
symbols:
  - symbol: SPY
    asset_class: stock
    notes: ETF
  - symbol: BTC/USD
    asset_class: crypto
    notes: BTC
"""
    )
    wl = load_watchlist(p)
    assert len(wl) == 2
    assert isinstance(wl[0], WatchlistEntry)
    assert wl[0].symbol == "SPY"
    assert wl[0].asset_class == "stock"
    assert wl[1].symbol == "BTC/USD"
    assert wl[1].asset_class == "crypto"


def test_has_open_position_true():
    pos = Position(
        symbol="AAPL",
        qty=Decimal("3"),
        market_value=Decimal("585"),
        avg_entry_price=Decimal("195"),
        unrealized_pl=Decimal("0"),
        asset_class="us_equity",
    )
    assert has_open_position("AAPL", [pos]) is True


def test_has_open_position_false():
    assert has_open_position("AAPL", []) is False


def test_has_open_position_normalizes_crypto_symbol():
    pos = Position(
        symbol="BTCUSD",
        qty=Decimal("0.001"),
        market_value=Decimal("70"),
        avg_entry_price=Decimal("70000"),
        unrealized_pl=Decimal("0"),
        asset_class="crypto",
    )
    assert has_open_position("BTC/USD", [pos]) is True
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_state.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/state.py
from dataclasses import dataclass
from pathlib import Path

import yaml

from trading_bot.alpaca_client import Position
from trading_bot.exceptions import ConfigError


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    asset_class: str
    notes: str


def load_watchlist(path: Path) -> list[WatchlistEntry]:
    if not path.exists():
        raise ConfigError(f"watchlist not found: {path}")
    raw = yaml.safe_load(path.read_text())
    out: list[WatchlistEntry] = []
    for entry in raw.get("symbols", []):
        out.append(
            WatchlistEntry(
                symbol=entry["symbol"],
                asset_class=entry["asset_class"],
                notes=entry.get("notes", ""),
            )
        )
    return out


def _normalize(symbol: str) -> str:
    """Normalize symbols across Alpaca's stock vs crypto representations.

    Alpaca returns crypto positions as 'BTCUSD' but the watchlist uses 'BTC/USD'.
    """
    return symbol.replace("/", "").upper()


def has_open_position(symbol: str, positions: list[Position]) -> bool:
    target = _normalize(symbol)
    return any(_normalize(p.symbol) == target for p in positions)
```

- [ ] **Step 4: Run to confirm pass**

```bash
.venv/bin/pytest tests/test_state.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/state.py tests/test_state.py
git commit -m "feat(state): watchlist loader + position lookup with crypto symbol normalization"
```

---

## Task 7: Trade Orchestrator

**Files:**
- Create: `src/trading_bot/orchestrator.py`
- Create: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trading_bot.alpaca_client import (
    AccountSnapshot,
    AssetClass,
    OrderResult,
    OrderSide,
    Position,
)
from trading_bot.market_data import Indicators
from trading_bot.orchestrator import ScanResult, TradeOrchestrator
from trading_bot.state import WatchlistEntry
from trading_bot.strategy import Signal, SignalAction


def _config():
    from trading_bot.config import (
        AllocationConfig,
        AppConfig,
        EmailConfig,
        RegimeAllocation,
        RiskConfig,
        StorageConfig,
    )
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0,
            weekly_loss_limit_pct=5.0,
            per_trade_risk_pct=1.0,
            max_position_pct=10.0,
            max_symbol_concentration_pct=5.0,
            max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(
            stocks_max_pct=70.0, crypto_max_pct=30.0, options_max_pct=20.0, cash_floor_pct=10.0
        ),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
            "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
            "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
            "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
        },
        email=EmailConfig(to="t@x.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("15000"),
        cash=Decimal("15000"),
        buying_power=Decimal("30000"),
        portfolio_value=Decimal("15000"),
    )


def _bars():
    return pd.DataFrame(
        {"close": [100 + i for i in range(40)],
         "open": [100 + i for i in range(40)],
         "high": [101 + i for i in range(40)],
         "low": [99 + i for i in range(40)],
         "volume": [1_000_000] * 40},
        index=pd.date_range("2026-04-01", periods=40, freq="D", tz="UTC"),
    )


@pytest.fixture
def watchlist():
    return [
        WatchlistEntry(symbol="AAPL", asset_class="stock", notes=""),
        WatchlistEntry(symbol="MSFT", asset_class="stock", notes=""),
    ]


def test_orchestrator_skips_existing_positions(watchlist):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = [
        Position(
            symbol="AAPL", qty=Decimal("3"), market_value=Decimal("585"),
            avg_entry_price=Decimal("195"), unrealized_pl=Decimal("0"), asset_class="us_equity",
        )
    ]
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    result = orch.scan(watchlist=watchlist)
    assert isinstance(result, ScanResult)
    skipped_aapl = [d for d in result.decisions if d.symbol == "AAPL"][0]
    assert skipped_aapl.action == "skipped_existing_position"
    alpaca.place_order_with_stop_loss.assert_not_called()


def test_orchestrator_places_order_on_buy_signal(watchlist, monkeypatch):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()

    # force a deterministic BUY signal for MSFT
    forced = Signal(
        symbol="MSFT",
        action=SignalAction.BUY,
        qty=Decimal("2"),
        entry_price=Decimal("139"),
        stop_loss_price=Decimal("133"),
        reason="forced",
    )

    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-1", stop_loss_order_id="s-1"
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    monkeypatch.setattr(orch._strategy, "evaluate",
                        lambda sym, ind, equity: forced if sym == "MSFT" else
                        Signal(sym, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"), "x"))

    result = orch.scan(watchlist=watchlist)
    placed = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert placed.action == "placed_order"
    assert placed.entry_order_id == "e-1"
    alpaca.place_order_with_stop_loss.assert_called_once()
    journal.append.assert_called_once()


def test_orchestrator_skips_on_risk_violation(watchlist, monkeypatch):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()

    # signal with way oversized qty → risk manager rejects
    forced = Signal(
        symbol="MSFT",
        action=SignalAction.BUY,
        qty=Decimal("100"),
        entry_price=Decimal("139"),
        stop_loss_price=Decimal("100"),  # huge risk per share × 100 shares
        reason="forced bad",
    )

    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    monkeypatch.setattr(orch._strategy, "evaluate",
                        lambda sym, ind, equity: forced if sym == "MSFT" else
                        Signal(sym, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"), "x"))

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "rejected_by_risk"
    assert "per_trade_risk_pct" in msft.reason or "max_position_pct" in msft.reason
    alpaca.place_order_with_stop_loss.assert_not_called()


def test_orchestrator_skips_when_bars_too_short(watchlist):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars().head(5)  # only 5 bars
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    result = orch.scan(watchlist=watchlist)
    for d in result.decisions:
        assert d.action == "skipped_insufficient_data"
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_orchestrator.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/orchestrator.py
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.alpaca_client import (
    AlpacaClient,
    AssetClass,
    OrderRequest,
    OrderSide,
)
from trading_bot.config import AppConfig
from trading_bot.exceptions import AlpacaClientError, RiskRuleViolation
from trading_bot.market_data import (
    MIN_BARS_FOR_INDICATORS,
    MarketDataClient,
    compute_indicators,
)
from trading_bot.risk_manager import RiskManager, RiskState
from trading_bot.state import WatchlistEntry, has_open_position
from trading_bot.strategy import MomentumStrategy, Signal, SignalAction
from trading_bot.trade_journal import TradeJournal, TradeRecord


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: str  # placed_order | skipped_existing_position | rejected_by_risk |
                 # skipped_insufficient_data | hold | api_error
    reason: str = ""
    entry_order_id: str = ""
    stop_loss_order_id: str = ""


@dataclass(frozen=True)
class ScanResult:
    decisions: list[Decision]
    timestamp: datetime


class TradeOrchestrator:
    """Scans watchlist, generates signals, gates through risk, places orders."""

    def __init__(
        self,
        *,
        config: AppConfig,
        market_data: MarketDataClient,
        alpaca: AlpacaClient,
        journal: TradeJournal,
        regime: str = "trending_up",
        strategy: MomentumStrategy | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self._cfg = config
        self._market = market_data
        self._alpaca = alpaca
        self._journal = journal
        self._regime = regime
        self._strategy = strategy or MomentumStrategy()
        self._risk = risk_manager or RiskManager(config)

    def _build_state(self) -> RiskState:
        # Plan 3 will compute live P&L. For now, assume zero loss (no halts).
        return RiskState(
            daily_pnl_pct=Decimal("0"),
            weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0,
            halted=False,
        )

    def scan(self, *, watchlist: list[WatchlistEntry]) -> ScanResult:
        account = self._alpaca.get_account()
        positions = self._alpaca.get_positions()
        state = self._build_state()
        decisions: list[Decision] = []

        for entry in watchlist:
            symbol = entry.symbol
            if has_open_position(symbol, positions):
                decisions.append(Decision(symbol=symbol, action="skipped_existing_position"))
                continue

            try:
                bars = self._market.get_daily_bars(symbol, lookback_days=60)
            except AlpacaClientError as e:
                decisions.append(Decision(symbol=symbol, action="api_error", reason=str(e)))
                continue

            if len(bars) < MIN_BARS_FOR_INDICATORS:
                decisions.append(
                    Decision(symbol=symbol, action="skipped_insufficient_data",
                             reason=f"{len(bars)} bars < {MIN_BARS_FOR_INDICATORS}")
                )
                continue

            ind = compute_indicators(bars)
            sig = self._strategy.evaluate(symbol, ind, equity=account.equity)
            if sig.action != SignalAction.BUY:
                decisions.append(Decision(symbol=symbol, action="hold", reason=sig.reason))
                continue

            asset_class = AssetClass.CRYPTO if entry.asset_class == "crypto" else AssetClass.STOCK
            order = OrderRequest(
                symbol=symbol,
                qty=sig.qty,
                side=OrderSide.BUY,
                asset_class=asset_class,
                limit_price=sig.entry_price,
                stop_loss_price=sig.stop_loss_price,
            )

            try:
                self._risk.check(order, account=account, positions=positions,
                                 state=state, regime=self._regime)
            except RiskRuleViolation as e:
                decisions.append(Decision(symbol=symbol, action="rejected_by_risk",
                                          reason=f"{e.rule}: {e.detail}"))
                continue

            try:
                result = self._alpaca.place_order_with_stop_loss(order)
            except AlpacaClientError as e:
                decisions.append(Decision(symbol=symbol, action="api_error", reason=str(e)))
                continue

            self._journal.append(TradeRecord(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                side="buy",
                qty=sig.qty,
                price=sig.entry_price,
                asset_class=asset_class.value,
                strategy="momentum",
                regime=self._regime,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
                notes=sig.reason,
            ))
            decisions.append(Decision(
                symbol=symbol, action="placed_order", reason=sig.reason,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
            ))

        return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))
```

- [ ] **Step 4: Run to confirm pass**

```bash
.venv/bin/pytest tests/test_orchestrator.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): scan watchlist, generate signals, gate through risk, place orders"
```

---

## Task 8: Daily Report Generator

**Files:**
- Create: `src/trading_bot/reports.py`
- Create: `tests/test_reports.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reports.py
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import Decision, ScanResult
from trading_bot.reports import build_daily_report_html


def test_daily_report_contains_account_and_decisions():
    account = AccountSnapshot(
        equity=Decimal("15123.45"),
        cash=Decimal("12000"),
        buying_power=Decimal("24000"),
        portfolio_value=Decimal("15123.45"),
    )
    positions = [
        Position(
            symbol="AAPL",
            qty=Decimal("3"),
            market_value=Decimal("585"),
            avg_entry_price=Decimal("195"),
            unrealized_pl=Decimal("12.50"),
            asset_class="us_equity",
        )
    ]
    scan = ScanResult(
        decisions=[
            Decision(symbol="MSFT", action="placed_order",
                     reason="rsi=58.0 macd>0.020 close>EMA20",
                     entry_order_id="e-1", stop_loss_order_id="s-1"),
            Decision(symbol="QQQ", action="hold", reason="rsi 45.2 outside [55, 70]"),
            Decision(symbol="SPY", action="skipped_existing_position"),
        ],
        timestamp=datetime(2026, 4, 25, 20, 30, tzinfo=timezone.utc),
    )

    html = build_daily_report_html(
        account=account, positions=positions, scan=scan,
        spy_daily_change_pct=Decimal("1.20"),
        regime="trending_up",
    )
    assert "15123.45" in html
    assert "AAPL" in html
    assert "MSFT" in html
    assert "placed_order" in html
    assert "trending_up" in html
    assert "1.20" in html  # SPY change
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_reports.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/reports.py
from datetime import datetime
from decimal import Decimal

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import ScanResult


def build_daily_report_html(
    *,
    account: AccountSnapshot,
    positions: list[Position],
    scan: ScanResult,
    spy_daily_change_pct: Decimal,
    regime: str,
) -> str:
    pos_rows = "".join(
        f"<tr><td>{p.symbol}</td><td>{p.qty}</td>"
        f"<td>${p.avg_entry_price}</td>"
        f"<td>${p.market_value}</td>"
        f"<td style='color:{'green' if p.unrealized_pl >= 0 else 'red'}'>${p.unrealized_pl}</td></tr>"
        for p in positions
    ) or "<tr><td colspan='5'><i>No open positions</i></td></tr>"

    dec_rows = "".join(
        f"<tr><td>{d.symbol}</td><td>{d.action}</td><td>{d.reason}</td></tr>"
        for d in scan.decisions
    ) or "<tr><td colspan='3'><i>No decisions this run</i></td></tr>"

    return f"""
<h2>Trading Bot — Daily Report</h2>
<p><b>Generated:</b> {scan.timestamp.isoformat(timespec='seconds')}<br>
<b>Regime:</b> {regime}<br>
<b>SPY daily move:</b> {spy_daily_change_pct}%</p>

<h3>Account</h3>
<table border='1' cellpadding='6'>
  <tr><th>Equity</th><td>${account.equity}</td></tr>
  <tr><th>Cash</th><td>${account.cash}</td></tr>
  <tr><th>Buying Power</th><td>${account.buying_power}</td></tr>
  <tr><th>Portfolio Value</th><td>${account.portfolio_value}</td></tr>
</table>

<h3>Open Positions</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Qty</th><th>Avg Entry</th><th>Market Value</th><th>Unrealized P&amp;L</th></tr>
  {pos_rows}
</table>

<h3>Decisions This Run</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Action</th><th>Reason</th></tr>
  {dec_rows}
</table>
"""
```

- [ ] **Step 4: Run to confirm pass**

```bash
.venv/bin/pytest tests/test_reports.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/reports.py tests/test_reports.py
git commit -m "feat(reports): daily HTML report builder with positions, decisions, vs-SPY"
```

---

## Task 9: CLI — `bot scan` and `bot daily-report`

**Files:**
- Modify: `src/trading_bot/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_cli.py`:

```python
def test_bot_scan_runs_orchestrator():
    from trading_bot.cli import main

    with patch("trading_bot.cli.AlpacaClient") as MockAlpaca, patch(
        "trading_bot.cli.MarketDataClient"
    ) as MockMarket, patch(
        "trading_bot.cli.Settings"
    ) as MockSettings, patch(
        "trading_bot.cli.load_config"
    ) as MockCfg, patch(
        "trading_bot.cli.load_watchlist"
    ) as MockWL, patch(
        "trading_bot.cli.TradeOrchestrator"
    ) as MockOrch, patch(
        "trading_bot.cli.TradeJournal"
    ):
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k", alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u", gmail_app_password="p", bot_mode="paper",
        )
        MockCfg.return_value = _real_config_for_test()
        MockWL.return_value = []
        from trading_bot.orchestrator import ScanResult
        from datetime import datetime, timezone
        scan = ScanResult(decisions=[], timestamp=datetime.now(timezone.utc))
        MockOrch.return_value.scan.return_value = scan

        runner = CliRunner()
        result = runner.invoke(main, ["scan", "--regime", "trending_up"])
        assert result.exit_code == 0, result.output
        assert "Scan complete" in result.output
        MockOrch.return_value.scan.assert_called_once()


def test_bot_daily_report_emails():
    from trading_bot.cli import main

    fake_account = MagicMock(
        equity=Decimal("15000"), cash=Decimal("15000"),
        buying_power=Decimal("30000"), portfolio_value=Decimal("15000"),
    )

    with patch("trading_bot.cli.AlpacaClient") as MockAlpaca, patch(
        "trading_bot.cli.MarketDataClient"
    ) as MockMarket, patch(
        "trading_bot.cli.Settings"
    ) as MockSettings, patch(
        "trading_bot.cli.load_config"
    ) as MockCfg, patch(
        "trading_bot.cli.EmailSender"
    ) as MockEmail:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k", alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u", gmail_app_password="p", bot_mode="paper",
        )
        MockCfg.return_value = _real_config_for_test()
        MockAlpaca.return_value.get_account.return_value = fake_account
        MockAlpaca.return_value.get_positions.return_value = []

        # SPY bars for daily change calc
        import pandas as pd
        from datetime import datetime, timezone
        bars = pd.DataFrame(
            {"close": [700.0, 707.0]},
            index=pd.date_range("2026-04-24", periods=2, freq="D", tz="UTC"),
        )
        MockMarket.return_value.get_daily_bars.return_value = bars

        sender = MockEmail.return_value
        runner = CliRunner()
        result = runner.invoke(main, ["daily-report"])
        assert result.exit_code == 0, result.output
        sender.send.assert_called_once()
        kwargs = sender.send.call_args.kwargs
        assert "Daily Report" in kwargs["subject"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: 2 new failures.

- [ ] **Step 3: Implement — extend `src/trading_bot/cli.py`**

ADD these imports near the top alongside existing imports:

```python
from trading_bot.market_data import MarketDataClient
from trading_bot.orchestrator import TradeOrchestrator
from trading_bot.reports import build_daily_report_html
from trading_bot.state import load_watchlist
from trading_bot.trade_journal import TradeJournal
```

ADD this constant after `CONFIG_PATH`:

```python
WATCHLIST_PATH = Path("strategy/watchlist.yaml")
```

ADD these commands after the existing `dry-run` command:

```python
@main.command()
@click.option(
    "--regime",
    type=click.Choice(["trending_up", "trending_down", "sideways", "risk_off"]),
    default="trending_up",
)
def scan(regime: str) -> None:
    """Scan watchlist and place trades on signals (real paper orders)."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    watchlist = load_watchlist(WATCHLIST_PATH)

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
    )
    result = orch.scan(watchlist=watchlist)
    click.echo(f"Scan complete — {len(result.decisions)} decisions:")
    for d in result.decisions:
        click.echo(f"  {d.symbol}: {d.action} ({d.reason})")


@main.command("daily-report")
def daily_report() -> None:
    """Email the daily P&L summary."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    account = alpaca.get_account()
    positions = alpaca.get_positions()

    # SPY daily move
    try:
        bars = market.get_daily_bars("SPY", lookback_days=2)
        if len(bars) >= 2:
            yesterday, today = bars["close"].iloc[-2], bars["close"].iloc[-1]
            spy_change = Decimal(str((today / yesterday - 1.0) * 100)).quantize(Decimal("0.01"))
        else:
            spy_change = Decimal("0.00")
    except Exception:
        spy_change = Decimal("0.00")

    from datetime import datetime, timezone

    from trading_bot.orchestrator import ScanResult

    empty_scan = ScanResult(decisions=[], timestamp=datetime.now(timezone.utc))
    html = build_daily_report_html(
        account=account, positions=positions, scan=empty_scan,
        spy_daily_change_pct=spy_change, regime="trending_up",
    )

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject="Trading Bot — Daily Report", html_body=html)
    click.echo(f"Sent daily report to {cfg.email.to}")
```

- [ ] **Step 4: Run to confirm pass**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: 5 passed (3 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/cli.py tests/test_cli.py
git commit -m "feat(cli): bot scan + bot daily-report commands"
```

---

## Task 10: Manual End-to-End — First Paper Trade

- [ ] **Step 1: Full suite green**

```bash
.venv/bin/pytest -v
```
Expected: all unit tests pass. Integration tests skipped unless `RUN_INTEGRATION=1`.

- [ ] **Step 2: Dry-run smoke against live API**

```bash
.venv/bin/bot dry-run --symbol AAPL --side buy --qty 1 --price 195.00 --stop 192.00 --regime trending_up
```
Expected: PASS (1 share is well within all caps for the $15k account).

- [ ] **Step 3: Run a real scan**

```bash
.venv/bin/bot scan --regime trending_up
```
Expected output (varies):
- Either `placed_order` for at least one symbol if signals trigger, OR
- `hold` for all if no symbol meets the momentum criteria today

If `placed_order`: check the Alpaca paper dashboard at https://paper-api.alpaca.markets to confirm the order is visible.

If `hold` for all: that's correct behavior — the strategy is conservative. Try `--regime sideways` or wait for a day with stronger momentum signals.

- [ ] **Step 4: Daily report email**

```bash
.venv/bin/bot daily-report
```
Expected: console says "Sent daily report to bharath8887@gmail.com" and the email arrives in your inbox with positions + decisions + SPY change.

- [ ] **Step 5: Verify trade journal**

```bash
.venv/bin/python -c "
from pathlib import Path
from trading_bot.trade_journal import TradeJournal
j = TradeJournal(Path('data/trade_journal.db'))
for t in j.all():
    print(f'{t.timestamp} {t.symbol} {t.side} qty={t.qty} @ \${t.price} stop={t.stop_loss_order_id}')
"
```
Expected: any trades placed in Step 3 are listed.

- [ ] **Step 6: Tag completion**

```bash
git tag plan-2-strategy-engine-complete
git log --oneline -10
```

---

## Plan 2 Acceptance Criteria

- [x] All unit tests pass
- [x] `bot scan` runs without errors and produces a clear decision per watchlist symbol
- [x] If signals trigger, at least one paper trade is placed and visible in the Alpaca dashboard
- [x] Each placed trade has both an entry order and a stop-loss order (verifiable in Alpaca)
- [x] `bot daily-report` delivers a complete HTML email
- [x] Trade journal records every placed order

---

## What Plan 3 Will Build (next)

- `trading-intelligence-mcp` — MCP server exposing all data feeds (SEC EDGAR, GDELT, FRED, Alpaca news, Finviz) to Claude
- Scheduled routines: `morning_brief`, `intraday_scan`, `eod_review`
- Live regime detection (using FRED VIX + SPY trend)
- Risk state computation (live daily/weekly P&L from journal + account history)
- Mean reversion strategy module
- Sentiment overlay (boost/veto entries based on news mood)

Plan 2's `TradeOrchestrator` becomes the engine called by Plan 3's scheduled routines — no rewrite needed.
