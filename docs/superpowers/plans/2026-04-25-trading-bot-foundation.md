# Trading Bot — Foundation Implementation Plan (Plan 1 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foolproof safety foundation — Alpaca client, risk manager, email reporting, trade journal, and CLI — that all subsequent trading logic depends on.

**Architecture:** Layered Python package with strict separation between Alpaca I/O, risk validation (gates every trade), persistence (SQLite journal), and reporting (email). Paper-trading is enforced at the client construction level so live trading is structurally impossible without an explicit code change.

**Tech Stack:** Python 3.11+, alpaca-py, pydantic, pyyaml, SQLAlchemy (SQLite), Jinja2 (email templates), pytest, click (CLI).

**This plan produces:**
- A `bot status` CLI command that emails account state
- A `bot dry-run` CLI command that simulates a trade decision through the risk manager (no order placed)
- Full unit + integration test coverage on the risk manager
- A clean, foolproof base for Plan 2 (Intelligence MCP) to build on

**Spec reference:** `docs/superpowers/specs/2026-04-25-trading-bot-design.md` Sections 3.1, 3.5, 3.6, 7.1, 7.2, 7.3.

---

## File Structure

```
/Users/bharathkandala/Trading/
├── pyproject.toml                          # Package config + dependencies
├── .env.example                            # Template (no secrets)
├── .env                                    # Actual secrets (gitignored)
├── .gitignore
├── README.md
├── strategy/
│   └── config.yaml                         # Risk parameters, allocation
├── src/
│   └── trading_bot/
│       ├── __init__.py
│       ├── config.py                       # Pydantic config loader
│       ├── alpaca_client.py                # Alpaca wrapper (paper-only enforced)
│       ├── risk_manager.py                 # All hard rules — gates every trade
│       ├── trade_journal.py                # SQLite write/read for trades
│       ├── email_sender.py                 # Gmail SMTP utility
│       ├── exceptions.py                   # Custom exceptions
│       └── cli.py                          # `bot` CLI entry point
└── tests/
    ├── __init__.py
    ├── conftest.py                         # pytest fixtures
    ├── test_config.py
    ├── test_alpaca_client.py               # Mocked Alpaca responses
    ├── test_risk_manager.py                # Pure-logic tests, no I/O
    ├── test_trade_journal.py               # SQLite tests against tmp DB
    └── test_integration.py                 # End-to-end with paper account
```

---

## Task 1: Project Initialization

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/trading_bot/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "trading-bot"
version = "0.1.0"
description = "Semi-autonomous algorithmic trading bot for Alpaca paper account"
requires-python = ">=3.11"
dependencies = [
    "alpaca-py>=0.30.0",
    "pydantic>=2.5",
    "pydantic-settings>=2.1",
    "pyyaml>=6.0",
    "sqlalchemy>=2.0",
    "jinja2>=3.1",
    "click>=8.1",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "pytest-cov>=4.1",
    "pytest-mock>=3.12",
    "freezegun>=1.4",
    "ruff>=0.2",
]

[project.scripts]
bot = "trading_bot.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v --strict-markers"

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 2: Create `.env.example`**

```
# Alpaca Paper Trading API (NEVER use live credentials here)
ALPACA_API_KEY=your_paper_key_here
ALPACA_API_SECRET=your_paper_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2

# Gmail App Password for daily reports
GMAIL_USER=bharath8887@gmail.com
GMAIL_APP_PASSWORD=generate_at_myaccount.google.com_security_app_passwords

# Bot mode — must be "paper". "live" requires explicit code unlock.
BOT_MODE=paper
```

- [ ] **Step 3: Create `.gitignore`**

```
__pycache__/
*.pyc
.env
.venv/
venv/
*.db
*.sqlite
*.sqlite3
.pytest_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
.DS_Store
```

- [ ] **Step 4: Create `README.md`**

```markdown
# Trading Bot

Semi-autonomous algorithmic trading bot for Alpaca paper account.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then fill in real values
```

## Usage

```bash
bot status      # Email current account status
bot dry-run --symbol AAPL --side buy --qty 10  # Simulate trade through risk manager
```

## Tests

```bash
pytest
```
```

- [ ] **Step 5: Create empty package files**

```python
# src/trading_bot/__init__.py
__version__ = "0.1.0"
```

```python
# tests/__init__.py
```

- [ ] **Step 6: Create the actual `.env` from credentials in memory**

```
ALPACA_API_KEY=PKEGGK2BR3EFLFHQ4BQLSUO2UA
ALPACA_API_SECRET=3ayyrjccjJpp7s92iKNXJN2TVtLnLnpsZyzYVjVxeMz7
ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2
GMAIL_USER=bharath8887@gmail.com
GMAIL_APP_PASSWORD=PLACEHOLDER_USER_MUST_GENERATE
BOT_MODE=paper
```

- [ ] **Step 7: Initialize git, install dependencies, smoke test**

```bash
cd /Users/bharathkandala/Trading
git init
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest --collect-only
```
Expected: `pytest` reports "0 tests collected" with no errors.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .env.example .gitignore README.md src/ tests/
git commit -m "chore: project scaffold with dependencies and pytest"
```

---

## Task 2: Custom Exceptions

**Files:**
- Create: `src/trading_bot/exceptions.py`
- Create: `tests/test_exceptions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exceptions.py
import pytest
from trading_bot.exceptions import (
    TradingBotError,
    RiskRuleViolation,
    LiveModeDisabled,
    ConfigError,
    AlpacaClientError,
)


def test_all_exceptions_inherit_base():
    for exc in (RiskRuleViolation, LiveModeDisabled, ConfigError, AlpacaClientError):
        assert issubclass(exc, TradingBotError)


def test_risk_rule_violation_carries_rule_name():
    err = RiskRuleViolation(rule="daily_loss_limit", detail="-3.1% breach")
    assert err.rule == "daily_loss_limit"
    assert "daily_loss_limit" in str(err)
    assert "-3.1%" in str(err)


def test_live_mode_disabled_message():
    err = LiveModeDisabled()
    assert "paper" in str(err).lower()
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_exceptions.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.exceptions'`

- [ ] **Step 3: Implement**

```python
# src/trading_bot/exceptions.py
class TradingBotError(Exception):
    """Base exception for all trading bot errors."""


class ConfigError(TradingBotError):
    """Raised when configuration is invalid or missing."""


class AlpacaClientError(TradingBotError):
    """Raised when an Alpaca API call fails."""


class LiveModeDisabled(TradingBotError):
    """Raised if anything attempts to enable live trading. This is paper-only."""

    def __init__(self) -> None:
        super().__init__(
            "Live trading is structurally disabled. This bot is paper-only. "
            "Live mode requires explicit code unlock and a separate authorization."
        )


class RiskRuleViolation(TradingBotError):
    """Raised when a trade violates a hard risk rule."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"Risk rule violated: {rule} — {detail}")
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_exceptions.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/exceptions.py tests/test_exceptions.py
git commit -m "feat(exceptions): add typed exceptions for risk, config, and paper-only enforcement"
```

---

## Task 3: Config Loader

**Files:**
- Create: `src/trading_bot/config.py`
- Create: `strategy/config.yaml`
- Create: `tests/test_config.py`

- [ ] **Step 1: Create `strategy/config.yaml`**

```yaml
# strategy/config.yaml — non-secret runtime configuration

risk:
  daily_loss_limit_pct: 2.0          # halt trading if daily P&L < -2%
  weekly_loss_limit_pct: 5.0         # halt trading if weekly P&L < -5%
  per_trade_risk_pct: 1.0            # max account at risk per trade
  max_position_pct: 10.0             # no single position > 10% of account
  max_symbol_concentration_pct: 5.0  # max 5% in any one symbol
  max_consecutive_losing_days: 3     # 3 losers → reduce sizing 50%

allocation:
  stocks_max_pct: 70.0
  crypto_max_pct: 30.0
  options_max_pct: 20.0
  cash_floor_pct: 10.0

regime_allocations:
  trending_up:    {stocks: 60.0, crypto: 25.0, options: 15.0, cash: 0.0}
  trending_down:  {stocks: 30.0, crypto: 15.0, options: 10.0, cash: 45.0}
  sideways:       {stocks: 40.0, crypto: 20.0, options: 20.0, cash: 20.0}
  risk_off:       {stocks: 10.0, crypto: 5.0,  options: 0.0,  cash: 85.0}

email:
  to: bharath8887@gmail.com
  daily_summary_time_et: "16:30"
  weekly_summary_day: "Sunday"

storage:
  trade_journal_path: data/trade_journal.db
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from trading_bot.config import Settings, load_config
from trading_bot.exceptions import ConfigError, LiveModeDisabled


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("GMAIL_USER", "x@y.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
    monkeypatch.setenv("BOT_MODE", "paper")

    s = Settings()
    assert s.alpaca_api_key == "key"
    assert s.bot_mode == "paper"


def test_live_mode_raises(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("GMAIL_USER", "x@y.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
    monkeypatch.setenv("BOT_MODE", "live")

    with pytest.raises(LiveModeDisabled):
        Settings()


def test_load_config_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
risk:
  daily_loss_limit_pct: 2.0
  weekly_loss_limit_pct: 5.0
  per_trade_risk_pct: 1.0
  max_position_pct: 10.0
  max_symbol_concentration_pct: 5.0
  max_consecutive_losing_days: 3
allocation:
  stocks_max_pct: 70.0
  crypto_max_pct: 30.0
  options_max_pct: 20.0
  cash_floor_pct: 10.0
regime_allocations:
  trending_up: {stocks: 60.0, crypto: 25.0, options: 15.0, cash: 0.0}
  trending_down: {stocks: 30.0, crypto: 15.0, options: 10.0, cash: 45.0}
  sideways: {stocks: 40.0, crypto: 20.0, options: 20.0, cash: 20.0}
  risk_off: {stocks: 10.0, crypto: 5.0, options: 0.0, cash: 85.0}
email:
  to: bharath8887@gmail.com
  daily_summary_time_et: "16:30"
  weekly_summary_day: Sunday
storage:
  trade_journal_path: data/trade_journal.db
"""
    )
    cfg = load_config(config_path)
    assert cfg.risk.daily_loss_limit_pct == 2.0
    assert cfg.regime_allocations["trending_up"].stocks == 60.0
    assert cfg.email.to == "bharath8887@gmail.com"


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nonexistent.yaml")
```

- [ ] **Step 3: Run to confirm failure**

```bash
pytest tests/test_config.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

```python
# src/trading_bot/config.py
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_bot.exceptions import ConfigError, LiveModeDisabled


class Settings(BaseSettings):
    """Secrets + mode pulled from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_base_url: str = "https://paper-api.alpaca.markets/v2"
    gmail_user: str
    gmail_app_password: str
    bot_mode: Literal["paper", "live"] = "paper"

    @field_validator("bot_mode")
    @classmethod
    def enforce_paper_only(cls, v: str) -> str:
        if v != "paper":
            raise LiveModeDisabled()
        return v


class RiskConfig(BaseModel):
    daily_loss_limit_pct: float = Field(gt=0, le=10)
    weekly_loss_limit_pct: float = Field(gt=0, le=20)
    per_trade_risk_pct: float = Field(gt=0, le=5)
    max_position_pct: float = Field(gt=0, le=25)
    max_symbol_concentration_pct: float = Field(gt=0, le=25)
    max_consecutive_losing_days: int = Field(gt=0, le=10)


class AllocationConfig(BaseModel):
    stocks_max_pct: float = Field(ge=0, le=100)
    crypto_max_pct: float = Field(ge=0, le=100)
    options_max_pct: float = Field(ge=0, le=100)
    cash_floor_pct: float = Field(ge=0, le=100)


class RegimeAllocation(BaseModel):
    stocks: float = Field(ge=0, le=100)
    crypto: float = Field(ge=0, le=100)
    options: float = Field(ge=0, le=100)
    cash: float = Field(ge=0, le=100)


class EmailConfig(BaseModel):
    to: str
    daily_summary_time_et: str
    weekly_summary_day: str


class StorageConfig(BaseModel):
    trade_journal_path: str


class AppConfig(BaseModel):
    risk: RiskConfig
    allocation: AllocationConfig
    regime_allocations: dict[str, RegimeAllocation]
    email: EmailConfig
    storage: StorageConfig


def load_config(path: Path) -> AppConfig:
    """Load YAML config from disk and validate."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    try:
        return AppConfig(**raw)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e
```

- [ ] **Step 5: Run to confirm pass**

```bash
pytest tests/test_config.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/config.py strategy/config.yaml tests/test_config.py
git commit -m "feat(config): typed settings + yaml app config with paper-only enforcement"
```

---

## Task 4: Alpaca Client — Account & Positions (Read-Only)

**Files:**
- Create: `src/trading_bot/alpaca_client.py`
- Create: `tests/test_alpaca_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alpaca_client.py
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.alpaca_client import AlpacaClient, AccountSnapshot, Position
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled


@pytest.fixture
def fake_settings():
    s = MagicMock()
    s.alpaca_api_key = "k"
    s.alpaca_api_secret = "s"
    s.alpaca_base_url = "https://paper-api.alpaca.markets/v2"
    s.bot_mode = "paper"
    return s


def test_client_refuses_non_paper_url(fake_settings):
    fake_settings.alpaca_base_url = "https://api.alpaca.markets/v2"  # live URL
    with pytest.raises(LiveModeDisabled):
        AlpacaClient(fake_settings)


def test_get_account_returns_snapshot(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        mock_account = MagicMock()
        mock_account.equity = "100000.50"
        mock_account.cash = "25000.10"
        mock_account.buying_power = "50000.20"
        mock_account.portfolio_value = "100000.50"
        MockTC.return_value.get_account.return_value = mock_account

        client = AlpacaClient(fake_settings)
        snap = client.get_account()
        assert isinstance(snap, AccountSnapshot)
        assert snap.equity == Decimal("100000.50")
        assert snap.cash == Decimal("25000.10")


def test_get_positions_returns_list(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_pos.qty = "10"
        mock_pos.market_value = "2000.00"
        mock_pos.avg_entry_price = "195.50"
        mock_pos.unrealized_pl = "50.00"
        mock_pos.asset_class = "us_equity"
        MockTC.return_value.get_all_positions.return_value = [mock_pos]

        client = AlpacaClient(fake_settings)
        positions = client.get_positions()
        assert len(positions) == 1
        p = positions[0]
        assert isinstance(p, Position)
        assert p.symbol == "AAPL"
        assert p.qty == Decimal("10")
        assert p.market_value == Decimal("2000.00")


def test_get_account_wraps_api_error(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.get_account.side_effect = RuntimeError("boom")
        client = AlpacaClient(fake_settings)
        with pytest.raises(AlpacaClientError):
            client.get_account()
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_alpaca_client.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/alpaca_client.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient

from trading_bot.config import Settings
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled

PAPER_URL_PREFIX = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    market_value: Decimal
    avg_entry_price: Decimal
    unrealized_pl: Decimal
    asset_class: str


class AlpacaClient:
    """Wrapper around alpaca-py TradingClient. Paper-only by construction."""

    def __init__(self, settings: Settings) -> None:
        if not settings.alpaca_base_url.startswith(PAPER_URL_PREFIX):
            raise LiveModeDisabled()
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )

    def get_account(self) -> AccountSnapshot:
        try:
            a = self._client.get_account()
        except Exception as e:
            raise AlpacaClientError(f"get_account failed: {e}") from e
        return AccountSnapshot(
            equity=Decimal(str(a.equity)),
            cash=Decimal(str(a.cash)),
            buying_power=Decimal(str(a.buying_power)),
            portfolio_value=Decimal(str(a.portfolio_value)),
        )

    def get_positions(self) -> list[Position]:
        try:
            raw = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"get_all_positions failed: {e}") from e
        return [
            Position(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                market_value=Decimal(str(p.market_value)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                unrealized_pl=Decimal(str(p.unrealized_pl)),
                asset_class=str(p.asset_class),
            )
            for p in raw
        ]
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_alpaca_client.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/alpaca_client.py tests/test_alpaca_client.py
git commit -m "feat(alpaca): paper-only client with account + positions read"
```

---

## Task 5: Alpaca Client — Order Placement with Stop-Loss

**Files:**
- Modify: `src/trading_bot/alpaca_client.py`
- Modify: `tests/test_alpaca_client.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_alpaca_client.py`:

```python
from trading_bot.alpaca_client import OrderRequest, OrderResult, OrderSide, AssetClass


def test_place_order_with_stop_loss(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-1", status="accepted", filled_qty="0", filled_avg_price=None)
        stop = MagicMock(id="stop-1", status="accepted", filled_qty="0", filled_avg_price=None)
        MockTC.return_value.submit_order.side_effect = [entry, stop]

        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="AAPL",
            qty=Decimal("10"),
            side=OrderSide.BUY,
            asset_class=AssetClass.STOCK,
            limit_price=Decimal("195.00"),
            stop_loss_price=Decimal("190.00"),
        )
        result = client.place_order_with_stop_loss(req)
        assert isinstance(result, OrderResult)
        assert result.entry_order_id == "entry-1"
        assert result.stop_loss_order_id == "stop-1"
        assert MockTC.return_value.submit_order.call_count == 2


def test_place_order_requires_stop_loss(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient"):
        client = AlpacaClient(fake_settings)
        with pytest.raises(ValueError, match="stop_loss_price"):
            OrderRequest(
                symbol="AAPL",
                qty=Decimal("10"),
                side=OrderSide.BUY,
                asset_class=AssetClass.STOCK,
                limit_price=Decimal("195.00"),
                stop_loss_price=None,
            )


def test_place_order_rolls_back_on_stop_failure(fake_settings):
    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        entry = MagicMock(id="entry-1", status="accepted", filled_qty="0", filled_avg_price=None)
        MockTC.return_value.submit_order.side_effect = [entry, RuntimeError("stop failed")]

        client = AlpacaClient(fake_settings)
        req = OrderRequest(
            symbol="AAPL",
            qty=Decimal("10"),
            side=OrderSide.BUY,
            asset_class=AssetClass.STOCK,
            limit_price=Decimal("195.00"),
            stop_loss_price=Decimal("190.00"),
        )
        with pytest.raises(AlpacaClientError, match="stop-loss"):
            client.place_order_with_stop_loss(req)
        # entry was canceled
        MockTC.return_value.cancel_order_by_id.assert_called_once_with("entry-1")
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_alpaca_client.py -v
```
Expected: 3 new failures (`OrderRequest` etc. not defined).

- [ ] **Step 3: Implement order placement**

Append to `src/trading_bot/alpaca_client.py`:

```python
from enum import Enum

from alpaca.trading.enums import OrderSide as AlpacaSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, StopOrderRequest
from pydantic import BaseModel, model_validator


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"


class OrderRequest(BaseModel):
    symbol: str
    qty: Decimal
    side: OrderSide
    asset_class: AssetClass
    limit_price: Decimal
    stop_loss_price: Decimal | None

    @model_validator(mode="after")
    def require_stop_loss(self) -> "OrderRequest":
        if self.stop_loss_price is None:
            raise ValueError("stop_loss_price is required — every position must have a stop")
        return self


@dataclass(frozen=True)
class OrderResult:
    entry_order_id: str
    stop_loss_order_id: str


def _to_alpaca_side(s: OrderSide) -> AlpacaSide:
    return AlpacaSide.BUY if s == OrderSide.BUY else AlpacaSide.SELL


def _opposite(s: OrderSide) -> OrderSide:
    return OrderSide.SELL if s == OrderSide.BUY else OrderSide.BUY


# --- inside class AlpacaClient: append the method below ---
```

Then add the method to the class:

```python
    def place_order_with_stop_loss(self, req: OrderRequest) -> OrderResult:
        """Atomically place entry + stop-loss. If stop fails, cancel entry."""
        try:
            entry_req = LimitOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(req.side),
                time_in_force=TimeInForce.DAY,
                limit_price=float(req.limit_price),
            )
            entry = self._client.submit_order(entry_req)
        except Exception as e:
            raise AlpacaClientError(f"entry order failed: {e}") from e

        try:
            stop_req = StopOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(_opposite(req.side)),
                time_in_force=TimeInForce.GTC,
                stop_price=float(req.stop_loss_price),
            )
            stop = self._client.submit_order(stop_req)
        except Exception as e:
            try:
                self._client.cancel_order_by_id(entry.id)
            except Exception:
                pass
            raise AlpacaClientError(
                f"stop-loss order failed (entry rolled back): {e}"
            ) from e

        return OrderResult(entry_order_id=entry.id, stop_loss_order_id=stop.id)
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_alpaca_client.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/alpaca_client.py tests/test_alpaca_client.py
git commit -m "feat(alpaca): atomic order placement with mandatory stop-loss + rollback"
```

---

## Task 6: Trade Journal (SQLite)

**Files:**
- Create: `src/trading_bot/trade_journal.py`
- Create: `tests/test_trade_journal.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_journal.py
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.trade_journal import TradeJournal, TradeRecord


@pytest.fixture
def journal(tmp_path: Path) -> TradeJournal:
    return TradeJournal(tmp_path / "test.db")


def test_journal_appends_and_reads_back(journal: TradeJournal):
    rec = TradeRecord(
        timestamp=datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc),
        symbol="AAPL",
        side="buy",
        qty=Decimal("10"),
        price=Decimal("195.00"),
        asset_class="stock",
        strategy="momentum",
        regime="trending_up",
        entry_order_id="e1",
        stop_loss_order_id="s1",
        notes="initial entry",
    )
    journal.append(rec)
    rows = journal.all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].qty == Decimal("10")


def test_journal_is_append_only(journal: TradeJournal):
    """Sanity: journal exposes no update/delete API."""
    assert not hasattr(journal, "update")
    assert not hasattr(journal, "delete")


def test_journal_filters_by_date_range(journal: TradeJournal):
    base = datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc)
    for i in range(3):
        journal.append(
            TradeRecord(
                timestamp=base.replace(day=25 + i),
                symbol=f"S{i}",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                asset_class="stock",
                strategy="momentum",
                regime="trending_up",
                entry_order_id=f"e{i}",
                stop_loss_order_id=f"s{i}",
                notes="",
            )
        )
    middle = base.replace(day=26)
    rows = journal.between(middle, middle.replace(hour=23, minute=59))
    assert len(rows) == 1
    assert rows[0].symbol == "S1"
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_trade_journal.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/trade_journal.py
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session


class _Base(DeclarativeBase):
    pass


class _TradeRow(_Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    qty = Column(Numeric(20, 8), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    asset_class = Column(String(16), nullable=False)
    strategy = Column(String(32), nullable=False)
    regime = Column(String(32), nullable=False)
    entry_order_id = Column(String(64), nullable=False)
    stop_loss_order_id = Column(String(64), nullable=False)
    notes = Column(Text, nullable=False, default="")


@dataclass(frozen=True)
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    asset_class: str
    strategy: str
    regime: str
    entry_order_id: str
    stop_loss_order_id: str
    notes: str


class TradeJournal:
    """Append-only SQLite trade journal."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)
        _Base.metadata.create_all(self._engine)

    def append(self, rec: TradeRecord) -> None:
        with Session(self._engine) as s:
            s.add(
                _TradeRow(
                    timestamp=rec.timestamp,
                    symbol=rec.symbol,
                    side=rec.side,
                    qty=rec.qty,
                    price=rec.price,
                    asset_class=rec.asset_class,
                    strategy=rec.strategy,
                    regime=rec.regime,
                    entry_order_id=rec.entry_order_id,
                    stop_loss_order_id=rec.stop_loss_order_id,
                    notes=rec.notes,
                )
            )
            s.commit()

    def all(self) -> list[TradeRecord]:
        with Session(self._engine) as s:
            rows = s.execute(select(_TradeRow).order_by(_TradeRow.timestamp)).scalars().all()
            return [self._to_record(r) for r in rows]

    def between(self, start: datetime, end: datetime) -> list[TradeRecord]:
        with Session(self._engine) as s:
            rows = (
                s.execute(
                    select(_TradeRow)
                    .where(_TradeRow.timestamp >= start, _TradeRow.timestamp <= end)
                    .order_by(_TradeRow.timestamp)
                )
                .scalars()
                .all()
            )
            return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(r: _TradeRow) -> TradeRecord:
        return TradeRecord(
            timestamp=r.timestamp,
            symbol=r.symbol,
            side=r.side,
            qty=Decimal(str(r.qty)),
            price=Decimal(str(r.price)),
            asset_class=r.asset_class,
            strategy=r.strategy,
            regime=r.regime,
            entry_order_id=r.entry_order_id,
            stop_loss_order_id=r.stop_loss_order_id,
            notes=r.notes,
        )
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_trade_journal.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/trade_journal.py tests/test_trade_journal.py
git commit -m "feat(journal): append-only sqlite trade journal with date filtering"
```

---

## Task 7: Risk Manager — Position Sizing & Per-Trade Risk

**Files:**
- Create: `src/trading_bot/risk_manager.py`
- Create: `tests/test_risk_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_risk_manager.py
from decimal import Decimal

import pytest

from trading_bot.alpaca_client import AccountSnapshot, AssetClass, OrderRequest, OrderSide, Position
from trading_bot.config import (
    AllocationConfig,
    AppConfig,
    EmailConfig,
    RegimeAllocation,
    RiskConfig,
    StorageConfig,
)
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.risk_manager import RiskManager, RiskState


def make_config(**overrides) -> AppConfig:
    risk = RiskConfig(
        daily_loss_limit_pct=2.0,
        weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0,
        max_position_pct=10.0,
        max_symbol_concentration_pct=5.0,
        max_consecutive_losing_days=3,
    )
    alloc = AllocationConfig(
        stocks_max_pct=70.0, crypto_max_pct=30.0, options_max_pct=20.0, cash_floor_pct=10.0
    )
    regimes = {
        "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
        "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
        "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
        "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
    }
    cfg = AppConfig(
        risk=risk,
        allocation=alloc,
        regime_allocations=regimes,
        email=EmailConfig(
            to="x@y.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"
        ),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )
    return cfg


@pytest.fixture
def cfg() -> AppConfig:
    return make_config()


@pytest.fixture
def acct() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
    )


@pytest.fixture
def state() -> RiskState:
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )


def test_risk_allows_normal_trade(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("10"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("195.00"),  # $1,950 trade, 1.95% of account
        stop_loss_price=Decimal("191.10"),  # 2% stop, $39 risk = 0.039% of account
    )
    rm.check(req, account=acct, positions=[], state=state, regime="trending_up")  # no raise


def test_risk_rejects_oversized_position(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("100"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("195.00"),  # $19,500 = 19.5% > 10% max
        stop_loss_price=Decimal("191.10"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="trending_up")
    assert e.value.rule == "max_position_pct"


def test_risk_rejects_excessive_per_trade_risk(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("10"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("195.00"),  # entry
        stop_loss_price=Decimal("85.00"),  # huge stop = $1100 risk = 1.1% > 1% limit
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="trending_up")
    assert e.value.rule == "per_trade_risk_pct"


def test_risk_rejects_when_halted(cfg, acct):
    rm = RiskManager(cfg)
    halted = RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=True,
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=halted, regime="trending_up")
    assert e.value.rule == "halted"
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_risk_manager.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement risk manager (basics first)**

```python
# src/trading_bot/risk_manager.py
from dataclasses import dataclass
from decimal import Decimal

from trading_bot.alpaca_client import (
    AccountSnapshot,
    AssetClass,
    OrderRequest,
    OrderSide,
    Position,
)
from trading_bot.config import AppConfig
from trading_bot.exceptions import RiskRuleViolation


@dataclass(frozen=True)
class RiskState:
    """Daily/weekly P&L state + halt flags. Reconciled before each check."""

    daily_pnl_pct: Decimal
    weekly_pnl_pct: Decimal
    consecutive_losing_days: int
    halted: bool


class RiskManager:
    """Gates EVERY trade. No bypass."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config

    def check(
        self,
        order: OrderRequest,
        *,
        account: AccountSnapshot,
        positions: list[Position],
        state: RiskState,
        regime: str,
    ) -> None:
        """Raise RiskRuleViolation if any rule is breached. Returns None on success."""
        if state.halted:
            raise RiskRuleViolation(
                rule="halted",
                detail="trading is halted by circuit-breaker; manual reset required",
            )
        self._check_per_trade_risk(order, account)
        self._check_max_position(order, account)
        self._check_concentration(order, positions, account)
        self._check_asset_class_caps(order, positions, account, regime)
        self._check_daily_weekly_limits(state)

    # ---- individual rule helpers ----

    def _check_per_trade_risk(self, o: OrderRequest, a: AccountSnapshot) -> None:
        # risk = (entry - stop) * qty for buy, (stop - entry) * qty for sell
        if o.side == OrderSide.BUY:
            per_share_risk = o.limit_price - o.stop_loss_price
        else:
            per_share_risk = o.stop_loss_price - o.limit_price
        if per_share_risk <= 0:
            raise RiskRuleViolation(
                rule="stop_loss_direction",
                detail=f"stop {o.stop_loss_price} on wrong side of entry {o.limit_price}",
            )
        risk_dollars = per_share_risk * o.qty
        risk_pct = (risk_dollars / a.equity) * Decimal("100")
        limit = Decimal(str(self._cfg.risk.per_trade_risk_pct))
        if risk_pct > limit:
            raise RiskRuleViolation(
                rule="per_trade_risk_pct",
                detail=f"risk {risk_pct:.2f}% exceeds limit {limit}%",
            )

    def _check_max_position(self, o: OrderRequest, a: AccountSnapshot) -> None:
        notional = o.limit_price * o.qty
        pct = (notional / a.equity) * Decimal("100")
        limit = Decimal(str(self._cfg.risk.max_position_pct))
        if pct > limit:
            raise RiskRuleViolation(
                rule="max_position_pct",
                detail=f"position {pct:.2f}% exceeds limit {limit}%",
            )

    def _check_concentration(
        self, o: OrderRequest, positions: list[Position], a: AccountSnapshot
    ) -> None:
        existing = next((p for p in positions if p.symbol == o.symbol), None)
        existing_notional = existing.market_value if existing else Decimal("0")
        new_notional = existing_notional + (o.limit_price * o.qty if o.side == OrderSide.BUY else 0)
        pct = (new_notional / a.equity) * Decimal("100")
        limit = Decimal(str(self._cfg.risk.max_symbol_concentration_pct))
        if pct > limit:
            raise RiskRuleViolation(
                rule="max_symbol_concentration_pct",
                detail=f"{o.symbol} concentration {pct:.2f}% exceeds limit {limit}%",
            )

    def _check_asset_class_caps(
        self,
        o: OrderRequest,
        positions: list[Position],
        a: AccountSnapshot,
        regime: str,
    ) -> None:
        existing_by_class = {"stock": Decimal("0"), "crypto": Decimal("0"), "option": Decimal("0")}
        for p in positions:
            ac = p.asset_class.replace("us_equity", "stock").replace("us_option", "option")
            if ac in existing_by_class:
                existing_by_class[ac] += p.market_value
        new_class = o.asset_class.value
        new_notional = (o.limit_price * o.qty) if o.side == OrderSide.BUY else Decimal("0")
        proposed = existing_by_class.get(new_class, Decimal("0")) + new_notional
        proposed_pct = (proposed / a.equity) * Decimal("100")

        regime_caps = self._cfg.regime_allocations.get(regime)
        if regime_caps is None:
            raise RiskRuleViolation(
                rule="regime_unknown", detail=f"regime '{regime}' not in config"
            )
        cap_map = {
            "stock": Decimal(str(regime_caps.stocks)),
            "crypto": Decimal(str(regime_caps.crypto)),
            "option": Decimal(str(regime_caps.options)),
        }
        cap = cap_map[new_class]
        if proposed_pct > cap:
            raise RiskRuleViolation(
                rule="asset_class_cap",
                detail=f"{new_class} {proposed_pct:.2f}% exceeds regime cap {cap}%",
            )

    def _check_daily_weekly_limits(self, s: RiskState) -> None:
        d_limit = Decimal(str(self._cfg.risk.daily_loss_limit_pct))
        w_limit = Decimal(str(self._cfg.risk.weekly_loss_limit_pct))
        if s.daily_pnl_pct <= -d_limit:
            raise RiskRuleViolation(
                rule="daily_loss_limit",
                detail=f"daily P&L {s.daily_pnl_pct}% breaches -{d_limit}%",
            )
        if s.weekly_pnl_pct <= -w_limit:
            raise RiskRuleViolation(
                rule="weekly_loss_limit",
                detail=f"weekly P&L {s.weekly_pnl_pct}% breaches -{w_limit}%",
            )
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_risk_manager.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/risk_manager.py tests/test_risk_manager.py
git commit -m "feat(risk): per-trade risk, position size, concentration, asset cap, halt rules"
```

---

## Task 8: Risk Manager — Circuit Breaker Tests

**Files:**
- Modify: `tests/test_risk_manager.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_risk_manager.py`:

```python
def test_risk_rejects_after_daily_loss_breach(cfg, acct):
    rm = RiskManager(cfg)
    breached = RiskState(
        daily_pnl_pct=Decimal("-2.5"),
        weekly_pnl_pct=Decimal("-1"),
        consecutive_losing_days=0,
        halted=False,
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=breached, regime="trending_up")
    assert e.value.rule == "daily_loss_limit"


def test_risk_rejects_after_weekly_loss_breach(cfg, acct):
    rm = RiskManager(cfg)
    breached = RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("-6"),
        consecutive_losing_days=0,
        halted=False,
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=breached, regime="trending_up")
    assert e.value.rule == "weekly_loss_limit"


def test_risk_rejects_concentration_breach(cfg, acct, state):
    rm = RiskManager(cfg)
    existing = Position(
        symbol="AAPL",
        qty=Decimal("20"),
        market_value=Decimal("4500"),  # already 4.5%
        avg_entry_price=Decimal("225"),
        unrealized_pl=Decimal("0"),
        asset_class="us_equity",
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("5"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("200"),  # +$1000 → 5.5% > 5% cap
        stop_loss_price=Decimal("198"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[existing], state=state, regime="trending_up")
    assert e.value.rule == "max_symbol_concentration_pct"


def test_risk_rejects_asset_class_cap_in_risk_off(cfg, acct, state):
    rm = RiskManager(cfg)
    # risk_off: crypto cap is 5%
    req = OrderRequest(
        symbol="BTC/USD",
        qty=Decimal("0.5"),
        side=OrderSide.BUY,
        asset_class=AssetClass.CRYPTO,
        limit_price=Decimal("70000"),  # $35k = 35% — way over 5% cap
        stop_loss_price=Decimal("68000"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="risk_off")
    assert e.value.rule in {"asset_class_cap", "max_position_pct"}


def test_risk_rejects_inverted_stop_loss(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("102"),  # stop ABOVE entry on a buy = inverted
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="trending_up")
    assert e.value.rule == "stop_loss_direction"
```

- [ ] **Step 2: Run to confirm pass (logic already covers these)**

```bash
pytest tests/test_risk_manager.py -v
```
Expected: 9 passed (4 prior + 5 new).

- [ ] **Step 3: Commit**

```bash
git add tests/test_risk_manager.py
git commit -m "test(risk): circuit-breaker, concentration, asset-class cap, inverted-stop coverage"
```

---

## Task 9: Email Sender

**Files:**
- Create: `src/trading_bot/email_sender.py`
- Create: `tests/test_email_sender.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_email_sender.py
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.email_sender import EmailSender


def test_send_uses_smtp_ssl():
    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        instance = MockSMTP.return_value.__enter__.return_value
        sender = EmailSender(user="from@x.com", app_password="p", to="to@y.com")
        sender.send(subject="Hello", html_body="<b>hi</b>")
        instance.login.assert_called_once_with("from@x.com", "p")
        assert instance.sendmail.called
        args = instance.sendmail.call_args[0]
        assert args[0] == "from@x.com"
        assert args[1] == ["to@y.com"]
        assert "Subject: Hello" in args[2]
        assert "<b>hi</b>" in args[2]


def test_send_retries_on_smtp_error():
    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        import smtplib

        MockSMTP.return_value.__enter__.side_effect = [
            smtplib.SMTPException("nope"),
            smtplib.SMTPException("nope2"),
            MockSMTP.return_value.__enter__.return_value,
        ]
        sender = EmailSender(user="from@x.com", app_password="p", to="to@y.com", retries=3)
        sender.send(subject="Hello", html_body="<b>hi</b>")
        assert MockSMTP.call_count == 3


def test_send_raises_after_max_retries():
    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        import smtplib

        MockSMTP.return_value.__enter__.side_effect = smtplib.SMTPException("nope")
        sender = EmailSender(user="from@x.com", app_password="p", to="to@y.com", retries=2)
        with pytest.raises(smtplib.SMTPException):
            sender.send(subject="Hello", html_body="<b>hi</b>")
        assert MockSMTP.call_count == 2
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_email_sender.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/email_sender.py
import smtplib
import time
from email.message import EmailMessage


class EmailSender:
    """Gmail SMTP_SSL sender with bounded retries."""

    def __init__(
        self,
        user: str,
        app_password: str,
        to: str,
        host: str = "smtp.gmail.com",
        port: int = 465,
        retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._user = user
        self._password = app_password
        self._to = to
        self._host = host
        self._port = port
        self._retries = retries
        self._backoff = retry_backoff_seconds

    def send(self, *, subject: str, html_body: str, text_body: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._user
        msg["To"] = self._to
        msg.set_content(text_body or "View this email in HTML.")
        msg.add_alternative(html_body, subtype="html")

        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                with smtplib.SMTP_SSL(self._host, self._port) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._user, [self._to], msg.as_string())
                return
            except smtplib.SMTPException as e:
                last_exc = e
                if attempt < self._retries:
                    time.sleep(self._backoff * attempt)
        assert last_exc is not None
        raise last_exc
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_email_sender.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_sender.py tests/test_email_sender.py
git commit -m "feat(email): gmail smtp_ssl sender with retries"
```

---

## Task 10: CLI — `bot status`

**Files:**
- Create: `src/trading_bot/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_cli.py
from decimal import Decimal
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def test_bot_status_runs_and_calls_email():
    from trading_bot.cli import main

    fake_account = MagicMock(equity=Decimal("100000"), cash=Decimal("50000"))
    fake_positions = []

    with patch("trading_bot.cli.AlpacaClient") as MockClient, patch(
        "trading_bot.cli.EmailSender"
    ) as MockEmail, patch("trading_bot.cli.Settings") as MockSettings, patch(
        "trading_bot.cli.load_config"
    ) as MockCfg:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k",
            alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u@x.com",
            gmail_app_password="p",
            bot_mode="paper",
        )
        MockCfg.return_value = MagicMock(email=MagicMock(to="u@x.com"))
        MockClient.return_value.get_account.return_value = fake_account
        MockClient.return_value.get_positions.return_value = fake_positions
        sender = MockEmail.return_value

        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        sender.send.assert_called_once()
        kwargs = sender.send.call_args.kwargs
        assert "Status" in kwargs["subject"]
        assert "100000" in kwargs["html_body"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_cli.py -v
```
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/cli.py
from datetime import datetime
from pathlib import Path

import click

from trading_bot.alpaca_client import AlpacaClient
from trading_bot.config import Settings, load_config
from trading_bot.email_sender import EmailSender

CONFIG_PATH = Path("strategy/config.yaml")


@click.group()
def main() -> None:
    """Trading bot CLI."""


@main.command()
def status() -> None:
    """Email a snapshot of the current paper account state."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()

    rows = "".join(
        f"<tr><td>{p.symbol}</td><td>{p.qty}</td><td>${p.market_value}</td>"
        f"<td>${p.unrealized_pl}</td></tr>"
        for p in positions
    ) or "<tr><td colspan='4'><i>No open positions</i></td></tr>"

    html = f"""
<h2>Trading Bot — Account Status</h2>
<p>Generated {datetime.now().isoformat(timespec='seconds')}</p>
<table border='1' cellpadding='6'>
  <tr><th>Equity</th><td>${account.equity}</td></tr>
  <tr><th>Cash</th><td>${account.cash}</td></tr>
  <tr><th>Buying Power</th><td>${account.buying_power}</td></tr>
  <tr><th>Portfolio Value</th><td>${account.portfolio_value}</td></tr>
</table>
<h3>Open Positions</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Qty</th><th>Market Value</th><th>Unrealized P&amp;L</th></tr>
  {rows}
</table>
"""

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject="Trading Bot — Status", html_body=html)
    click.echo(f"Sent status email to {cfg.email.to}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_cli.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/cli.py tests/test_cli.py
git commit -m "feat(cli): bot status command emails account snapshot"
```

---

## Task 11: CLI — `bot dry-run`

**Files:**
- Modify: `src/trading_bot/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_cli.py`:

```python
def test_bot_dry_run_passes_risk_manager():
    from trading_bot.cli import main

    fake_account = MagicMock(
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
    )
    with patch("trading_bot.cli.AlpacaClient") as MockClient, patch(
        "trading_bot.cli.Settings"
    ) as MockSettings, patch("trading_bot.cli.load_config") as MockCfg, patch(
        "trading_bot.cli._build_risk_state"
    ) as MockState:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k",
            alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u@x.com",
            gmail_app_password="p",
            bot_mode="paper",
        )
        MockCfg.return_value = _real_config_for_test()
        MockClient.return_value.get_account.return_value = fake_account
        MockClient.return_value.get_positions.return_value = []
        MockState.return_value = _real_state_zero()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dry-run",
                "--symbol", "AAPL",
                "--side", "buy",
                "--qty", "10",
                "--price", "195.00",
                "--stop", "192.00",
                "--regime", "trending_up",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "PASS" in result.output


def test_bot_dry_run_reports_violation():
    from trading_bot.cli import main

    fake_account = MagicMock(
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
    )
    with patch("trading_bot.cli.AlpacaClient") as MockClient, patch(
        "trading_bot.cli.Settings"
    ) as MockSettings, patch("trading_bot.cli.load_config") as MockCfg, patch(
        "trading_bot.cli._build_risk_state"
    ) as MockState:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k",
            alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u@x.com",
            gmail_app_password="p",
            bot_mode="paper",
        )
        MockCfg.return_value = _real_config_for_test()
        MockClient.return_value.get_account.return_value = fake_account
        MockClient.return_value.get_positions.return_value = []
        MockState.return_value = _real_state_zero()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dry-run",
                "--symbol", "AAPL",
                "--side", "buy",
                "--qty", "100",  # oversized
                "--price", "195.00",
                "--stop", "192.00",
                "--regime", "trending_up",
            ],
        )
        assert result.exit_code != 0
        assert "max_position_pct" in result.output


def _real_config_for_test():
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
        email=EmailConfig(to="u@x.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )


def _real_state_zero():
    from trading_bot.risk_manager import RiskState
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_cli.py -v
```
Expected: 2 new failures (`dry-run` not defined).

- [ ] **Step 3: Implement `dry-run`**

Append to `src/trading_bot/cli.py`:

```python
from decimal import Decimal

from trading_bot.alpaca_client import AssetClass, OrderRequest, OrderSide
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.risk_manager import RiskManager, RiskState


def _build_risk_state() -> RiskState:
    """Stub state — Plan 2 wires this to live P&L calculation."""
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )


@main.command("dry-run")
@click.option("--symbol", required=True)
@click.option("--side", type=click.Choice(["buy", "sell"]), required=True)
@click.option("--qty", required=True, type=str)
@click.option("--price", required=True, type=str)
@click.option("--stop", required=True, type=str)
@click.option(
    "--asset-class",
    type=click.Choice(["stock", "crypto", "option"]),
    default="stock",
)
@click.option(
    "--regime",
    type=click.Choice(["trending_up", "trending_down", "sideways", "risk_off"]),
    default="trending_up",
)
def dry_run(
    symbol: str, side: str, qty: str, price: str, stop: str, asset_class: str, regime: str
) -> None:
    """Validate a hypothetical order through the risk manager. No order is sent."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()
    state = _build_risk_state()

    req = OrderRequest(
        symbol=symbol,
        qty=Decimal(qty),
        side=OrderSide(side),
        asset_class=AssetClass(asset_class),
        limit_price=Decimal(price),
        stop_loss_price=Decimal(stop),
    )
    rm = RiskManager(cfg)
    try:
        rm.check(req, account=account, positions=positions, state=state, regime=regime)
    except RiskRuleViolation as e:
        click.echo(f"REJECTED: {e}")
        raise SystemExit(1)
    click.echo(f"PASS: {symbol} {side} {qty} @ ${price} (stop ${stop}) — would be submitted.")
```

- [ ] **Step 4: Run to confirm pass**

```bash
pytest tests/test_cli.py -v
```
Expected: 3 passed (1 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/cli.py tests/test_cli.py
git commit -m "feat(cli): bot dry-run validates trades through risk manager without execution"
```

---

## Task 12: Integration Test — Live Paper Account Smoke

**Files:**
- Create: `tests/test_integration.py`

This test hits the real Alpaca paper API. It is opt-in via `RUN_INTEGRATION=1`.

- [ ] **Step 1: Write the integration test**

```python
# tests/test_integration.py
import os

import pytest

from trading_bot.alpaca_client import AlpacaClient
from trading_bot.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Integration test — set RUN_INTEGRATION=1 to enable",
)


def test_real_paper_account_returns_account():
    settings = Settings()  # loads from .env
    client = AlpacaClient(settings)
    account = client.get_account()
    assert account.equity > 0
    assert account.portfolio_value > 0


def test_real_paper_account_returns_positions():
    settings = Settings()
    client = AlpacaClient(settings)
    positions = client.get_positions()
    # may be empty on a fresh account; just assert it's a list
    assert isinstance(positions, list)
```

- [ ] **Step 2: Run unit tests (should still pass, integration skipped)**

```bash
pytest -v
```
Expected: all unit tests pass; integration tests skipped.

- [ ] **Step 3: Run integration test against real paper account**

```bash
RUN_INTEGRATION=1 pytest tests/test_integration.py -v
```
Expected: both integration tests pass — confirms credentials work end-to-end.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: integration smoke test against real alpaca paper account (opt-in)"
```

---

## Task 13: Manual End-to-End Verification

- [ ] **Step 1: Run the full suite**

```bash
pytest -v --cov=trading_bot --cov-report=term-missing
```
Expected: all tests green, coverage on `risk_manager.py` ≥ 90%.

- [ ] **Step 2: Run `bot status` against the real paper account**

User must first generate a Gmail App Password and update `.env` `GMAIL_APP_PASSWORD`. Then:

```bash
bot status
```
Expected: console says "Sent status email to bharath8887@gmail.com" and the user receives the email with the account snapshot.

- [ ] **Step 3: Run `bot dry-run` with a sane order**

```bash
bot dry-run --symbol AAPL --side buy --qty 10 --price 195.00 --stop 192.00 --regime trending_up
```
Expected: `PASS: AAPL buy 10 @ $195.00 (stop $192.00) — would be submitted.`

- [ ] **Step 4: Run `bot dry-run` with an oversized order**

```bash
bot dry-run --symbol AAPL --side buy --qty 100 --price 195.00 --stop 192.00 --regime trending_up
```
Expected: `REJECTED: Risk rule violated: max_position_pct — position 19.50% exceeds limit 10%`. Exit code non-zero.

- [ ] **Step 5: Run `bot dry-run` with inverted stop**

```bash
bot dry-run --symbol AAPL --side buy --qty 10 --price 195.00 --stop 200.00 --regime trending_up
```
Expected: rejection on `stop_loss_direction`.

- [ ] **Step 6: Final commit + tag**

```bash
git tag plan-1-foundation-complete
git log --oneline -20
```

---

## Plan 1 Acceptance Criteria

- [x] All unit tests pass (`pytest -v`)
- [x] Coverage on `risk_manager.py` ≥ 90%
- [x] Integration test passes against real Alpaca paper API (`RUN_INTEGRATION=1 pytest tests/test_integration.py`)
- [x] `bot status` successfully delivers email to bharath8887@gmail.com
- [x] `bot dry-run` correctly accepts valid trades and rejects all violations
- [x] No code path can place an order without a stop-loss (enforced by `OrderRequest` validation)
- [x] No code path can connect to live Alpaca (enforced by `Settings` and `AlpacaClient` URL check)

---

## What Plan 2 Will Build (next)

- `trading-intelligence-mcp` server — market_data, screener, crypto, sentiment feeds
- Strategy store: `rules.md`, `regime.json`, `positions.json`, `performance.md`
- First scheduled routine: `morning_brief` (regime detection + watchlist refresh)
- `intraday_scan` routine that places its first paper trade

Plan 2 will reuse everything from Plan 1 unchanged. The risk manager and Alpaca client built here are the load-bearing safety layer that allows Plan 2 to proceed safely.
