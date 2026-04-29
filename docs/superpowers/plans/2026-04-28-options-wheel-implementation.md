# Options Wheel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an intelligent wheel-strategy lane (cash-secured puts → covered calls cycle) on Alpaca paper, with full wiring into existing config / state DB / alpaca client / risk manager / intelligence / orchestrator / reconciler / scheduler / alerts / emails / dashboard / CLI.

**Architecture:** New `trading_bot.options` package owns chain helpers, IV-rank, wheel state machine, and the lane. Two new intelligence clients (Finnhub, ApeWisdom) plug into the existing graceful-degradation pattern. One Alembic migration adds three tables (`wheel_cycles`, `option_iv_history`, `option_fills`, `wheel_universe_cache`). Existing surfaces gain wheel sections via the documented extension points (alerts kinds, email sections, dashboard fragments, scheduler jobs, CLI subcommands).

**Tech Stack:** Python 3.12, alpaca-py, pydantic v2, SQLAlchemy + Alembic, APScheduler, FastAPI (dashboard), pytest, requests.

**Spec:** [`docs/superpowers/specs/2026-04-28-options-wheel-design.md`](../specs/2026-04-28-options-wheel-design.md)

---

## File Structure

### New files
| Path | Responsibility |
|---|---|
| `migrations/versions/011_wheel_strategy.py` | Adds `option_fills`, `option_iv_history`, `wheel_cycles`, `wheel_universe_cache` tables |
| `src/trading_bot/options/__init__.py` | Package marker |
| `src/trading_bot/options/symbols.py` | OCC contract-symbol parse/format helpers |
| `src/trading_bot/options/chain.py` | `ChainContract` dataclass, contract-pickers (CSP/CC) and liquidity gate |
| `src/trading_bot/options/iv_rank.py` | Daily ATM-IV capture + rank/percentile compute |
| `src/trading_bot/options/wheel_state.py` | DB-backed cycle state machine |
| `src/trading_bot/options/wheel_universe.py` | Dynamic universe filter pipeline (with cache) |
| `src/trading_bot/options/alpaca_options.py` | Wraps `OptionHistoricalDataClient` + option order submission via `TradingClient` |
| `src/trading_bot/options/wheel_lane.py` | The `Lane`-protocol implementation that produces wheel candidates |
| `src/trading_bot/options/wheel_runner.py` | `run_wheel_scan()` and `run_wheel_manage()` entry points |
| `src/trading_bot/intelligence_finnhub.py` | Free-tier Finnhub client (earnings, profile, corp actions) |
| `src/trading_bot/intelligence_apewisdom.py` | ApeWisdom WSB mentions client |
| `strategy/wheel_blocklist.yaml` | User override — never wheel these |
| `strategy/wheel_allowlist.yaml` | User override — force into universe |
| `src/trading_bot/dashboard/templates/_wheel.html` | Dashboard fragment |
| `tests/test_wheel_*.py` | One test file per module above |

### Modified files
| Path | What changes |
|---|---|
| `src/trading_bot/config.py` | Add `WheelConfig` model + `Settings.finnhub_api_key` |
| `strategy/config.yaml` | Add `wheel:` block |
| `src/trading_bot/state_db.py` | Add ORM classes for the 4 new tables |
| `src/trading_bot/alpaca_client.py` | Add option-order paths + `get_option_positions` |
| `src/trading_bot/risk_manager.py` | Add `option_collateral_ok()` |
| `src/trading_bot/alerts.py` | Add 9 new `kind` values |
| `src/trading_bot/email_digest.py` | Add `wheel_*` fields to `DigestContext`, render Wheel section |
| `src/trading_bot/email_midday.py` | Add wheel watchlist section |
| `src/trading_bot/email_fill.py` | Add option `fill_type` cases |
| `src/trading_bot/dashboard/app.py` | Register `/fragment/wheel` |
| `src/trading_bot/dashboard/data.py` | Add wheel fields to `DashboardSnapshot` |
| `src/trading_bot/reconciler.py` | Diff `option_fills` and write closed-trade outcomes |
| `src/trading_bot/scheduler_jobs.py` | Register `wheel_scan` + `wheel_manage` cron jobs |
| `src/trading_bot/cadence.py` | Add `wheel_scan_enabled`, `wheel_manage_interval_minutes` |
| `src/trading_bot/cli.py` | Add `wheel-scan`, `wheel-manage`, `wheel-status`, `wheel-close` subcommands |
| `src/trading_bot/daemon.py` | Wire wheel runners into the runners dict |
| `src/trading_bot/evolution.py` | Read `wheel_cycles` for parameter-tweak proposals |

---

## Phase 0 — Bootstrap (1 task)

### Task 0.1: Create the `options` package skeleton

**Files:**
- Create: `src/trading_bot/options/__init__.py`
- Create: `tests/test_wheel_package.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wheel_package.py
def test_options_package_importable():
    import trading_bot.options  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_wheel_package.py -v`
Expected: `ModuleNotFoundError: No module named 'trading_bot.options'`

- [ ] **Step 3: Create empty package**

```python
# src/trading_bot/options/__init__.py
"""Options trading package — wheel strategy implementation."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_wheel_package.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/__init__.py tests/test_wheel_package.py
git commit -m "feat(options): create options package skeleton"
```

---

## Phase 1 — Config + Migrations + State DB

### Task 1.1: Add `WheelConfig` to `config.py`

**Files:**
- Modify: `src/trading_bot/config.py`
- Modify: `strategy/config.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test (append to test_config.py)**

```python
def test_wheel_config_defaults(tmp_path):
    from trading_bot.config import load_config
    cfg = tmp_path / "c.yaml"
    cfg.write_text("""
risk: {daily_loss_limit_pct: 2, weekly_loss_limit_pct: 5, per_trade_risk_pct: 1, max_position_pct: 10, max_symbol_concentration_pct: 5, max_consecutive_losing_days: 3}
allocation: {stocks_max_pct: 70, crypto_max_pct: 30, options_max_pct: 20, cash_floor_pct: 10}
regime_allocations:
  trending_up: {stocks: 60, crypto: 25, options: 15, cash: 0}
email: {to: x@y.com, daily_summary_time_et: "16:30", weekly_summary_day: Sunday}
storage: {trade_journal_path: data/x.db}
wheel: {enabled: true}
""")
    out = load_config(cfg)
    assert out.wheel.enabled is True
    assert out.wheel.delta_target_low == 0.20
    assert out.wheel.delta_target_high == 0.30
    assert out.wheel.dte_min == 30
    assert out.wheel.dte_max == 45
    assert out.wheel.take_profit_pct == 0.50
    assert out.wheel.dte_force_close == 21
    assert out.wheel.iv_rank_floor == 30.0
    assert out.wheel.min_open_interest == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_wheel_config_defaults -v`
Expected: FAIL — `wheel` field unknown.

- [ ] **Step 3: Add `WheelConfig` model and field**

```python
# src/trading_bot/config.py — add Settings field
class Settings(BaseSettings):
    # ... existing fields ...
    finnhub_api_key: str = ""
```

```python
# src/trading_bot/config.py — add WheelConfig (place above AppConfig)
class WheelConfig(BaseModel):
    enabled: bool = False
    delta_target_low: float = Field(default=0.20, ge=0.05, le=0.50)
    delta_target_high: float = Field(default=0.30, ge=0.05, le=0.50)
    dte_min: int = Field(default=30, ge=7, le=90)
    dte_max: int = Field(default=45, ge=7, le=90)
    take_profit_pct: float = Field(default=0.50, gt=0, lt=1)
    dte_force_close: int = Field(default=21, ge=1, le=45)
    delta_breach_csp: float = Field(default=0.45, gt=0, lt=1)
    delta_breach_cc: float = Field(default=0.55, gt=0, lt=1)
    max_rolls_per_cycle: int = Field(default=2, ge=0, le=5)
    iv_rank_floor: float = Field(default=30.0, ge=0, le=100)
    vix_floor: float = Field(default=15.0, ge=0, le=100)
    vix_ceiling: float = Field(default=30.0, ge=0, le=100)
    sentiment_floor: float = Field(default=-0.3, ge=-1, le=1)
    min_premium_abs: float = Field(default=0.20, ge=0)
    min_annualized_yield: float = Field(default=0.12, ge=0)
    min_open_interest: int = Field(default=100, ge=0)
    universe_cache_hours: int = Field(default=24, ge=1, le=168)
    wsb_spike_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    blocklist_path: str = "strategy/wheel_blocklist.yaml"
    allowlist_path: str = "strategy/wheel_allowlist.yaml"
```

```python
# src/trading_bot/config.py — extend AppConfig
class AppConfig(BaseModel):
    risk: RiskConfig
    allocation: AllocationConfig
    regime_allocations: dict[str, RegimeAllocation]
    email: EmailConfig
    storage: StorageConfig
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    wheel: WheelConfig = Field(default_factory=WheelConfig)
```

- [ ] **Step 4: Append wheel block to strategy/config.yaml**

```yaml
wheel:
  enabled: false  # paper-test cautiously — flip to true after Phase 3 verification
  delta_target_low: 0.20
  delta_target_high: 0.30
  dte_min: 30
  dte_max: 45
  take_profit_pct: 0.50
  dte_force_close: 21
  delta_breach_csp: 0.45
  delta_breach_cc: 0.55
  max_rolls_per_cycle: 2
  iv_rank_floor: 30.0
  vix_floor: 15.0
  vix_ceiling: 30.0
  sentiment_floor: -0.3
  min_premium_abs: 0.20
  min_annualized_yield: 0.12
  min_open_interest: 100
  universe_cache_hours: 24
  wsb_spike_multiplier: 2.0
```

- [ ] **Step 5: Run all config tests, verify pass**

Run: `pytest tests/test_config.py -v`
Expected: all PASS, including new `test_wheel_config_defaults`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/config.py strategy/config.yaml tests/test_config.py
git commit -m "feat(config): WheelConfig + finnhub_api_key + yaml block"
```

---

### Task 1.2: Alembic migration for wheel tables

**Files:**
- Create: `migrations/versions/011_wheel_strategy.py`
- Test: `tests/test_state_db.py` (append)

- [ ] **Step 1: Write the failing test (append to test_state_db.py)**

```python
def test_wheel_tables_present_after_migration(tmp_path, monkeypatch):
    """Smoke-test: after `alembic upgrade head`, the four wheel tables exist."""
    import subprocess
    import os
    db = tmp_path / "test_state.db"
    monkeypatch.setenv("STATE_DB_URL", f"sqlite:///{db}")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        ["alembic", "upgrade", "head"], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "STATE_DB_URL": f"sqlite:///{db}"},
    )
    assert r.returncode == 0, r.stderr
    import sqlite3
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    for t in ("option_fills", "option_iv_history", "wheel_cycles", "wheel_universe_cache"):
        assert t in tables, f"missing table {t}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_state_db.py::test_wheel_tables_present_after_migration -v`
Expected: FAIL — tables missing.

- [ ] **Step 3: Create migration file**

```python
# migrations/versions/011_wheel_strategy.py
"""wheel strategy: option_fills, option_iv_history, wheel_cycles, wheel_universe_cache

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-28 12:00:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'option_fills',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('underlying', sa.String(length=16), nullable=False, index=True),
        sa.Column('contract_symbol', sa.String(length=32), nullable=False),
        sa.Column('option_type', sa.String(length=4), nullable=False),  # CSP|CC|ROLL
        sa.Column('side', sa.String(length=8), nullable=False),  # SELL|BUY
        sa.Column('strike', sa.Numeric(20, 4), nullable=False),
        sa.Column('expiration', sa.Date(), nullable=False),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('premium', sa.Numeric(20, 4), nullable=False),
        sa.Column('alpaca_order_id', sa.String(length=64), nullable=False),
        sa.Column('cycle_id', sa.String(length=64), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False, server_default=''),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('alpaca_order_id'),
    )
    op.create_table(
        'option_iv_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(length=16), nullable=False, index=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('atm_iv_30d', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'recorded_at', name='uq_iv_history_symbol_recorded'),
    )
    op.create_table(
        'wheel_cycles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cycle_id', sa.String(length=64), nullable=False, unique=True),
        sa.Column('symbol', sa.String(length=16), nullable=False, index=True),
        sa.Column('phase', sa.String(length=32), nullable=False),  # csp_open|assigned|cc_open|closed
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('csp_contract', sa.String(length=32), nullable=True),
        sa.Column('csp_strike', sa.Numeric(20, 4), nullable=True),
        sa.Column('csp_expiration', sa.Date(), nullable=True),
        sa.Column('csp_credit', sa.Numeric(20, 4), nullable=True),
        sa.Column('cc_contract', sa.String(length=32), nullable=True),
        sa.Column('cc_strike', sa.Numeric(20, 4), nullable=True),
        sa.Column('cc_expiration', sa.Date(), nullable=True),
        sa.Column('cc_credit', sa.Numeric(20, 4), nullable=True),
        sa.Column('rolls_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_basis', sa.Numeric(20, 4), nullable=True),
        sa.Column('realized_pnl', sa.Numeric(20, 4), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'wheel_universe_cache',
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('eligible', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('cached_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('symbol'),
    )


def downgrade() -> None:
    op.drop_table('wheel_universe_cache')
    op.drop_table('wheel_cycles')
    op.drop_table('option_iv_history')
    op.drop_table('option_fills')
```

- [ ] **Step 4: Run migration test, verify pass**

Run: `pytest tests/test_state_db.py::test_wheel_tables_present_after_migration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/011_wheel_strategy.py tests/test_state_db.py
git commit -m "feat(db): migration 011 — wheel cycles + option fills + iv history + universe cache"
```

---

### Task 1.3: Add wheel ORM classes to `state_db.py`

**Files:**
- Modify: `src/trading_bot/state_db.py`
- Test: `tests/test_state_db.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_wheel_orm_round_trip(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from trading_bot.state_db import Base, WheelCycle, OptionFill, OptionIvHistory, WheelUniverseCache
    import datetime as dt
    db_path = tmp_path / "rt.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(WheelCycle(cycle_id="c1", symbol="AAPL", phase="csp_open",
                         opened_at=dt.datetime.now(dt.timezone.utc)))
        s.add(OptionFill(ts=dt.datetime.now(dt.timezone.utc), underlying="AAPL",
                         contract_symbol="AAPL250516P00190000", option_type="CSP",
                         side="SELL", strike=190, expiration=dt.date(2025, 5, 16),
                         qty=1, premium=2.10, alpaca_order_id="ord1", cycle_id="c1"))
        s.add(OptionIvHistory(symbol="AAPL",
                              recorded_at=dt.datetime.now(dt.timezone.utc), atm_iv_30d=0.27))
        s.add(WheelUniverseCache(symbol="AAPL", eligible=True, reason="",
                                 cached_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()
    with Session(engine) as s:
        assert s.query(WheelCycle).count() == 1
        assert s.query(OptionFill).count() == 1
        assert s.query(OptionIvHistory).count() == 1
        assert s.query(WheelUniverseCache).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_state_db.py::test_wheel_orm_round_trip -v`
Expected: FAIL — classes don't exist.

- [ ] **Step 3: Append ORM classes to state_db.py**

```python
# src/trading_bot/state_db.py — append (use existing imports plus Numeric, Boolean, Date)
from sqlalchemy import Boolean, Date, Numeric


class OptionFill(Base):
    __tablename__ = "option_fills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    underlying = Column(String(16), nullable=False, index=True)
    contract_symbol = Column(String(32), nullable=False)
    option_type = Column(String(4), nullable=False)  # CSP|CC|ROLL
    side = Column(String(8), nullable=False)  # SELL|BUY
    strike = Column(Numeric(20, 4), nullable=False)
    expiration = Column(Date, nullable=False)
    qty = Column(Integer, nullable=False)
    premium = Column(Numeric(20, 4), nullable=False)
    alpaca_order_id = Column(String(64), nullable=False, unique=True)
    cycle_id = Column(String(64), nullable=True)
    notes = Column(Text, nullable=False, default="")


class OptionIvHistory(Base):
    __tablename__ = "option_iv_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(16), nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    atm_iv_30d = Column(Float, nullable=False)


class WheelCycle(Base):
    __tablename__ = "wheel_cycles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(String(64), nullable=False, unique=True)
    symbol = Column(String(16), nullable=False, index=True)
    phase = Column(String(32), nullable=False)
    opened_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    csp_contract = Column(String(32), nullable=True)
    csp_strike = Column(Numeric(20, 4), nullable=True)
    csp_expiration = Column(Date, nullable=True)
    csp_credit = Column(Numeric(20, 4), nullable=True)
    cc_contract = Column(String(32), nullable=True)
    cc_strike = Column(Numeric(20, 4), nullable=True)
    cc_expiration = Column(Date, nullable=True)
    cc_credit = Column(Numeric(20, 4), nullable=True)
    rolls_used = Column(Integer, nullable=False, default=0)
    cost_basis = Column(Numeric(20, 4), nullable=True)
    realized_pnl = Column(Numeric(20, 4), nullable=False, default=0)


class WheelUniverseCache(Base):
    __tablename__ = "wheel_universe_cache"
    symbol = Column(String(16), primary_key=True)
    eligible = Column(Boolean, nullable=False)
    reason = Column(Text, nullable=False, default="")
    cached_at = Column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_state_db.py::test_wheel_orm_round_trip -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/state_db.py tests/test_state_db.py
git commit -m "feat(state-db): wheel + option fills + iv history + universe cache ORM"
```

---

## Phase 2 — Data sources & Alpaca options client

### Task 2.1: OCC contract-symbol parser

**Files:**
- Create: `src/trading_bot/options/symbols.py`
- Create: `tests/test_options_symbols.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_symbols.py
import datetime as dt
import pytest
from trading_bot.options.symbols import parse_occ, format_occ, OccContract


def test_parse_aapl_call():
    c = parse_occ("AAPL250117C00190000")
    assert c == OccContract(underlying="AAPL", expiration=dt.date(2025, 1, 17),
                            kind="C", strike=190.0)


def test_parse_spy_put_with_decimal_strike():
    c = parse_occ("SPY250516P00425500")
    assert c.strike == 425.5
    assert c.kind == "P"


def test_format_round_trip():
    c = OccContract(underlying="QQQ", expiration=dt.date(2026, 6, 19),
                    kind="C", strike=505.0)
    assert format_occ(c) == "QQQ260619C00505000"


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_occ("nope")
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_options_symbols.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/options/symbols.py
"""OCC option symbol parse/format. Format: <UND><YYMMDD><C|P><STRIKE*1000 padded to 8>."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class OccContract:
    underlying: str
    expiration: dt.date
    kind: str  # "C" | "P"
    strike: float


def parse_occ(symbol: str) -> OccContract:
    m = _OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"not an OCC contract: {symbol!r}")
    und, yymmdd, kind, strike8 = m.groups()
    expiration = dt.datetime.strptime(yymmdd, "%y%m%d").date()
    strike = int(strike8) / 1000.0
    return OccContract(underlying=und, expiration=expiration, kind=kind, strike=strike)


def format_occ(c: OccContract) -> str:
    yymmdd = c.expiration.strftime("%y%m%d")
    strike8 = f"{int(round(c.strike * 1000)):08d}"
    return f"{c.underlying}{yymmdd}{c.kind}{strike8}"
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_options_symbols.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/symbols.py tests/test_options_symbols.py
git commit -m "feat(options): OCC contract symbol parse/format"
```

---

### Task 2.2: ChainContract dataclass + chain pickers

**Files:**
- Create: `src/trading_bot/options/chain.py`
- Create: `tests/test_options_chain.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_chain.py
import datetime as dt
import pytest
from trading_bot.options.chain import (
    ChainContract, pick_csp_contract, pick_cc_contract, passes_liquidity,
)
from trading_bot.config import WheelConfig


def _c(strike, kind, delta, *, dte=35, bid=2.0, ask=2.10, oi=500, iv=0.30):
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}{kind}{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind=kind, strike=strike,
        bid=bid, ask=ask, last=bid + 0.05, volume=100, open_interest=oi,
        implied_volatility=iv, delta=delta,
    )


def test_pick_csp_chooses_closest_to_target_within_band():
    cfg = WheelConfig(enabled=True)
    chain = [
        _c(200, "P", -0.18),
        _c(195, "P", -0.22),
        _c(190, "P", -0.27),  # closest to 0.25 inside [0.20, 0.30]
        _c(185, "P", -0.33),
    ]
    today = dt.date(2026, 4, 28)
    pick = pick_csp_contract(chain, cfg=cfg, today=today)
    assert pick is not None and pick.strike == 190


def test_pick_csp_returns_none_when_no_contract_in_delta_band():
    cfg = WheelConfig(enabled=True)
    chain = [_c(200, "P", -0.10), _c(180, "P", -0.40)]  # all outside band
    today = dt.date(2026, 4, 28)
    assert pick_csp_contract(chain, cfg=cfg, today=today) is None


def test_pick_csp_skips_contracts_outside_dte_window():
    cfg = WheelConfig(enabled=True)
    chain = [_c(190, "P", -0.25, dte=10), _c(190, "P", -0.25, dte=70)]
    today = dt.date(2026, 4, 28)
    assert pick_csp_contract(chain, cfg=cfg, today=today) is None


def test_pick_cc_requires_strike_at_or_above_cost_basis():
    cfg = WheelConfig(enabled=True)
    chain = [
        _c(195, "C", 0.27),  # below cost basis 200 — disallowed
        _c(205, "C", 0.25),
        _c(215, "C", 0.18),  # outside delta band
    ]
    today = dt.date(2026, 4, 28)
    pick = pick_cc_contract(chain, cost_basis=200.0, cfg=cfg, today=today)
    assert pick is not None and pick.strike == 205


def test_passes_liquidity_spread_pct_path():
    cfg = WheelConfig(enabled=True)
    c = _c(190, "P", -0.25, bid=2.0, ask=2.08, oi=200)  # 4% spread
    assert passes_liquidity(c, cfg) is True


def test_passes_liquidity_absolute_path():
    cfg = WheelConfig(enabled=True)
    c = _c(190, "P", -0.25, bid=0.50, ask=0.58, oi=200)  # 16% but $0.08 absolute
    assert passes_liquidity(c, cfg) is True


def test_passes_liquidity_fails_low_oi():
    cfg = WheelConfig(enabled=True)
    c = _c(190, "P", -0.25, oi=50)
    assert passes_liquidity(c, cfg) is False
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_options_chain.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/options/chain.py
"""Options chain — dataclass + contract pickers + liquidity gate."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trading_bot.config import WheelConfig


@dataclass(frozen=True)
class ChainContract:
    contract_symbol: str
    underlying: str
    expiration: dt.date
    kind: str  # "C" | "P"
    strike: float
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_volatility: float
    delta: float  # signed: puts negative, calls positive


def _dte(c: ChainContract, today: dt.date) -> int:
    return (c.expiration - today).days


def passes_liquidity(c: ChainContract, cfg: WheelConfig) -> bool:
    if c.open_interest < cfg.min_open_interest:
        return False
    mid = (c.bid + c.ask) / 2.0
    if mid <= 0:
        return False
    spread = c.ask - c.bid
    if spread <= 0.10 or (spread / mid) <= 0.05:
        return True
    return False


def pick_csp_contract(
    chain: list[ChainContract], *, cfg: WheelConfig, today: dt.date,
) -> ChainContract | None:
    """Pick the put with abs(delta) closest to 0.25 inside [delta_target_low, high]
    and DTE inside [dte_min, dte_max]. Liquidity must pass. Returns None if no fit."""
    target = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
    candidates = [
        c for c in chain
        if c.kind == "P"
        and cfg.dte_min <= _dte(c, today) <= cfg.dte_max
        and cfg.delta_target_low <= abs(c.delta) <= cfg.delta_target_high
        and passes_liquidity(c, cfg)
        and c.bid >= cfg.min_premium_abs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def pick_cc_contract(
    chain: list[ChainContract], *, cost_basis: float, cfg: WheelConfig, today: dt.date,
) -> ChainContract | None:
    """Pick a call with strike >= cost_basis, abs(delta) inside band, DTE in window."""
    target = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
    candidates = [
        c for c in chain
        if c.kind == "C"
        and c.strike >= cost_basis
        and cfg.dte_min <= _dte(c, today) <= cfg.dte_max
        and cfg.delta_target_low <= abs(c.delta) <= cfg.delta_target_high
        and passes_liquidity(c, cfg)
        and c.bid >= cfg.min_premium_abs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def annualized_yield(c: ChainContract, today: dt.date) -> float:
    dte = max(_dte(c, today), 1)
    collateral = c.strike * 100.0
    if collateral <= 0:
        return 0.0
    return (c.bid * 100.0 / collateral) * (365.0 / dte)
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_options_chain.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/chain.py tests/test_options_chain.py
git commit -m "feat(options): chain dataclass + CSP/CC contract pickers + liquidity gate"
```

---

### Task 2.3: Finnhub free-tier client

**Files:**
- Create: `src/trading_bot/intelligence_finnhub.py`
- Create: `tests/test_intelligence_finnhub.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intelligence_finnhub.py
import datetime as dt
from unittest.mock import patch, MagicMock
import pytest
from trading_bot.intelligence_finnhub import FinnhubClient, FinnhubUnavailable


def _resp(json_body, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_body
    m.raise_for_status.return_value = None
    return m


def test_earnings_calendar_returns_normalized_rows():
    body = {"earningsCalendar": [
        {"symbol": "AAPL", "date": "2026-05-02", "epsActual": None, "epsEstimate": 1.5},
        {"symbol": "MSFT", "date": "2026-05-03", "epsActual": None, "epsEstimate": 2.7},
    ]}
    c = FinnhubClient(api_key="k")
    with patch("requests.get", return_value=_resp(body)) as g:
        out = c.earnings_calendar(dt.date(2026, 5, 1), dt.date(2026, 5, 8))
    assert len(out) == 2
    assert out[0].symbol == "AAPL" and out[0].date == dt.date(2026, 5, 2)
    g.assert_called_once()


def test_earnings_calendar_returns_empty_when_no_key():
    c = FinnhubClient(api_key="")
    assert c.earnings_calendar(dt.date(2026, 5, 1), dt.date(2026, 5, 8)) == []


def test_company_profile_caches_and_returns():
    body = {"marketCapitalization": 2500.0, "ipo": "1986-03-13", "exchange": "NASDAQ"}
    c = FinnhubClient(api_key="k")
    with patch("requests.get", return_value=_resp(body)) as g:
        a = c.company_profile("MSFT")
        b = c.company_profile("MSFT")
    assert a == b
    assert a.market_cap_musd == 2500.0
    assert g.call_count == 1  # cache hit on second call


def test_raises_finnhub_unavailable_on_500():
    c = FinnhubClient(api_key="k")
    bad = MagicMock()
    bad.status_code = 503
    bad.raise_for_status.side_effect = Exception("server error")
    with patch("requests.get", return_value=bad):
        with pytest.raises(FinnhubUnavailable):
            c.earnings_calendar(dt.date(2026, 5, 1), dt.date(2026, 5, 8))
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_intelligence_finnhub.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/intelligence_finnhub.py
"""Finnhub free-tier client. Soft-fail (returns empty / raises FinnhubUnavailable)
on errors so the rest of the bot can degrade gracefully."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import requests


_BASE = "https://finnhub.io/api/v1"
_TIMEOUT = 10
_USER_AGENT = "TradingBot/1.0 (paper-trading; bharath8887@gmail.com)"


class FinnhubUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class EarningsRow:
    symbol: str
    date: dt.date
    eps_estimate: float | None


@dataclass(frozen=True)
class CompanyProfile:
    symbol: str
    market_cap_musd: float | None
    ipo_date: dt.date | None
    exchange: str


class FinnhubClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._profile_cache: dict[str, CompanyProfile] = {}

    def _get(self, path: str, params: dict) -> dict:
        if not self.api_key:
            return {}
        params = {**params, "token": self.api_key}
        try:
            r = requests.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT,
                             headers={"User-Agent": _USER_AGENT})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise FinnhubUnavailable(f"finnhub {path}: {e}") from e

    def earnings_calendar(self, start: dt.date, end: dt.date) -> list[EarningsRow]:
        body = self._get("/calendar/earnings",
                         {"from": start.isoformat(), "to": end.isoformat()})
        rows = body.get("earningsCalendar", []) if isinstance(body, dict) else []
        out: list[EarningsRow] = []
        for r in rows:
            try:
                out.append(EarningsRow(
                    symbol=r["symbol"],
                    date=dt.date.fromisoformat(r["date"]),
                    eps_estimate=r.get("epsEstimate"),
                ))
            except (KeyError, ValueError):
                continue
        return out

    def company_profile(self, symbol: str) -> CompanyProfile:
        if symbol in self._profile_cache:
            return self._profile_cache[symbol]
        body = self._get("/stock/profile2", {"symbol": symbol})
        ipo_str = body.get("ipo") if isinstance(body, dict) else None
        ipo_date: dt.date | None = None
        if ipo_str:
            try:
                ipo_date = dt.date.fromisoformat(ipo_str)
            except ValueError:
                pass
        prof = CompanyProfile(
            symbol=symbol,
            market_cap_musd=(body.get("marketCapitalization") if isinstance(body, dict) else None),
            ipo_date=ipo_date,
            exchange=(body.get("exchange") if isinstance(body, dict) else "") or "",
        )
        self._profile_cache[symbol] = prof
        return prof

    def has_earnings_in_window(self, symbol: str, start: dt.date, end: dt.date) -> bool:
        try:
            rows = self.earnings_calendar(start, end)
        except FinnhubUnavailable:
            return True  # conservative: treat as "earnings present" → block CSP
        return any(r.symbol == symbol for r in rows)
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_intelligence_finnhub.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/intelligence_finnhub.py tests/test_intelligence_finnhub.py
git commit -m "feat(intel): Finnhub client — earnings calendar + company profile"
```

---

### Task 2.4: ApeWisdom WSB-mentions client

**Files:**
- Create: `src/trading_bot/intelligence_apewisdom.py`
- Create: `tests/test_intelligence_apewisdom.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intelligence_apewisdom.py
from unittest.mock import patch, MagicMock
from trading_bot.intelligence_apewisdom import ApeWisdomClient


def _resp(body):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = body
    m.raise_for_status.return_value = None
    return m


def test_returns_normalized_mentions():
    body = {"results": [
        {"ticker": "GME", "mentions": 800, "mentions_24h_ago": 200, "rank": 1},
        {"ticker": "AAPL", "mentions": 50, "mentions_24h_ago": 60, "rank": 22},
    ]}
    c = ApeWisdomClient()
    with patch("requests.get", return_value=_resp(body)):
        out = c.wallstreetbets_mentions()
    assert out["GME"].mentions == 800 and out["GME"].rank == 1
    assert out["AAPL"].mentions == 50


def test_is_spike_detects_high_growth():
    body = {"results": [{"ticker": "GME", "mentions": 800, "mentions_24h_ago": 200, "rank": 1}]}
    c = ApeWisdomClient()
    with patch("requests.get", return_value=_resp(body)):
        c.wallstreetbets_mentions()
    assert c.is_spike("GME", multiplier=2.0) is True
    assert c.is_spike("AAPL", multiplier=2.0) is False  # not loaded


def test_returns_empty_on_network_error():
    c = ApeWisdomClient()
    with patch("requests.get", side_effect=Exception("boom")):
        assert c.wallstreetbets_mentions() == {}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_intelligence_apewisdom.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/intelligence_apewisdom.py
"""ApeWisdom WSB / r/stocks mention tracker. No auth, soft-fail to empty dict
so the bot keeps running when the source is down."""
from __future__ import annotations

from dataclasses import dataclass

import requests

_BASE = "https://apewisdom.io/api/v1.0/filter/wallstreetbets"
_TIMEOUT = 10
_USER_AGENT = "TradingBot/1.0 (paper-trading; bharath8887@gmail.com)"


@dataclass(frozen=True)
class MentionRow:
    ticker: str
    rank: int
    mentions: int
    mentions_24h_ago: int


class ApeWisdomClient:
    def __init__(self) -> None:
        self._last: dict[str, MentionRow] = {}

    def wallstreetbets_mentions(self) -> dict[str, MentionRow]:
        try:
            r = requests.get(_BASE, timeout=_TIMEOUT,
                             headers={"User-Agent": _USER_AGENT})
            r.raise_for_status()
            body = r.json()
        except Exception:
            return {}
        out: dict[str, MentionRow] = {}
        for row in (body.get("results") or []):
            try:
                out[row["ticker"]] = MentionRow(
                    ticker=row["ticker"], rank=int(row.get("rank") or 999),
                    mentions=int(row.get("mentions") or 0),
                    mentions_24h_ago=int(row.get("mentions_24h_ago") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._last = out
        return out

    def is_spike(self, symbol: str, *, multiplier: float) -> bool:
        row = self._last.get(symbol)
        if not row or row.mentions_24h_ago <= 0:
            return False
        return row.mentions >= row.mentions_24h_ago * multiplier
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_intelligence_apewisdom.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/intelligence_apewisdom.py tests/test_intelligence_apewisdom.py
git commit -m "feat(intel): ApeWisdom WSB mention spike client"
```

---

### Task 2.5: Alpaca options client wrapper

**Files:**
- Create: `src/trading_bot/options/alpaca_options.py`
- Create: `tests/test_options_alpaca.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_alpaca.py
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.chain import ChainContract


def _settings():
    s = MagicMock()
    s.alpaca_api_key = "k"
    s.alpaca_api_secret = "s"
    s.alpaca_base_url = "https://paper-api.alpaca.markets/v2"
    return s


def test_get_chain_normalizes_snapshot():
    snap_call = MagicMock()
    snap_call.symbol = "AAPL250516C00200000"
    snap_call.latest_quote = MagicMock(bid_price=2.0, ask_price=2.10)
    snap_call.latest_trade = MagicMock(price=2.05)
    snap_call.greeks = MagicMock(delta=0.27, gamma=0.0, theta=-0.04, vega=0.10, rho=0.0)
    snap_call.implied_volatility = 0.30

    feed = MagicMock()
    feed.get_option_chain.return_value = {"AAPL250516C00200000": snap_call}

    with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient",
               return_value=feed):
        with patch("trading_bot.options.alpaca_options.TradingClient"):
            c = OptionAlpacaClient(_settings())
            chain = c.get_chain("AAPL", expiration_gte=dt.date(2026, 5, 1),
                                expiration_lte=dt.date(2026, 5, 30))
    assert len(chain) == 1
    cc = chain[0]
    assert isinstance(cc, ChainContract)
    assert cc.kind == "C" and cc.strike == 200.0
    assert cc.bid == 2.0 and cc.delta == 0.27


def test_submit_csp_sell_to_open_uses_limit_order():
    trading = MagicMock()
    submitted = MagicMock(id="ord-1")
    trading.submit_order.return_value = submitted
    with patch("trading_bot.options.alpaca_options.TradingClient",
               return_value=trading):
        with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient"):
            c = OptionAlpacaClient(_settings())
            order_id = c.sell_to_open(
                contract_symbol="AAPL250516P00190000", qty=1, limit_price=Decimal("2.10"),
            )
    assert order_id == "ord-1"
    trading.submit_order.assert_called_once()


def test_buy_to_close_returns_order_id():
    trading = MagicMock()
    trading.submit_order.return_value = MagicMock(id="ord-2")
    with patch("trading_bot.options.alpaca_options.TradingClient",
               return_value=trading):
        with patch("trading_bot.options.alpaca_options.OptionHistoricalDataClient"):
            c = OptionAlpacaClient(_settings())
            assert c.buy_to_close(
                contract_symbol="AAPL250516P00190000", qty=1, limit_price=Decimal("0.95"),
            ) == "ord-2"


def test_constructor_rejects_live_url():
    s = _settings()
    s.alpaca_base_url = "https://api.alpaca.markets/v2"
    from trading_bot.exceptions import LiveModeDisabled
    with pytest.raises(LiveModeDisabled):
        OptionAlpacaClient(s)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_options_alpaca.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/options/alpaca_options.py
"""OptionAlpacaClient — wraps OptionHistoricalDataClient (chain + Greeks via the
free indicative feed) and the TradingClient for option order submission.
Paper-only: rejects any non-paper base_url at construction."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide, OrderType, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from trading_bot.alpaca_client import PAPER_URL_PREFIX
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled
from trading_bot.options.chain import ChainContract
from trading_bot.options.symbols import parse_occ


class OptionAlpacaClient:
    def __init__(self, settings) -> None:
        if not settings.alpaca_base_url.startswith(PAPER_URL_PREFIX):
            raise LiveModeDisabled()
        self._data = OptionHistoricalDataClient(
            api_key=settings.alpaca_api_key, secret_key=settings.alpaca_api_secret,
        )
        self._trading = TradingClient(
            api_key=settings.alpaca_api_key, secret_key=settings.alpaca_api_secret, paper=True,
        )

    def get_chain(
        self, underlying: str, *,
        expiration_gte: dt.date, expiration_lte: dt.date,
    ) -> list[ChainContract]:
        try:
            req = OptionChainRequest(
                underlying_symbol=underlying,
                expiration_date_gte=expiration_gte,
                expiration_date_lte=expiration_lte,
            )
            snap_map = self._data.get_option_chain(req)
        except Exception as e:
            raise AlpacaClientError(f"get_option_chain {underlying}: {e}") from e

        out: list[ChainContract] = []
        for symbol, snap in (snap_map or {}).items():
            try:
                meta = parse_occ(symbol)
            except ValueError:
                continue
            q = getattr(snap, "latest_quote", None)
            t = getattr(snap, "latest_trade", None)
            g = getattr(snap, "greeks", None)
            iv = getattr(snap, "implied_volatility", None)
            if q is None or g is None or iv is None:
                continue  # incomplete row — skip
            bid = float(getattr(q, "bid_price", 0.0) or 0.0)
            ask = float(getattr(q, "ask_price", 0.0) or 0.0)
            last = float(getattr(t, "price", 0.0) or 0.0)
            delta = float(getattr(g, "delta", 0.0) or 0.0)
            out.append(ChainContract(
                contract_symbol=symbol, underlying=meta.underlying,
                expiration=meta.expiration, kind=meta.kind, strike=meta.strike,
                bid=bid, ask=ask, last=last, volume=int(getattr(t, "size", 0) or 0),
                open_interest=int(getattr(snap, "open_interest", 0) or 0),
                implied_volatility=float(iv), delta=delta,
            ))
        return out

    def sell_to_open(
        self, *, contract_symbol: str, qty: int, limit_price: Decimal,
    ) -> str:
        return self._submit(contract_symbol, qty, limit_price, AlpacaSide.SELL)

    def buy_to_close(
        self, *, contract_symbol: str, qty: int, limit_price: Decimal,
    ) -> str:
        return self._submit(contract_symbol, qty, limit_price, AlpacaSide.BUY)

    def _submit(
        self, contract_symbol: str, qty: int, limit_price: Decimal, side: AlpacaSide,
    ) -> str:
        if qty <= 0:
            raise ValueError("qty must be positive integer")
        try:
            req = LimitOrderRequest(
                symbol=contract_symbol, qty=qty, side=side,
                time_in_force=TimeInForce.DAY, limit_price=float(limit_price),
                type=OrderType.LIMIT,
            )
            order = self._trading.submit_order(req)
            return str(order.id)
        except Exception as e:
            raise AlpacaClientError(f"option order {side} {contract_symbol}: {e}") from e

    def get_option_positions(self) -> list:
        try:
            return [p for p in self._trading.get_all_positions()
                    if str(p.asset_class).lower() == "us_option"]
        except Exception as e:
            raise AlpacaClientError(f"get_option_positions: {e}") from e

    def list_optionable_us_equities(self) -> set[str]:
        from alpaca.trading.requests import GetAssetsRequest
        try:
            assets = self._trading.get_all_assets(
                GetAssetsRequest(asset_class="us_equity", status="active"))
        except Exception as e:
            raise AlpacaClientError(f"list_optionable: {e}") from e
        out: set[str] = set()
        for a in assets:
            if getattr(a, "options_enabled", False) and a.tradable:
                out.add(str(a.symbol))
        return out

    def get_recent_option_orders(self, symbol_or_contract: str, lookback_days: int = 30):
        try:
            req = GetOrdersRequest(
                status="closed",
                after=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days),
                symbols=[symbol_or_contract],
            )
            return self._trading.get_orders(filter=req)
        except Exception as e:
            raise AlpacaClientError(f"get_recent_option_orders: {e}") from e
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_options_alpaca.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/alpaca_options.py tests/test_options_alpaca.py
git commit -m "feat(options): OptionAlpacaClient — chain + sell-to-open + buy-to-close"
```

---

### Task 2.6: IV-rank capture and rank computation

**Files:**
- Create: `src/trading_bot/options/iv_rank.py`
- Create: `tests/test_options_iv_rank.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_iv_rank.py
import datetime as dt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.options.iv_rank import compute_iv_rank, capture_atm_iv_for_symbol
from trading_bot.options.chain import ChainContract
from trading_bot.state_db import Base, OptionIvHistory


def _seed_history(engine, symbol, ivs: list[float]) -> None:
    today = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i, iv in enumerate(ivs):
            s.add(OptionIvHistory(symbol=symbol,
                                  recorded_at=today - dt.timedelta(days=len(ivs) - i),
                                  atm_iv_30d=iv))
        s.commit()


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'iv.db'}")
    Base.metadata.create_all(e)
    return e


def test_iv_rank_high_when_current_above_history(engine):
    _seed_history(engine, "AAPL", [0.20, 0.22, 0.21, 0.23, 0.25])
    rank = compute_iv_rank(engine, "AAPL", current_iv=0.40)
    assert rank == 100.0  # current way above hi=0.25


def test_iv_rank_low_when_current_below_history(engine):
    _seed_history(engine, "AAPL", [0.20, 0.22, 0.21, 0.23, 0.25])
    rank = compute_iv_rank(engine, "AAPL", current_iv=0.10)
    assert rank == 0.0


def test_iv_rank_returns_none_when_history_too_short(engine):
    _seed_history(engine, "AAPL", [0.25, 0.27])  # < 30 entries
    assert compute_iv_rank(engine, "AAPL", current_iv=0.30, min_history=30) is None


def test_capture_atm_iv_picks_30dte_atm():
    today = dt.date(2026, 4, 28)
    chain = [
        ChainContract(contract_symbol="AAPL260530C00200000", underlying="AAPL",
                      expiration=dt.date(2026, 5, 30), kind="C", strike=200,
                      bid=1, ask=1.1, last=1.05, volume=10, open_interest=100,
                      implied_volatility=0.28, delta=0.50),
        ChainContract(contract_symbol="AAPL260530P00200000", underlying="AAPL",
                      expiration=dt.date(2026, 5, 30), kind="P", strike=200,
                      bid=1, ask=1.1, last=1.05, volume=10, open_interest=100,
                      implied_volatility=0.30, delta=-0.50),
        ChainContract(contract_symbol="AAPL260530C00210000", underlying="AAPL",
                      expiration=dt.date(2026, 5, 30), kind="C", strike=210,
                      bid=0.5, ask=0.6, last=0.55, volume=5, open_interest=50,
                      implied_volatility=0.40, delta=0.20),
    ]
    iv = capture_atm_iv_for_symbol(chain, spot=200.0, today=today)
    assert iv == pytest.approx((0.28 + 0.30) / 2, rel=1e-6)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_options_iv_rank.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/options/iv_rank.py
"""ATM 30-day IV capture + IV-rank computation from local history."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.state_db import OptionIvHistory


def capture_atm_iv_for_symbol(
    chain: list[ChainContract], *, spot: float, today: dt.date,
    target_dte: int = 30, dte_window: int = 7,
) -> float | None:
    """Pick ATM call+put pair closest to target_dte, return mean IV."""
    if not chain or spot <= 0:
        return None
    candidates = [c for c in chain
                  if abs((c.expiration - today).days - target_dte) <= dte_window]
    if not candidates:
        return None

    def by_strike_dte(c: ChainContract):
        return (abs(c.strike - spot), abs((c.expiration - today).days - target_dte))

    calls = sorted([c for c in candidates if c.kind == "C"], key=by_strike_dte)
    puts = sorted([c for c in candidates if c.kind == "P"], key=by_strike_dte)
    if not calls or not puts:
        return None
    iv_call = calls[0].implied_volatility
    iv_put = puts[0].implied_volatility
    return (iv_call + iv_put) / 2.0


def compute_iv_rank(
    engine: Engine, symbol: str, *, current_iv: float, min_history: int = 30,
    lookback_days: int = 252,
) -> float | None:
    """Return IV rank in [0, 100] vs trailing `lookback_days` of stored ATM IV.
    Returns None if local history < `min_history` rows."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    with Session(engine) as s:
        rows = s.execute(
            select(OptionIvHistory.atm_iv_30d)
            .where(OptionIvHistory.symbol == symbol,
                   OptionIvHistory.recorded_at >= cutoff)
            .order_by(desc(OptionIvHistory.recorded_at))
        ).scalars().all()
    if len(rows) < min_history:
        return None
    lo, hi = min(rows), max(rows)
    if hi <= lo:
        return 0.0 if current_iv <= lo else 100.0
    rank = (current_iv - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, rank))


def record_iv(engine: Engine, *, symbol: str, atm_iv: float, ts: dt.datetime | None = None) -> None:
    when = ts or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(OptionIvHistory(symbol=symbol, recorded_at=when, atm_iv_30d=atm_iv))
        s.commit()
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_options_iv_rank.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/iv_rank.py tests/test_options_iv_rank.py
git commit -m "feat(options): ATM IV capture + IV-rank computation"
```

---

## Phase 3 — Universe + state machine + risk + lane

### Task 3.1: Wheel universe filter pipeline

**Files:**
- Create: `src/trading_bot/options/wheel_universe.py`
- Create: `strategy/wheel_blocklist.yaml`
- Create: `strategy/wheel_allowlist.yaml`
- Create: `tests/test_wheel_universe.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wheel_universe.py
import datetime as dt
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.options.wheel_universe import filter_universe, UniverseInputs
from trading_bot.config import WheelConfig
from trading_bot.state_db import Base, WheelUniverseCache
from trading_bot.intelligence_finnhub import CompanyProfile


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'u.db'}")
    Base.metadata.create_all(e)
    return e


def _profile(symbol, mcap, ipo):
    return CompanyProfile(symbol=symbol, market_cap_musd=mcap,
                          ipo_date=dt.date.fromisoformat(ipo), exchange="NASDAQ")


def test_passes_when_all_filters_satisfied(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("AAPL", 3_000_000.0, "1980-12-12")
    inputs = UniverseInputs(
        candidates=["AAPL"], optionable_set={"AAPL"},
        avg_dollar_volume_50d={"AAPL": 5_000_000_000.0},
        avg_option_volume_30d={"AAPL": 100_000},
        finnhub=fin, blocklist=set(), allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert out == {"AAPL"}


def test_blocked_by_market_cap(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("XYZ", 5_000.0, "2024-01-01")  # $5M cap
    inputs = UniverseInputs(
        candidates=["XYZ"], optionable_set={"XYZ"},
        avg_dollar_volume_50d={"XYZ": 100_000_000.0},
        avg_option_volume_30d={"XYZ": 50_000},
        finnhub=fin, blocklist=set(), allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert "XYZ" not in out


def test_blocklist_overrides_pass(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("AAPL", 3_000_000.0, "1980-12-12")
    inputs = UniverseInputs(
        candidates=["AAPL"], optionable_set={"AAPL"},
        avg_dollar_volume_50d={"AAPL": 5_000_000_000.0},
        avg_option_volume_30d={"AAPL": 100_000},
        finnhub=fin, blocklist={"AAPL"}, allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert "AAPL" not in out


def test_allowlist_forces_inclusion_even_if_filters_fail(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("ZZZ", 1_000.0, "2025-01-01")
    inputs = UniverseInputs(
        candidates=["ZZZ"], optionable_set={"ZZZ"},
        avg_dollar_volume_50d={"ZZZ": 10_000_000.0},
        avg_option_volume_30d={"ZZZ": 1_000},
        finnhub=fin, blocklist=set(), allowlist={"ZZZ"},
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert out == {"ZZZ"}


def test_cache_hit_skips_recomputation(engine):
    cached_at = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(WheelUniverseCache(symbol="AAPL", eligible=True, reason="", cached_at=cached_at))
        s.add(WheelUniverseCache(symbol="MSFT", eligible=False, reason="market_cap", cached_at=cached_at))
        s.commit()
    fin = MagicMock()  # never called
    inputs = UniverseInputs(
        candidates=["AAPL", "MSFT"], optionable_set={"AAPL", "MSFT"},
        avg_dollar_volume_50d={}, avg_option_volume_30d={},
        finnhub=fin, blocklist=set(), allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28), use_cache=True)
    assert out == {"AAPL"}
    fin.company_profile.assert_not_called()
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_wheel_universe.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement universe filter + create yaml stubs**

```python
# src/trading_bot/options/wheel_universe.py
"""Dynamic wheel universe filter — runs candidates through size / liquidity /
listing-age / blocklist/allowlist filters with a 24h SQLite cache."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.config import WheelConfig
from trading_bot.intelligence_finnhub import FinnhubClient, FinnhubUnavailable
from trading_bot.state_db import WheelUniverseCache


_MIN_MARKET_CAP_MUSD = 10_000.0  # $10B in millions
_MIN_DOLLAR_VOLUME_50D = 50_000_000.0
_MIN_OPTION_VOLUME_30D = 5_000
_MIN_LISTING_YEARS = 3


@dataclass(frozen=True)
class UniverseInputs:
    candidates: list[str]
    optionable_set: set[str]
    avg_dollar_volume_50d: dict[str, float]
    avg_option_volume_30d: dict[str, float]
    finnhub: FinnhubClient
    blocklist: set[str]
    allowlist: set[str]


def _eligibility(
    sym: str, inp: UniverseInputs, today: dt.date,
) -> tuple[bool, str]:
    if sym in inp.blocklist:
        return False, "blocklist"
    if sym not in inp.optionable_set:
        return False, "not_optionable"
    if inp.avg_dollar_volume_50d.get(sym, 0.0) < _MIN_DOLLAR_VOLUME_50D:
        return False, "dollar_volume"
    if inp.avg_option_volume_30d.get(sym, 0) < _MIN_OPTION_VOLUME_30D:
        return False, "option_volume"
    try:
        prof = inp.finnhub.company_profile(sym)
    except FinnhubUnavailable:
        return False, "finnhub_unavailable"
    is_etf = (prof.market_cap_musd is None and prof.exchange.upper() in {"ARCA", "BATS", "NYSE ARCA"})
    if not is_etf and (prof.market_cap_musd or 0.0) < _MIN_MARKET_CAP_MUSD:
        return False, "market_cap"
    if prof.ipo_date is not None:
        years = (today - prof.ipo_date).days / 365.25
        if years < _MIN_LISTING_YEARS:
            return False, "listing_age"
    return True, ""


def filter_universe(
    inp: UniverseInputs, *, cfg: WheelConfig, engine: Engine, today: dt.date,
    use_cache: bool = True,
) -> set[str]:
    eligible: set[str] = set(inp.allowlist)  # forced inclusion
    cache_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cfg.universe_cache_hours)
    cached: dict[str, tuple[bool, dt.datetime]] = {}
    if use_cache:
        with Session(engine) as s:
            for row in s.query(WheelUniverseCache).all():
                cached[row.symbol] = (bool(row.eligible), row.cached_at)

    fresh_rows: list[tuple[str, bool, str]] = []
    for sym in inp.candidates:
        if sym in inp.allowlist:
            continue  # already added
        c = cached.get(sym)
        if c is not None and c[1] >= cache_cutoff:
            if c[0]:
                eligible.add(sym)
            continue
        ok, reason = _eligibility(sym, inp, today)
        if ok:
            eligible.add(sym)
        fresh_rows.append((sym, ok, reason))

    if fresh_rows:
        now = dt.datetime.now(dt.timezone.utc)
        with Session(engine) as s:
            symbols = [r[0] for r in fresh_rows]
            s.execute(delete(WheelUniverseCache)
                      .where(WheelUniverseCache.symbol.in_(symbols)))
            for sym, ok, reason in fresh_rows:
                s.add(WheelUniverseCache(symbol=sym, eligible=ok, reason=reason, cached_at=now))
            s.commit()
    return eligible
```

```yaml
# strategy/wheel_blocklist.yaml
# Symbols never to wheel. One symbol per line under `symbols:`.
symbols: []
```

```yaml
# strategy/wheel_allowlist.yaml
# Symbols to force into the wheel candidate set, bypassing size/age/volume filters.
symbols: []
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_wheel_universe.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/wheel_universe.py strategy/wheel_blocklist.yaml strategy/wheel_allowlist.yaml tests/test_wheel_universe.py
git commit -m "feat(options): wheel universe filter + cache + override yamls"
```

---

### Task 3.2: Wheel state machine

**Files:**
- Create: `src/trading_bot/options/wheel_state.py`
- Create: `tests/test_wheel_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wheel_state.py
import datetime as dt
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.options.wheel_state import (
    WheelStateRepo, Phase, open_csp, mark_assigned, open_cc, close_cycle,
)
from trading_bot.state_db import Base, WheelCycle


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'w.db'}")
    Base.metadata.create_all(e)
    return e


def test_open_csp_creates_cycle(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc is not None
    assert cyc.cycle_id == cid and cyc.phase == Phase.CSP_OPEN.value
    assert cyc.csp_strike == Decimal("190")


def test_mark_assigned_advances_phase_and_records_cost_basis(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    mark_assigned(repo, cycle_id=cid, when=dt.datetime.now(dt.timezone.utc))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc.phase == Phase.ASSIGNED.value
    # cost basis = strike − credit
    assert cyc.cost_basis == Decimal("187.90")


def test_open_cc_after_assignment(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    mark_assigned(repo, cycle_id=cid, when=dt.datetime.now(dt.timezone.utc))
    open_cc(repo, cycle_id=cid, contract="AAPL250620C00195000",
            strike=Decimal("195"), expiration=dt.date(2025, 6, 20),
            credit=Decimal("1.10"))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc.phase == Phase.CC_OPEN.value
    assert cyc.cc_strike == Decimal("195")


def test_close_cycle_finalizes(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    close_cycle(repo, cycle_id=cid, realized_pnl=Decimal("105"))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc is None  # no active cycle


def test_no_two_active_cycles_for_same_symbol(engine):
    repo = WheelStateRepo(engine)
    open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
             strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
             credit=Decimal("2.10"))
    with pytest.raises(ValueError, match="active cycle exists"):
        open_csp(repo, symbol="AAPL", contract="AAPL250620P00185000",
                 strike=Decimal("185"), expiration=dt.date(2025, 6, 20),
                 credit=Decimal("1.80"))
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_wheel_state.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/options/wheel_state.py
"""Wheel cycle state machine. One cycle = one CSP→assigned→CC→closed lifecycle.
Persisted in `wheel_cycles` table."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.state_db import WheelCycle


class Phase(str, Enum):
    CSP_OPEN = "csp_open"
    ASSIGNED = "assigned"
    CC_OPEN = "cc_open"
    CLOSED = "closed"


_ACTIVE_PHASES = {Phase.CSP_OPEN.value, Phase.ASSIGNED.value, Phase.CC_OPEN.value}


class WheelStateRepo:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_active(self, *, symbol: str) -> WheelCycle | None:
        with Session(self.engine) as s:
            return (s.query(WheelCycle)
                    .filter(WheelCycle.symbol == symbol,
                            WheelCycle.phase.in_(_ACTIVE_PHASES))
                    .one_or_none())

    def get_by_cycle_id(self, cycle_id: str) -> WheelCycle | None:
        with Session(self.engine) as s:
            return (s.query(WheelCycle)
                    .filter(WheelCycle.cycle_id == cycle_id)
                    .one_or_none())

    def list_active(self) -> list[WheelCycle]:
        with Session(self.engine) as s:
            return (s.query(WheelCycle)
                    .filter(WheelCycle.phase.in_(_ACTIVE_PHASES))
                    .all())

    def _update(self, cycle_id: str, **fields) -> None:
        with Session(self.engine) as s:
            row = (s.query(WheelCycle).filter(WheelCycle.cycle_id == cycle_id)
                   .one_or_none())
            if row is None:
                raise ValueError(f"unknown cycle_id {cycle_id}")
            for k, v in fields.items():
                setattr(row, k, v)
            s.commit()


def _new_cycle_id() -> str:
    return f"wc_{uuid.uuid4().hex[:12]}"


def open_csp(
    repo: WheelStateRepo, *, symbol: str, contract: str,
    strike: Decimal, expiration: dt.date, credit: Decimal,
) -> str:
    if repo.get_active(symbol=symbol) is not None:
        raise ValueError(f"active cycle exists for {symbol}")
    cid = _new_cycle_id()
    with Session(repo.engine) as s:
        s.add(WheelCycle(
            cycle_id=cid, symbol=symbol, phase=Phase.CSP_OPEN.value,
            opened_at=dt.datetime.now(dt.timezone.utc),
            csp_contract=contract, csp_strike=strike,
            csp_expiration=expiration, csp_credit=credit,
        ))
        s.commit()
    return cid


def mark_assigned(repo: WheelStateRepo, *, cycle_id: str, when: dt.datetime) -> None:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None or cyc.phase != Phase.CSP_OPEN.value:
        raise ValueError(f"cannot assign cycle {cycle_id} (phase={cyc.phase if cyc else None})")
    cost_basis = (cyc.csp_strike or Decimal(0)) - (cyc.csp_credit or Decimal(0))
    repo._update(cycle_id, phase=Phase.ASSIGNED.value, cost_basis=cost_basis)


def open_cc(
    repo: WheelStateRepo, *, cycle_id: str, contract: str,
    strike: Decimal, expiration: dt.date, credit: Decimal,
) -> None:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None or cyc.phase != Phase.ASSIGNED.value:
        raise ValueError(f"cannot open CC for cycle {cycle_id}")
    repo._update(cycle_id, phase=Phase.CC_OPEN.value,
                 cc_contract=contract, cc_strike=strike,
                 cc_expiration=expiration, cc_credit=credit)


def close_cycle(repo: WheelStateRepo, *, cycle_id: str, realized_pnl: Decimal) -> None:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None:
        raise ValueError(f"cannot close unknown cycle {cycle_id}")
    repo._update(cycle_id, phase=Phase.CLOSED.value,
                 closed_at=dt.datetime.now(dt.timezone.utc),
                 realized_pnl=realized_pnl)


def increment_rolls(repo: WheelStateRepo, *, cycle_id: str) -> int:
    cyc = repo.get_by_cycle_id(cycle_id)
    if cyc is None:
        raise ValueError(f"unknown cycle {cycle_id}")
    new_count = (cyc.rolls_used or 0) + 1
    repo._update(cycle_id, rolls_used=new_count)
    return new_count
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_wheel_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/wheel_state.py tests/test_wheel_state.py
git commit -m "feat(options): wheel state machine — CSP→assigned→CC→closed lifecycle"
```

---

### Task 3.3: RiskManager — `option_collateral_ok`

**Files:**
- Modify: `src/trading_bot/risk_manager.py`
- Modify: `tests/test_risk_manager.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_option_collateral_ok_passes_when_under_caps(tmp_path):
    from trading_bot.risk_manager import RiskManager
    from trading_bot.config import RiskConfig, AllocationConfig
    from decimal import Decimal
    rm = RiskManager(
        risk=RiskConfig(daily_loss_limit_pct=2, weekly_loss_limit_pct=5,
                        per_trade_risk_pct=1, max_position_pct=10,
                        max_symbol_concentration_pct=5,
                        max_consecutive_losing_days=3),
        allocation=AllocationConfig(stocks_max_pct=70, crypto_max_pct=30,
                                    options_max_pct=20, cash_floor_pct=10),
    )
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("100000"), prospective_collateral=Decimal("5000"),
        existing_options_value=Decimal("0"), per_symbol_collateral=Decimal("5000"),
    )
    assert ok and reason == ""


def test_option_collateral_ok_blocks_when_options_cap_breached():
    from trading_bot.risk_manager import RiskManager
    from trading_bot.config import RiskConfig, AllocationConfig
    from decimal import Decimal
    rm = RiskManager(
        risk=RiskConfig(daily_loss_limit_pct=2, weekly_loss_limit_pct=5,
                        per_trade_risk_pct=1, max_position_pct=10,
                        max_symbol_concentration_pct=5,
                        max_consecutive_losing_days=3),
        allocation=AllocationConfig(stocks_max_pct=70, crypto_max_pct=30,
                                    options_max_pct=20, cash_floor_pct=10),
    )
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("100000"),
        prospective_collateral=Decimal("3000"),
        existing_options_value=Decimal("18000"),  # already at 18%
        per_symbol_collateral=Decimal("3000"),
    )
    assert ok is False
    assert "options_cap" in reason


def test_option_collateral_ok_blocks_per_symbol_concentration():
    from trading_bot.risk_manager import RiskManager
    from trading_bot.config import RiskConfig, AllocationConfig
    from decimal import Decimal
    rm = RiskManager(
        risk=RiskConfig(daily_loss_limit_pct=2, weekly_loss_limit_pct=5,
                        per_trade_risk_pct=1, max_position_pct=10,
                        max_symbol_concentration_pct=5,
                        max_consecutive_losing_days=3),
        allocation=AllocationConfig(stocks_max_pct=70, crypto_max_pct=30,
                                    options_max_pct=20, cash_floor_pct=10),
    )
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("100000"), prospective_collateral=Decimal("3000"),
        existing_options_value=Decimal("0"),
        per_symbol_collateral=Decimal("6000"),  # 6% > 5%
    )
    assert ok is False
    assert "symbol_concentration" in reason
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_risk_manager.py -k option_collateral -v`
Expected: FAIL — method missing.

- [ ] **Step 3: Add method to RiskManager**

```python
# src/trading_bot/risk_manager.py — append to RiskManager class
def option_collateral_ok(
    self, *,
    equity: "Decimal",
    prospective_collateral: "Decimal",
    existing_options_value: "Decimal",
    per_symbol_collateral: "Decimal",
) -> tuple[bool, str]:
    """Check options-allocation cap + per-symbol concentration.
    Returns (ok, reason). reason="" when ok."""
    if equity <= 0:
        return False, "equity_zero"
    options_pct = (existing_options_value + prospective_collateral) / equity * 100
    if options_pct > self.allocation.options_max_pct:
        return False, f"options_cap ({options_pct:.1f}% > {self.allocation.options_max_pct}%)"
    sym_pct = per_symbol_collateral / equity * 100
    if sym_pct > self.risk.max_symbol_concentration_pct:
        return False, f"symbol_concentration ({sym_pct:.1f}% > {self.risk.max_symbol_concentration_pct}%)"
    return True, ""
```

(If RiskManager doesn't already accept `risk` and `allocation` separately, ensure constructor stores both. Inspect existing signature first; if different, adapt the parameters accordingly — the test above assumes `RiskManager(risk=..., allocation=...)`. If the existing constructor differs, update tests + signature consistently in this same task.)

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_risk_manager.py -k option_collateral -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/risk_manager.py tests/test_risk_manager.py
git commit -m "feat(risk): option_collateral_ok — options cap + per-symbol concentration"
```

---

### Task 3.4: Wheel lane (filter pipeline + candidate emission)

**Files:**
- Create: `src/trading_bot/options/wheel_lane.py`
- Create: `tests/test_wheel_lane.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wheel_lane.py
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock
from trading_bot.options.wheel_lane import WheelLane, WheelDecision, WheelInputs
from trading_bot.options.chain import ChainContract
from trading_bot.config import WheelConfig
from trading_bot.intelligence_apewisdom import MentionRow


def _put(strike, delta, *, dte=35, bid=2.0, oi=200, iv=0.3):
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=bid, ask=bid + 0.05, last=bid, volume=10, open_interest=oi,
        implied_volatility=iv, delta=delta,
    )


def test_wheel_lane_emits_csp_when_all_filters_pass():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    ape = MagicMock(); ape.is_spike.return_value = False
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "open_csp"
    assert out.contract is not None and out.contract.strike == 190


def test_wheel_lane_skips_when_iv_rank_low():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    ape = MagicMock(); ape.is_spike.return_value = False
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=10.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "iv_rank" in out.reason


def test_wheel_lane_skips_when_earnings_present():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = True
    ape = MagicMock(); ape.is_spike.return_value = False
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "earnings" in out.reason


def test_wheel_lane_skips_when_regime_risk_off():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); ape = MagicMock()
    inp = WheelInputs(
        symbol="AAPL", regime="risk_off", vix=35.0, sentiment_score=0.0,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "regime" in out.reason


def test_wheel_lane_skips_when_wsb_spike():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    ape = MagicMock(); ape.is_spike.return_value = True
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "wsb" in out.reason
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_wheel_lane.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/trading_bot/options/wheel_lane.py
"""WheelLane — applies entry filters to a single (symbol, chain) and emits a
WheelDecision: open_csp / open_cc / skip with a reason."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trading_bot.config import WheelConfig
from trading_bot.intelligence_apewisdom import ApeWisdomClient
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.options.chain import (
    ChainContract, pick_csp_contract, pick_cc_contract,
)


@dataclass(frozen=True)
class WheelInputs:
    symbol: str
    regime: str
    vix: float | None
    sentiment_score: float | None
    spot: float
    iv_rank: float | None
    finnhub: FinnhubClient
    apewisdom: ApeWisdomClient
    today: dt.date
    chain: list[ChainContract]
    cycle: object | None  # WheelCycle row when present
    cost_basis: float | None


@dataclass(frozen=True)
class WheelDecision:
    action: str  # "open_csp" | "open_cc" | "skip"
    contract: ChainContract | None
    reason: str


class WheelLane:
    name = "wheel"

    def __init__(self, cfg: WheelConfig) -> None:
        self.cfg = cfg

    def evaluate(self, inp: WheelInputs) -> WheelDecision:
        if not self.cfg.enabled:
            return WheelDecision("skip", None, "wheel_disabled")
        if inp.regime not in ("trending_up", "sideways"):
            return WheelDecision("skip", None, f"regime={inp.regime}")
        if inp.vix is None or not (self.cfg.vix_floor <= inp.vix <= self.cfg.vix_ceiling):
            return WheelDecision("skip", None, f"vix={inp.vix}")
        if inp.sentiment_score is not None and inp.sentiment_score < self.cfg.sentiment_floor:
            return WheelDecision("skip", None, f"sentiment={inp.sentiment_score:.2f}")
        if inp.iv_rank is None or inp.iv_rank < self.cfg.iv_rank_floor:
            return WheelDecision("skip", None, f"iv_rank={inp.iv_rank}")
        if inp.apewisdom.is_spike(inp.symbol, multiplier=self.cfg.wsb_spike_multiplier):
            return WheelDecision("skip", None, "wsb_spike")
        # earnings window = today .. today + dte_max + 2
        end = inp.today + dt.timedelta(days=self.cfg.dte_max + 2)
        if inp.finnhub.has_earnings_in_window(inp.symbol, inp.today, end):
            return WheelDecision("skip", None, "earnings_in_window")

        if inp.cycle is None:
            pick = pick_csp_contract(inp.chain, cfg=self.cfg, today=inp.today)
            if pick is None:
                return WheelDecision("skip", None, "no_csp_contract_in_band")
            return WheelDecision("open_csp", pick, "")
        # cycle in 'assigned' phase ⇒ open CC
        if inp.cost_basis is None:
            return WheelDecision("skip", None, "no_cost_basis")
        pick = pick_cc_contract(inp.chain, cost_basis=inp.cost_basis,
                                cfg=self.cfg, today=inp.today)
        if pick is None:
            return WheelDecision("skip", None, "no_cc_contract_in_band")
        return WheelDecision("open_cc", pick, "")
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_wheel_lane.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/wheel_lane.py tests/test_wheel_lane.py
git commit -m "feat(options): WheelLane — entry filters + decision emission"
```

---

## Phase 4 — Orchestrator + reconciler + scheduler + alerts

### Task 4.1: New alert kinds in `alerts.py`

**Files:**
- Modify: `src/trading_bot/alerts.py`
- Modify: `tests/test_alerts.py`

- [ ] **Step 1: Write the failing test**

```python
def test_wheel_alert_kinds_accepted():
    from trading_bot.alerts import AlertEvent
    import datetime as dt
    e = AlertEvent(
        kind="wheel_csp_opened", severity="info",
        title="t", detail_html="<p/>", fired_at=dt.datetime.now(dt.timezone.utc),
        dedup_key="x",
    )
    assert e.kind == "wheel_csp_opened"
    for k in ("wheel_cc_opened", "wheel_take_profit", "wheel_dte_close",
              "wheel_roll", "wheel_assignment", "wheel_called_away",
              "wheel_allocation_cap", "wheel_chain_fetch_failure"):
        AlertEvent(kind=k, severity="info", title="t", detail_html="<p/>",
                   fired_at=dt.datetime.now(dt.timezone.utc), dedup_key=f"k_{k}")
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_alerts.py::test_wheel_alert_kinds_accepted -v`
Expected: FAIL — kinds not in Literal.

- [ ] **Step 3: Extend `AlertEvent.kind` Literal in `alerts.py`**

```python
# In src/trading_bot/alerts.py, locate the AlertEvent dataclass and extend
# the Literal alias used for `kind`. The exact existing alias name should be
# kept; new values appended:
#   "wheel_csp_opened", "wheel_cc_opened", "wheel_take_profit",
#   "wheel_dte_close", "wheel_roll", "wheel_assignment",
#   "wheel_called_away", "wheel_allocation_cap", "wheel_chain_fetch_failure"
# (Keep the existing values; only add. Read the file first to find the exact
# line and adjust.)
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_alerts.py -v`
Expected: PASS (all alert tests).

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): wheel-related alert kinds (csp/cc opened, roll, assignment, ...)"
```

---

### Task 4.2: Wheel runner — `run_wheel_scan` + `run_wheel_manage`

**Files:**
- Create: `src/trading_bot/options/wheel_runner.py`
- Create: `tests/test_wheel_runner.py`

This is the largest task. The runner ties chain → IV-rank → universe → lane → risk-manager → alpaca-options → state-machine → journal → alerts. Tests use mocks for all external IO.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wheel_runner.py
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_runner import run_wheel_scan, run_wheel_manage, WheelDeps
from trading_bot.options.wheel_lane import WheelDecision
from trading_bot.options.wheel_state import WheelStateRepo
from trading_bot.state_db import Base, OptionFill, WheelCycle


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'r.db'}")
    Base.metadata.create_all(e)
    return e


def _put(strike, delta=-0.25):
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=35)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=2.10, ask=2.20, last=2.15, volume=100, open_interest=400,
        implied_volatility=0.30, delta=delta,
    )


def _deps(engine):
    d = MagicMock(spec=WheelDeps)
    d.engine = engine
    d.option_alpaca = MagicMock()
    d.option_alpaca.get_chain.return_value = [_put(190)]
    d.option_alpaca.sell_to_open.return_value = "ord-csp-1"
    d.option_alpaca.buy_to_close.return_value = "ord-bto-1"
    d.option_alpaca.get_option_positions.return_value = []
    d.alpaca_client = MagicMock()
    acct = MagicMock(); acct.equity = Decimal("100000"); acct.cash = Decimal("50000")
    acct.buying_power = Decimal("100000"); acct.portfolio_value = Decimal("100000")
    d.alpaca_client.get_account.return_value = acct
    d.alpaca_client.get_positions.return_value = []
    d.risk_manager = MagicMock()
    d.risk_manager.option_collateral_ok.return_value = (True, "")
    d.intelligence_macro = MagicMock(); d.intelligence_macro.snapshot.return_value = MagicMock(vix=20.0)
    d.regime_detector = MagicMock(); d.regime_detector.detect.return_value = "trending_up"
    d.universe_filter = MagicMock(return_value={"AAPL"})
    d.iv_rank_for = MagicMock(return_value=55.0)
    d.spot_for = MagicMock(return_value=200.0)
    d.sentiment_for = MagicMock(return_value=0.1)
    d.finnhub = MagicMock(); d.finnhub.has_earnings_in_window.return_value = False
    d.apewisdom = MagicMock(); d.apewisdom.is_spike.return_value = False
    d.alert_queue = MagicMock()
    d.cfg = MagicMock()
    d.cfg.enabled = True
    d.cfg.delta_target_low = 0.20
    d.cfg.delta_target_high = 0.30
    d.cfg.dte_min = 30
    d.cfg.dte_max = 45
    d.cfg.vix_floor = 15
    d.cfg.vix_ceiling = 30
    d.cfg.sentiment_floor = -0.3
    d.cfg.iv_rank_floor = 30
    d.cfg.wsb_spike_multiplier = 2.0
    d.cfg.min_premium_abs = 0.20
    d.cfg.min_open_interest = 100
    d.cfg.take_profit_pct = 0.50
    d.cfg.dte_force_close = 21
    d.cfg.delta_breach_csp = 0.45
    d.cfg.delta_breach_cc = 0.55
    d.cfg.max_rolls_per_cycle = 2
    return d


def test_wheel_scan_opens_csp_and_writes_journal_and_alert(engine):
    d = _deps(engine)
    run_wheel_scan(d)
    d.option_alpaca.sell_to_open.assert_called_once()
    with Session(engine) as s:
        cyc = s.query(WheelCycle).one()
        assert cyc.symbol == "AAPL" and cyc.phase == "csp_open"
        fill = s.query(OptionFill).one()
        assert fill.option_type == "CSP" and fill.side == "SELL"
    assert any("wheel_csp_opened" in str(c) for c in d.alert_queue.mock_calls)


def test_wheel_scan_skips_when_risk_blocks(engine):
    d = _deps(engine)
    d.risk_manager.option_collateral_ok.return_value = (False, "options_cap")
    run_wheel_scan(d)
    d.option_alpaca.sell_to_open.assert_not_called()
    assert any("wheel_allocation_cap" in str(c) for c in d.alert_queue.mock_calls)


def test_wheel_manage_buys_to_close_at_50pct_profit(engine):
    d = _deps(engine)
    repo = WheelStateRepo(engine)
    with Session(engine) as s:
        s.add(WheelCycle(cycle_id="c1", symbol="AAPL", phase="csp_open",
                         opened_at=dt.datetime.now(dt.timezone.utc),
                         csp_contract="AAPL250603P00190000",
                         csp_strike=Decimal("190"),
                         csp_expiration=dt.date(2025, 6, 3),
                         csp_credit=Decimal("2.10")))
        s.commit()
    pos = MagicMock()
    pos.symbol = "AAPL250603P00190000"
    pos.qty = "-1"
    pos.cost_basis = "-210"
    snap = MagicMock()
    snap.contract_symbol = "AAPL250603P00190000"
    snap.bid = 1.00; snap.ask = 1.05  # mid = 1.025 ≤ 50% of 2.10
    snap.delta = -0.20
    snap.expiration = dt.date(2025, 6, 3)
    d.option_alpaca.get_option_positions.return_value = [pos]
    d.option_alpaca.snapshot_for_contract = MagicMock(return_value=snap)
    run_wheel_manage(d)
    d.option_alpaca.buy_to_close.assert_called_once()
    assert any("wheel_take_profit" in str(c) for c in d.alert_queue.mock_calls)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_wheel_runner.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement runner**

```python
# src/trading_bot/options/wheel_runner.py
"""run_wheel_scan + run_wheel_manage — the orchestrator entry points.
The deps-bag pattern makes the runner deterministic and unit-testable."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.alerts import AlertEvent
from trading_bot.config import WheelConfig
from trading_bot.intelligence_apewisdom import ApeWisdomClient
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_lane import WheelInputs, WheelLane
from trading_bot.options.wheel_state import (
    Phase, WheelStateRepo, close_cycle, increment_rolls, mark_assigned,
    open_cc, open_csp,
)
from trading_bot.options.symbols import parse_occ
from trading_bot.state_db import OptionFill, WheelCycle


@dataclass
class WheelDeps:
    cfg: WheelConfig
    engine: Engine
    option_alpaca: OptionAlpacaClient
    alpaca_client: object  # AlpacaClient (equity)
    risk_manager: object
    intelligence_macro: object
    regime_detector: object
    universe_filter: Callable[[], set[str]]
    iv_rank_for: Callable[[str], float | None]
    spot_for: Callable[[str], float | None]
    sentiment_for: Callable[[str], float | None]
    finnhub: FinnhubClient
    apewisdom: ApeWisdomClient
    alert_queue: Callable[[AlertEvent], None]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _today() -> dt.date:
    return _now().date()


def _journal_fill(
    engine: Engine, *, ts: dt.datetime, underlying: str, contract: str,
    option_type: str, side: str, strike: Decimal, expiration: dt.date,
    qty: int, premium: Decimal, alpaca_order_id: str, cycle_id: str | None,
    notes: str = "",
) -> None:
    with Session(engine) as s:
        s.add(OptionFill(
            ts=ts, underlying=underlying, contract_symbol=contract,
            option_type=option_type, side=side, strike=strike,
            expiration=expiration, qty=qty, premium=premium,
            alpaca_order_id=alpaca_order_id, cycle_id=cycle_id, notes=notes,
        ))
        s.commit()


def _emit(deps: WheelDeps, *, kind: str, severity: str, title: str,
          detail_html: str, dedup_key: str) -> None:
    deps.alert_queue(AlertEvent(
        kind=kind, severity=severity, title=title, detail_html=detail_html,
        fired_at=_now(), dedup_key=dedup_key,
    ))


def _existing_options_value(deps: WheelDeps) -> Decimal:
    """Sum of |market_value| across current option positions (rough collateral proxy)."""
    total = Decimal(0)
    try:
        positions = deps.option_alpaca.get_option_positions()
    except Exception:
        return total
    for p in positions:
        try:
            mv = abs(Decimal(str(getattr(p, "market_value", 0))))
            total += mv
        except Exception:
            continue
    return total


def run_wheel_scan(deps: WheelDeps) -> None:
    if not deps.cfg.enabled:
        return
    today = _today()
    regime = deps.regime_detector.detect()
    macro = deps.intelligence_macro.snapshot()
    vix = getattr(macro, "vix", None)
    repo = WheelStateRepo(deps.engine)
    eligible = deps.universe_filter()
    account = deps.alpaca_client.get_account()
    equity = Decimal(str(account.equity))
    existing_opt = _existing_options_value(deps)
    lane = WheelLane(deps.cfg)

    for symbol in sorted(eligible):
        try:
            chain = deps.option_alpaca.get_chain(
                symbol,
                expiration_gte=today + dt.timedelta(days=deps.cfg.dte_min),
                expiration_lte=today + dt.timedelta(days=deps.cfg.dte_max),
            )
        except Exception as e:
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"chain fetch failed: {symbol}",
                  detail_html=f"<p>{e}</p>", dedup_key=f"chain_fail_{symbol}_{today}")
            continue
        cycle = repo.get_active(symbol=symbol)
        # Guard: only the ASSIGNED phase or no-cycle should produce a new
        # entry. csp_open / cc_open are already managed by run_wheel_manage.
        if cycle is not None and cycle.phase != Phase.ASSIGNED.value:
            continue
        cost_basis: float | None = None
        if cycle is not None and cycle.phase == Phase.ASSIGNED.value:
            cost_basis = float(cycle.cost_basis or 0)
        decision = lane.evaluate(WheelInputs(
            symbol=symbol, regime=regime, vix=vix,
            sentiment_score=deps.sentiment_for(symbol),
            spot=(deps.spot_for(symbol) or 0.0),
            iv_rank=deps.iv_rank_for(symbol),
            finnhub=deps.finnhub, apewisdom=deps.apewisdom, today=today,
            chain=chain, cycle=cycle, cost_basis=cost_basis,
        ))
        if decision.action == "skip" or decision.contract is None:
            continue
        contract = decision.contract
        per_symbol_collateral = Decimal(str(contract.strike)) * Decimal(100)
        ok, reason = deps.risk_manager.option_collateral_ok(
            equity=equity, prospective_collateral=per_symbol_collateral,
            existing_options_value=existing_opt,
            per_symbol_collateral=per_symbol_collateral,
        )
        if not ok:
            _emit(deps, kind="wheel_allocation_cap", severity="bad",
                  title=f"wheel skipped {symbol}: {reason}",
                  detail_html=f"<p>{symbol}: {reason}</p>",
                  dedup_key=f"alloc_cap_{symbol}_{today}")
            continue
        limit = Decimal(str(round(contract.bid, 2)))
        try:
            order_id = deps.option_alpaca.sell_to_open(
                contract_symbol=contract.contract_symbol, qty=1, limit_price=limit,
            )
        except Exception as e:
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"sell-to-open failed: {symbol}",
                  detail_html=f"<p>{e}</p>",
                  dedup_key=f"sto_fail_{symbol}_{today}")
            continue
        if decision.action == "open_csp":
            cid = open_csp(repo, symbol=symbol, contract=contract.contract_symbol,
                           strike=Decimal(str(contract.strike)),
                           expiration=contract.expiration, credit=limit)
            otype = "CSP"
        else:
            assert cycle is not None
            open_cc(repo, cycle_id=cycle.cycle_id, contract=contract.contract_symbol,
                    strike=Decimal(str(contract.strike)),
                    expiration=contract.expiration, credit=limit)
            cid = cycle.cycle_id
            otype = "CC"
        _journal_fill(
            deps.engine, ts=_now(), underlying=symbol,
            contract=contract.contract_symbol, option_type=otype, side="SELL",
            strike=Decimal(str(contract.strike)), expiration=contract.expiration,
            qty=1, premium=limit, alpaca_order_id=order_id, cycle_id=cid,
        )
        _emit(deps, kind=("wheel_csp_opened" if otype == "CSP" else "wheel_cc_opened"),
              severity="info",
              title=f"{otype} opened: {symbol} {contract.strike} exp {contract.expiration}",
              detail_html=(f"<p>{symbol} sold {otype} @ {contract.strike} "
                           f"for {limit} (delta {contract.delta:.2f})</p>"),
              dedup_key=f"open_{otype}_{contract.contract_symbol}")


def _dte(expiration: dt.date, today: dt.date) -> int:
    return (expiration - today).days


def run_wheel_manage(deps: WheelDeps) -> None:
    if not deps.cfg.enabled:
        return
    today = _today()
    repo = WheelStateRepo(deps.engine)
    try:
        positions = deps.option_alpaca.get_option_positions()
    except Exception as e:
        _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
              title="get_option_positions failed",
              detail_html=f"<p>{e}</p>", dedup_key=f"pos_fail_{today}")
        return

    pos_by_contract = {str(p.symbol): p for p in positions}

    for cyc in repo.list_active():
        contract_sym = cyc.cc_contract or cyc.csp_contract
        if not contract_sym or contract_sym not in pos_by_contract:
            continue
        is_cc = (cyc.phase == Phase.CC_OPEN.value)
        try:
            snap = deps.option_alpaca.snapshot_for_contract(contract_sym)
        except Exception as e:
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"snapshot failed {contract_sym}",
                  detail_html=f"<p>{e}</p>",
                  dedup_key=f"snap_fail_{contract_sym}_{today}")
            continue
        mid = (snap.bid + snap.ask) / 2.0
        credit = float(cyc.cc_credit if is_cc else cyc.csp_credit or 0)
        exp = parse_occ(contract_sym).expiration
        dte = _dte(exp, today)
        delta_now = abs(snap.delta)
        # Take-profit: mid <= (1 - take_profit_pct) * credit
        take_profit_threshold = credit * (1 - deps.cfg.take_profit_pct)
        if mid <= take_profit_threshold:
            _close_short(deps, cyc, contract_sym, kind="wheel_take_profit",
                         price=Decimal(str(round(mid, 2))))
            continue
        if dte <= deps.cfg.dte_force_close:
            _close_short(deps, cyc, contract_sym, kind="wheel_dte_close",
                         price=Decimal(str(round(mid, 2))))
            continue
        breach = (deps.cfg.delta_breach_cc if is_cc else deps.cfg.delta_breach_csp)
        if delta_now >= breach and (cyc.rolls_used or 0) < deps.cfg.max_rolls_per_cycle:
            _try_roll(deps, cyc, contract_sym, is_cc=is_cc, today=today,
                      current_mid=mid)


def _close_short(deps: WheelDeps, cyc: WheelCycle, contract_sym: str,
                 *, kind: str, price: Decimal) -> None:
    try:
        order_id = deps.option_alpaca.buy_to_close(
            contract_symbol=contract_sym, qty=1, limit_price=price,
        )
    except Exception as e:
        _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
              title=f"buy-to-close failed {contract_sym}",
              detail_html=f"<p>{e}</p>",
              dedup_key=f"btc_fail_{contract_sym}_{_today()}")
        return
    is_cc = (cyc.phase == Phase.CC_OPEN.value)
    meta = parse_occ(contract_sym)
    _journal_fill(
        deps.engine, ts=_now(), underlying=cyc.symbol, contract=contract_sym,
        option_type=("CC" if is_cc else "CSP"), side="BUY",
        strike=Decimal(str(meta.strike)), expiration=meta.expiration, qty=1,
        premium=price, alpaca_order_id=order_id, cycle_id=cyc.cycle_id,
        notes=kind,
    )
    credit = (cyc.cc_credit if is_cc else cyc.csp_credit) or Decimal(0)
    pnl = (credit - price) * Decimal(100)
    if kind in ("wheel_take_profit", "wheel_dte_close"):
        repo = WheelStateRepo(deps.engine)
        if is_cc:
            total_pnl = pnl + (cyc.csp_credit or Decimal(0)) * Decimal(100)
        else:
            total_pnl = pnl
        close_cycle(repo, cycle_id=cyc.cycle_id, realized_pnl=total_pnl)
    _emit(deps, kind=kind, severity="info",
          title=f"{kind} {cyc.symbol} {contract_sym}",
          detail_html=f"<p>closed {contract_sym} for {price}, P&L {pnl}</p>",
          dedup_key=f"{kind}_{contract_sym}")


def _try_roll(
    deps: WheelDeps, cyc: WheelCycle, contract_sym: str, *,
    is_cc: bool, today: dt.date, current_mid: float,
) -> None:
    """Buy-to-close current short and sell-to-open a new one one expiry out
    at the same delta band. Best effort — if no replacement contract found,
    just close (treated as DTE-style close)."""
    try:
        new_chain = deps.option_alpaca.get_chain(
            cyc.symbol,
            expiration_gte=today + dt.timedelta(days=deps.cfg.dte_min),
            expiration_lte=today + dt.timedelta(days=deps.cfg.dte_max),
        )
    except Exception:
        new_chain = []
    if is_cc:
        from trading_bot.options.chain import pick_cc_contract
        pick = pick_cc_contract(new_chain,
                                cost_basis=float(cyc.cost_basis or 0),
                                cfg=deps.cfg, today=today)
    else:
        from trading_bot.options.chain import pick_csp_contract
        pick = pick_csp_contract(new_chain, cfg=deps.cfg, today=today)
    if pick is None:
        # Fall back to a defensive close
        _close_short(deps, cyc, contract_sym, kind="wheel_dte_close",
                     price=Decimal(str(round(current_mid, 2))))
        return
    # Close existing
    try:
        deps.option_alpaca.buy_to_close(contract_symbol=contract_sym, qty=1,
                                        limit_price=Decimal(str(round(current_mid, 2))))
    except Exception:
        return
    # Open new
    new_credit = Decimal(str(round(pick.bid, 2)))
    try:
        order_id = deps.option_alpaca.sell_to_open(
            contract_symbol=pick.contract_symbol, qty=1, limit_price=new_credit,
        )
    except Exception:
        return
    if is_cc:
        from trading_bot.options.wheel_state import open_cc as _open_cc
        _open_cc(WheelStateRepo(deps.engine), cycle_id=cyc.cycle_id,
                 contract=pick.contract_symbol,
                 strike=Decimal(str(pick.strike)),
                 expiration=pick.expiration, credit=new_credit)
    else:
        # Roll a CSP: same cycle stays in csp_open with updated contract
        with Session(deps.engine) as s:
            row = s.query(WheelCycle).filter(WheelCycle.cycle_id == cyc.cycle_id).one()
            row.csp_contract = pick.contract_symbol
            row.csp_strike = Decimal(str(pick.strike))
            row.csp_expiration = pick.expiration
            row.csp_credit = new_credit
            s.commit()
    increment_rolls(WheelStateRepo(deps.engine), cycle_id=cyc.cycle_id)
    _journal_fill(
        deps.engine, ts=_now(), underlying=cyc.symbol,
        contract=pick.contract_symbol, option_type="ROLL", side="SELL",
        strike=Decimal(str(pick.strike)), expiration=pick.expiration, qty=1,
        premium=new_credit, alpaca_order_id=order_id, cycle_id=cyc.cycle_id,
        notes="rolled_from " + contract_sym,
    )
    _emit(deps, kind="wheel_roll", severity="warn",
          title=f"wheel roll {cyc.symbol} {contract_sym} → {pick.contract_symbol}",
          detail_html=f"<p>rolled to delta {pick.delta:.2f}</p>",
          dedup_key=f"roll_{cyc.cycle_id}_{today}")
```

NOTE: `OptionAlpacaClient.snapshot_for_contract` is referenced in the runner. Add it to `alpaca_options.py` in this same task:

```python
# Append to OptionAlpacaClient in src/trading_bot/options/alpaca_options.py
def snapshot_for_contract(self, contract_symbol: str) -> ChainContract:
    """Single-contract snapshot. Returns a ChainContract via the chain endpoint
    filtered to this expiration."""
    meta = parse_occ(contract_symbol)
    chain = self.get_chain(meta.underlying,
                           expiration_gte=meta.expiration,
                           expiration_lte=meta.expiration)
    for c in chain:
        if c.contract_symbol == contract_symbol:
            return c
    raise AlpacaClientError(f"contract not in chain: {contract_symbol}")
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_wheel_runner.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/options/wheel_runner.py src/trading_bot/options/alpaca_options.py tests/test_wheel_runner.py
git commit -m "feat(options): wheel runner — scan + manage + roll + journal + alerts"
```

---

### Task 4.3: Reconciler — extend for option fills

**Files:**
- Modify: `src/trading_bot/reconciler.py`
- Modify: `tests/test_reconciler.py`

- [ ] **Step 1: Read current reconciler structure** (no edits yet)

Run: `grep -n "def reconcile" src/trading_bot/reconciler.py`

The reconciler file already exposes a top-level `reconcile()` function. The new logic adds an option-fills pass after the existing equity pass. Examine the function to ensure the new code is inserted at the right spot — append, do not refactor.

- [ ] **Step 2: Write the failing test (append)**

```python
def test_reconcile_marks_csp_assigned_when_alpaca_position_disappears(tmp_path):
    """When a short-put position disappears from Alpaca and the underlying now shows
    100 long shares, the reconciler advances the cycle to 'assigned' and emits the
    appropriate alert."""
    import datetime as dt
    from decimal import Decimal
    from unittest.mock import MagicMock
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from trading_bot.state_db import Base, OptionFill, WheelCycle
    from trading_bot.reconciler import reconcile_options
    engine = create_engine(f"sqlite:///{tmp_path/'rec.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(OptionFill(ts=dt.datetime.now(dt.timezone.utc), underlying="AAPL",
                         contract_symbol="AAPL250516P00190000", option_type="CSP",
                         side="SELL", strike=Decimal("190"),
                         expiration=dt.date(2025, 5, 16), qty=1,
                         premium=Decimal("2.10"), alpaca_order_id="o1",
                         cycle_id="c1"))
        s.add(WheelCycle(cycle_id="c1", symbol="AAPL", phase="csp_open",
                         opened_at=dt.datetime.now(dt.timezone.utc),
                         csp_contract="AAPL250516P00190000",
                         csp_strike=Decimal("190"), csp_expiration=dt.date(2025, 5, 16),
                         csp_credit=Decimal("2.10")))
        s.commit()
    option_alpaca = MagicMock()
    option_alpaca.get_option_positions.return_value = []  # CSP gone
    alpaca_eq = MagicMock()
    eq_pos = MagicMock(); eq_pos.symbol = "AAPL"; eq_pos.qty = "100"; eq_pos.avg_entry_price = "190"
    alpaca_eq.get_positions.return_value = [eq_pos]
    alert_q = MagicMock()
    reconcile_options(engine=engine, option_alpaca=option_alpaca,
                      alpaca_equity=alpaca_eq, alert_queue=alert_q)
    with Session(engine) as s:
        cyc = s.query(WheelCycle).one()
        assert cyc.phase == "assigned"
    assert any("wheel_assignment" in str(c) for c in alert_q.mock_calls)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_reconciler.py -k reconcile_marks_csp_assigned -v`
Expected: FAIL — `reconcile_options` not defined.

- [ ] **Step 4: Append the new function to reconciler.py**

```python
# src/trading_bot/reconciler.py — append at end of file
import datetime as _dt
from decimal import Decimal as _Decimal

from sqlalchemy import select as _select
from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session

from trading_bot.alerts import AlertEvent as _AlertEvent
from trading_bot.options.wheel_state import (
    Phase as _Phase, WheelStateRepo as _WheelStateRepo,
    close_cycle as _close_cycle, mark_assigned as _mark_assigned,
)
from trading_bot.state_db import OptionFill as _OptionFill, WheelCycle as _WheelCycle


def reconcile_options(
    *, engine: _Engine, option_alpaca, alpaca_equity, alert_queue,
) -> None:
    """For each open wheel cycle whose short option no longer appears in
    Alpaca's option positions, classify the outcome:
      - underlying now shows +100 shares per contract → CSP assigned
      - underlying still flat, near/past expiration → expired worthless or BTC fill
      - CC: underlying drops to 0 shares → called away
    Emits the matching wheel_* alert."""
    repo = _WheelStateRepo(engine)
    open_option_symbols = {str(p.symbol) for p in option_alpaca.get_option_positions()}
    eq_positions = {str(p.symbol): p for p in alpaca_equity.get_positions()}

    for cyc in repo.list_active():
        contract = cyc.cc_contract or cyc.csp_contract
        if contract is None or contract in open_option_symbols:
            continue  # still open

        is_cc = (cyc.phase == _Phase.CC_OPEN.value)
        eq = eq_positions.get(cyc.symbol)
        eq_qty = int(_Decimal(str(getattr(eq, "qty", "0") or "0"))) if eq else 0

        if not is_cc:
            # CSP closed somehow
            if eq_qty >= 100:
                _mark_assigned(repo, cycle_id=cyc.cycle_id,
                               when=_dt.datetime.now(_dt.timezone.utc))
                alert_queue(_AlertEvent(
                    kind="wheel_assignment", severity="warn",
                    title=f"CSP assigned: {cyc.symbol} @ {cyc.csp_strike}",
                    detail_html=f"<p>{cyc.symbol} now holding {eq_qty} shares</p>",
                    fired_at=_dt.datetime.now(_dt.timezone.utc),
                    dedup_key=f"assignment_{cyc.cycle_id}",
                ))
            else:
                # Expired worthless or already bought-to-close
                pnl = (cyc.csp_credit or _Decimal(0)) * _Decimal(100)
                _close_cycle(repo, cycle_id=cyc.cycle_id, realized_pnl=pnl)
        else:
            # CC closed somehow
            if eq_qty == 0:
                # called away
                pnl = ((cyc.csp_credit or _Decimal(0)) + (cyc.cc_credit or _Decimal(0))) \
                      * _Decimal(100) + ((cyc.cc_strike or _Decimal(0))
                                          - (cyc.cost_basis or _Decimal(0))) * _Decimal(100)
                _close_cycle(repo, cycle_id=cyc.cycle_id, realized_pnl=pnl)
                alert_queue(_AlertEvent(
                    kind="wheel_called_away", severity="info",
                    title=f"CC called away: {cyc.symbol} @ {cyc.cc_strike}",
                    detail_html=f"<p>{cyc.symbol} called away — cycle closed</p>",
                    fired_at=_dt.datetime.now(_dt.timezone.utc),
                    dedup_key=f"called_{cyc.cycle_id}",
                ))
            else:
                # CC expired worthless — cycle reverts back to assigned (still hold shares)
                with _Session(engine) as s:
                    row = s.query(_WheelCycle).filter(_WheelCycle.cycle_id == cyc.cycle_id).one()
                    row.phase = _Phase.ASSIGNED.value
                    row.cc_contract = None
                    row.cc_strike = None
                    row.cc_expiration = None
                    row.cc_credit = None
                    s.commit()
```

Also wire `reconcile_options` into the existing top-level `reconcile()` so the nightly job calls it. Find the call-site and add:

```python
# Inside the existing reconcile() — after equity pass, append:
reconcile_options(
    engine=state_engine, option_alpaca=option_alpaca_client,
    alpaca_equity=client, alert_queue=alert_queue,
)
```

(Use the actual variable names found in the existing function. If those globals aren't already passed in, extend `reconcile()`'s signature in the same edit and pass them through from the caller in the daemon.)

- [ ] **Step 5: Run test, verify pass**

Run: `pytest tests/test_reconciler.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): wheel cycle assignment / called-away / expired-worthless detection"
```

---

### Task 4.4: Scheduler jobs — register wheel scan + manage

**Files:**
- Modify: `src/trading_bot/scheduler_jobs.py`
- Modify: `src/trading_bot/cadence.py`
- Modify: `tests/test_scheduler_jobs.py`
- Modify: `tests/test_cadence.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scheduler_jobs.py — append
def test_wheel_scan_and_manage_jobs_registered():
    from trading_bot.scheduler_jobs import register_jobs
    from trading_bot.cadence import CadenceConfig
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler()
    runners = {k: (lambda: None) for k in (
        "heartbeat", "intel_scan", "crypto_scan", "portfolio_watch",
        "verify_stops", "vip_scan", "alerts_drain", "reconcile_post_close",
        "reconcile_pre_digest", "schedule_audit", "wheel_scan", "wheel_manage",
    )}
    cad = CadenceConfig(
        heartbeat_seconds=10, stock_scanner_minutes=60,
        crypto_scanner_minutes=60, portfolio_monitor_minutes=15,
        vip_listener_minutes=15, wheel_scan_enabled=True,
        wheel_manage_interval_minutes=30,
    )
    register_jobs(scheduler=sched, cadence=cad, runners=runners)
    ids = {j.id for j in sched.get_jobs()}
    assert "wheel_scan" in ids and "wheel_manage" in ids
```

```python
# tests/test_cadence.py — append
def test_cadence_wheel_fields_default(tmp_path):
    from trading_bot.cadence import load_cadence
    p = tmp_path / "cad.json"
    p.write_text('{"cadence": {}}')
    c = load_cadence(p)
    assert c.wheel_scan_enabled is True
    assert c.wheel_manage_interval_minutes == 30
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_scheduler_jobs.py tests/test_cadence.py -v`
Expected: failures referencing missing `wheel_scan_enabled` / unregistered jobs.

- [ ] **Step 3: Extend `cadence.py`**

```python
# src/trading_bot/cadence.py — extend CadenceConfig dataclass
# (Add these fields next to existing ones; keep dataclass(frozen=True) if used.)
    wheel_scan_enabled: bool = True
    wheel_manage_interval_minutes: int = 30
```

```python
# src/trading_bot/cadence.py — extend load_cadence parser
# (Inside load_cadence, when reading the JSON dict — add:)
    wheel_scan_enabled=block.get("wheel_scan_enabled", True),
    wheel_manage_interval_minutes=int(block.get("wheel_manage_interval_minutes", 30)),
```

- [ ] **Step 4: Extend `scheduler_jobs.py`**

```python
# src/trading_bot/scheduler_jobs.py — append inside register_jobs() before the
# closing of the function:
    if cadence.wheel_scan_enabled and "wheel_scan" in runners:
        scheduler.add_job(
            runners["wheel_scan"],
            trigger=CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone=et),
            id="wheel_scan", replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )
    if cadence.wheel_scan_enabled and "wheel_manage" in runners:
        interval = cadence.wheel_manage_interval_minutes
        scheduler.add_job(
            runners["wheel_manage"],
            trigger=CronTrigger(
                hour="10-15",
                minute="0,30" if interval == 30 else f"*/{interval}",
                day_of_week="mon-fri", timezone=et,
            ),
            id="wheel_manage", replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )
```

- [ ] **Step 5: Run tests, verify pass**

Run: `pytest tests/test_scheduler_jobs.py tests/test_cadence.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/scheduler_jobs.py src/trading_bot/cadence.py tests/test_scheduler_jobs.py tests/test_cadence.py
git commit -m "feat(scheduler): register wheel_scan @10:15 ET + wheel_manage every 30min"
```

---

## Phase 5 — User-facing surfaces (emails + dashboard + CLI)

### Task 5.1: Email digest — Wheel section

**Files:**
- Modify: `src/trading_bot/email_digest.py`
- Modify: `tests/test_email_digest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_digest.py — append
def test_digest_includes_wheel_section():
    from decimal import Decimal
    from trading_bot.email_digest import build_daily_digest_email, DigestContext
    # Use the existing `DigestContext` constructor — find its required fields
    # by reading the dataclass and supply minimal valid values for the test.
    # The new fields wheel_open_cycles / wheel_pnl_mtd / wheel_collateral_pct
    # must be accepted; the rendered body must include "Wheel".
    ctx = DigestContext(  # type: ignore[call-arg]
        # ... existing required fields ... include:
        wheel_open_cycles=[
            {"symbol": "AAPL", "phase": "csp_open", "strike": "190",
             "expiration": "2026-05-30", "dte": 32, "delta": -0.27,
             "iv": "0.30", "credit": "2.10", "mark": "1.20",
             "pnl": "+90", "trigger_distance": "8 days to 21-DTE"},
        ],
        wheel_pnl_mtd=Decimal("325"),
        wheel_collateral_pct=8.5,
        wheel_win_rate=0.80,
    )  # remaining fields filled by helper / fixture below
    out = build_daily_digest_email(ctx)
    assert "Wheel" in out.html
    assert "AAPL" in out.html
```

Note: you may need a small fixture helper that builds a "minimal valid DigestContext" — check existing test_email_digest_integration.py for the convention used; reuse it. Add only the new wheel fields.

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_email_digest.py -k digest_includes_wheel -v`
Expected: FAIL.

- [ ] **Step 3: Add fields to DigestContext + render section**

```python
# src/trading_bot/email_digest.py — extend DigestContext (frozen dataclass)
# Add these new fields with safe defaults at the end of the dataclass:
    wheel_open_cycles: list[dict] = field(default_factory=list)
    wheel_pnl_mtd: Decimal = Decimal("0")
    wheel_collateral_pct: float = 0.0
    wheel_win_rate: float = 0.0
```

```python
# src/trading_bot/email_digest.py — inside build_daily_digest_email(), insert
# a wheel section after the "Closed Trades (last 7d)" section (find that
# section and add this immediately after):
sections.append(section(
    title="Wheel Cycles",
    glyph="♻",
    body=(
        kpi(
            ("Open cycles", str(len(ctx.wheel_open_cycles))),
            ("Collateral % equity", f"{ctx.wheel_collateral_pct:.1f}%"),
            ("MTD wheel P&L", f"${ctx.wheel_pnl_mtd}"),
            ("Win rate", f"{ctx.wheel_win_rate*100:.0f}%"),
        )
        + data_table(
            headers=["Sym", "Phase", "Strike", "Exp", "DTE", "Δ", "IV",
                     "Credit", "Mark", "P&L", "Trigger"],
            rows=[
                [c["symbol"], c["phase"], c["strike"], c["expiration"],
                 str(c["dte"]), f"{c['delta']:.2f}", c["iv"],
                 c["credit"], c["mark"], c["pnl"], c["trigger_distance"]]
                for c in ctx.wheel_open_cycles
            ],
        )
    ),
))
```

(`kpi(...)` and `section(...)` and `data_table(...)` are existing helpers from `email_shell.py` — verify the exact arity and naming by reading those functions before insertion.)

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_email_digest.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_digest.py tests/test_email_digest.py
git commit -m "feat(email-digest): wheel cycles section with KPIs + table"
```

---

### Task 5.2: Email midday — Wheel watchlist

**Files:**
- Modify: `src/trading_bot/email_midday.py`
- Modify: `tests/test_email_midday.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_midday.py — append
def test_midday_includes_wheel_watchlist():
    from trading_bot.email_midday import build_midday_snapshot_email, SnapshotContext
    ctx = SnapshotContext(  # supply existing required fields via fixture pattern
        wheel_watchlist=[
            {"symbol": "MSFT", "iv_rank": 42.0, "best_csp_delta": -0.25,
             "best_csp_strike": "405", "annualized_yield_pct": "14.0"},
        ],
    )  # rest via existing helper
    out = build_midday_snapshot_email(ctx)
    assert "Wheel watchlist" in out.html
    assert "MSFT" in out.html
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_email_midday.py -k wheel_watchlist -v`
Expected: FAIL.

- [ ] **Step 3: Add field + render**

```python
# src/trading_bot/email_midday.py — extend SnapshotContext
    wheel_watchlist: list[dict] = field(default_factory=list)
```

```python
# inside build_midday_snapshot_email, insert after "Watchlist signals":
body_sections.append(section(
    title="Wheel watchlist",
    glyph="♻",
    body=data_table(
        headers=["Sym", "IV-rank", "Best CSP Δ", "Strike", "Ann. yield"],
        rows=[
            [w["symbol"], f"{w['iv_rank']:.0f}", f"{w['best_csp_delta']:.2f}",
             w["best_csp_strike"], f"{w['annualized_yield_pct']}%"]
            for w in ctx.wheel_watchlist
        ],
    ),
))
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_email_midday.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_midday.py tests/test_email_midday.py
git commit -m "feat(email-midday): wheel watchlist section"
```

---

### Task 5.3: Email fill — option fill paths

**Files:**
- Modify: `src/trading_bot/email_fill.py`
- Modify: `tests/test_email_fill.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_fill.py — append
def test_fill_email_renders_for_option_csp_open():
    from trading_bot.email_fill import build_fill_email, FillContext
    ctx = FillContext(  # supply existing required fields
        fill_type="option_csp_open", symbol="AAPL", contract="AAPL250516P00190000",
        qty=1, premium="2.10", strike="190", expiration="2025-05-16",
        notes="entry",
    )  # rest via existing helper / minimal builder
    out = build_fill_email(ctx)
    assert "AAPL" in out.html and "190" in out.html


def test_fill_email_renders_for_option_assignment():
    from trading_bot.email_fill import build_fill_email, FillContext
    ctx = FillContext(fill_type="option_assignment", symbol="AAPL",
                      contract="AAPL250516P00190000", qty=100,
                      premium="0", strike="190", expiration="2025-05-16",
                      notes="assigned 100 shares @ 190")
    out = build_fill_email(ctx)
    assert "Assigned" in out.html or "assignment" in out.html.lower()
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_email_fill.py -k option -v`
Expected: FAIL.

- [ ] **Step 3: Extend FillContext + builder**

```python
# src/trading_bot/email_fill.py — extend FillContext.fill_type Literal
# Existing values stay; add:
#   "option_csp_open" | "option_csp_close"
#   "option_cc_open"  | "option_cc_close"
#   "option_roll"     | "option_assignment" | "option_called_away"
# Add fields needed for option rendering: contract, strike, expiration
```

```python
# In build_fill_email() add a branch for option fill types — render a small
# table of (symbol, contract, qty, premium, strike, expiration, notes) and
# pick a subject line based on fill_type. Reuse section()/data_table() from
# email_shell. No new images / no new layout — minimal extension.
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_email_fill.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_fill.py tests/test_email_fill.py
git commit -m "feat(email-fill): option fill rendering — open/close/roll/assignment"
```

---

### Task 5.4: Web dashboard — wheel fragment

**Files:**
- Modify: `src/trading_bot/dashboard/data.py`
- Modify: `src/trading_bot/dashboard/app.py`
- Create: `src/trading_bot/dashboard/templates/_wheel.html`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard.py — append
def test_wheel_fragment_route_returns_html():
    from fastapi.testclient import TestClient
    from trading_bot.dashboard.app import app
    client = TestClient(app)
    r = client.get("/fragment/wheel")
    assert r.status_code == 200
    assert "Wheel" in r.text
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_dashboard.py -k wheel_fragment -v`
Expected: FAIL — 404 from route.

- [ ] **Step 3: Add data fields**

```python
# src/trading_bot/dashboard/data.py — extend DashboardSnapshot
# (Add these fields to the existing dataclass with defaults)
    wheel_open_cycles: list[dict] = field(default_factory=list)
    wheel_universe_top: list[dict] = field(default_factory=list)
    wheel_pnl_30d: Decimal = Decimal("0")
    wheel_win_rate: float = 0.0
    wheel_collateral_pct: float = 0.0
```

```python
# src/trading_bot/dashboard/data.py — inside build_snapshot()
# After the existing accumulation, populate the wheel fields:
from trading_bot.options.wheel_state import WheelStateRepo as _WSR
from trading_bot.state_db import OptionIvHistory as _IV, WheelCycle as _WC
import datetime as _dt
_repo = _WSR(state_engine)
snapshot.wheel_open_cycles = [
    {
        "symbol": c.symbol, "phase": c.phase,
        "strike": str(c.cc_strike or c.csp_strike or ""),
        "expiration": str(c.cc_expiration or c.csp_expiration or ""),
        "credit": str(c.cc_credit or c.csp_credit or ""),
    }
    for c in _repo.list_active()
]
# wheel_universe_top: pull most recent 20 rows from option_iv_history per-symbol
# wheel_pnl_30d: sum realized_pnl from wheel_cycles closed in last 30 days
# wheel_collateral_pct: sum strike*100 of open csp/cc / equity
# wheel_win_rate: closed cycles realized_pnl > 0 / closed count over last 60 days
```

- [ ] **Step 4: Add route + template**

```python
# src/trading_bot/dashboard/app.py — extend FRAGMENTS dict + register route
FRAGMENTS["wheel"] = "_wheel.html"

@app.get("/fragment/wheel")
def fragment_wheel():
    snap = build_snapshot()
    return templates.TemplateResponse("_wheel.html", {
        "request": None, "snap": snap,
    })
```

```html
<!-- src/trading_bot/dashboard/templates/_wheel.html -->
<section class="card">
  <h2>♻ Wheel</h2>
  <div class="kpi-row">
    <div class="kpi"><span class="k">Open cycles</span><span class="v">{{ snap.wheel_open_cycles | length }}</span></div>
    <div class="kpi"><span class="k">Collateral</span><span class="v">{{ "%.1f"|format(snap.wheel_collateral_pct) }}%</span></div>
    <div class="kpi"><span class="k">30d P&amp;L</span><span class="v">${{ snap.wheel_pnl_30d }}</span></div>
    <div class="kpi"><span class="k">Win rate</span><span class="v">{{ "%.0f"|format(snap.wheel_win_rate * 100) }}%</span></div>
  </div>
  {% if snap.wheel_open_cycles %}
  <table>
    <thead><tr><th>Symbol</th><th>Phase</th><th>Strike</th><th>Exp</th><th>Credit</th></tr></thead>
    <tbody>
      {% for c in snap.wheel_open_cycles %}
      <tr><td>{{ c.symbol }}</td><td>{{ c.phase }}</td><td>{{ c.strike }}</td><td>{{ c.expiration }}</td><td>{{ c.credit }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p>No open wheel cycles.</p>
  {% endif %}
</section>
```

- [ ] **Step 5: Run test, verify pass**

Run: `pytest tests/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/dashboard/data.py src/trading_bot/dashboard/app.py src/trading_bot/dashboard/templates/_wheel.html tests/test_dashboard.py
git commit -m "feat(dashboard): wheel fragment — KPIs + open cycles table"
```

---

### Task 5.5: CLI — wheel-scan / wheel-manage / wheel-status / wheel-close

**Files:**
- Modify: `src/trading_bot/cli.py`
- Modify: `src/trading_bot/daemon.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py — append
def test_cli_wheel_status_prints_open_cycles(capsys, monkeypatch, tmp_path):
    """Smoke-test wheel-status on an empty DB — prints "No open wheel cycles"."""
    from trading_bot import cli
    monkeypatch.setenv("STATE_DB_URL", f"sqlite:///{tmp_path/'cli.db'}")
    rc = cli.main(["wheel-status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "wheel" in out.lower()
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_cli.py -k wheel -v`
Expected: FAIL — subcommand missing.

- [ ] **Step 3: Add subcommands to cli.py**

```python
# src/trading_bot/cli.py — extend the argparse subparsers
def _build_parser():
    # ... existing ...
    sub.add_parser("wheel-scan", help="Run a one-shot wheel scan")
    sub.add_parser("wheel-manage", help="Run a one-shot wheel manage pass")
    sub.add_parser("wheel-status", help="Print open wheel cycles")
    cl = sub.add_parser("wheel-close", help="Emergency close a wheel position")
    cl.add_argument("symbol")
    return parser
```

```python
# src/trading_bot/cli.py — extend main() dispatch
elif args.command == "wheel-scan":
    from trading_bot.options.wheel_runner import run_wheel_scan
    run_wheel_scan(_build_wheel_deps())
    return 0
elif args.command == "wheel-manage":
    from trading_bot.options.wheel_runner import run_wheel_manage
    run_wheel_manage(_build_wheel_deps())
    return 0
elif args.command == "wheel-status":
    _print_wheel_status()
    return 0
elif args.command == "wheel-close":
    _emergency_close(args.symbol)
    return 0
```

```python
# src/trading_bot/cli.py — helpers
def _build_wheel_deps():
    """Constructs a WheelDeps from existing settings/clients. Mirror the
    daemon.py wiring."""
    # Use existing get_settings(), get_engine(), AlpacaClient, etc.
    # Returns trading_bot.options.wheel_runner.WheelDeps.
    ...

def _print_wheel_status():
    from trading_bot.options.wheel_state import WheelStateRepo
    from trading_bot.state_db import get_engine
    repo = WheelStateRepo(get_engine())
    rows = repo.list_active()
    if not rows:
        print("No open wheel cycles.")
        return
    for c in rows:
        print(f"{c.symbol:6s}  phase={c.phase:10s}  contract={c.cc_contract or c.csp_contract}")

def _emergency_close(symbol: str) -> None:
    from trading_bot.options.wheel_runner import _close_short
    from trading_bot.options.wheel_state import WheelStateRepo
    from trading_bot.state_db import get_engine
    deps = _build_wheel_deps()
    repo = WheelStateRepo(deps.engine)
    cyc = repo.get_active(symbol=symbol)
    if cyc is None:
        print(f"No active wheel cycle for {symbol}")
        return
    contract = cyc.cc_contract or cyc.csp_contract
    snap = deps.option_alpaca.snapshot_for_contract(contract)
    mid = (snap.bid + snap.ask) / 2
    _close_short(deps, cyc, contract, kind="wheel_dte_close",
                 price=Decimal(str(round(mid, 2))))
    print(f"Closed {contract} at ~{mid:.2f}")
```

```python
# src/trading_bot/daemon.py — wire wheel runners into the runners dict that
# is passed to register_jobs(). Locate the runners dict construction and add:
runners["wheel_scan"] = lambda: run_wheel_scan(wheel_deps)
runners["wheel_manage"] = lambda: run_wheel_manage(wheel_deps)
# where wheel_deps is built once at startup, mirroring _build_wheel_deps().
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/cli.py src/trading_bot/daemon.py tests/test_cli.py
git commit -m "feat(cli): wheel-scan / wheel-manage / wheel-status / wheel-close subcommands"
```

---

### Task 5.6: Evolution — wheel-aware analysis

**Files:**
- Modify: `src/trading_bot/evolution.py`
- Modify: `tests/test_evolution.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_evolution.py — append
def test_evolution_reports_wheel_cycle_metrics(tmp_path):
    """When `wheel_cycles` has closed rows, evolution.report() includes
    wheel-specific KPIs (count, win rate, avg P&L per cycle)."""
    import datetime as dt
    from decimal import Decimal
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from trading_bot.state_db import Base, WheelCycle
    from trading_bot.evolution import report_wheel_kpis
    e = create_engine(f"sqlite:///{tmp_path/'ev.db'}")
    Base.metadata.create_all(e)
    with Session(e) as s:
        for i in range(4):
            s.add(WheelCycle(cycle_id=f"c{i}", symbol="AAPL", phase="closed",
                             opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10),
                             closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
                             realized_pnl=Decimal("100" if i < 3 else "-50")))
        s.commit()
    kpis = report_wheel_kpis(e, lookback_days=30)
    assert kpis["count"] == 4
    assert kpis["win_rate"] == 0.75
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_evolution.py -k wheel -v`
Expected: FAIL.

- [ ] **Step 3: Add `report_wheel_kpis`**

```python
# src/trading_bot/evolution.py — append
import datetime as _dt
from decimal import Decimal as _Decimal
from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session
from trading_bot.state_db import WheelCycle as _WC


def report_wheel_kpis(engine: _Engine, *, lookback_days: int = 30) -> dict:
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=lookback_days)
    with _Session(engine) as s:
        rows = (s.query(_WC)
                .filter(_WC.phase == "closed", _WC.closed_at >= cutoff)
                .all())
    count = len(rows)
    if count == 0:
        return {"count": 0, "win_rate": 0.0, "avg_pnl": _Decimal(0), "total_pnl": _Decimal(0)}
    wins = sum(1 for r in rows if (r.realized_pnl or _Decimal(0)) > 0)
    total = sum((r.realized_pnl or _Decimal(0)) for r in rows)
    return {
        "count": count,
        "win_rate": wins / count,
        "avg_pnl": total / count,
        "total_pnl": total,
    }
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_evolution.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/evolution.py tests/test_evolution.py
git commit -m "feat(evolution): report_wheel_kpis — count / win-rate / avg P&L per closed cycle"
```

---

## Phase 6 — Final integration & smoke

### Task 6.1: Daemon wiring + end-to-end smoke

**Files:**
- Modify: `src/trading_bot/daemon.py`
- Create: `tests/test_integration_wheel.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_integration_wheel.py
"""End-to-end smoke: build WheelDeps, run scan + manage on an in-memory engine
with mocked external IO, verify cycle lifecycle through assignment."""
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.state_db import Base, OptionFill, WheelCycle
from trading_bot.options.wheel_runner import run_wheel_scan, run_wheel_manage, WheelDeps
from trading_bot.options.chain import ChainContract


def _put(strike, delta=-0.25, dte=35):
    today = dt.date.today()
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=2.10, ask=2.20, last=2.15, volume=100, open_interest=400,
        implied_volatility=0.30, delta=delta,
    )


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'i.db'}")
    Base.metadata.create_all(e)
    return e


def test_scan_then_assignment_then_cc_then_called_away(engine):
    """Cycle: open CSP → reconciler marks assigned → run_wheel_scan opens CC →
    reconciler marks called_away → cycle closed."""
    from trading_bot.options.wheel_state import WheelStateRepo, mark_assigned
    deps = MagicMock(spec=WheelDeps)
    deps.engine = engine
    deps.cfg = MagicMock(enabled=True, dte_min=30, dte_max=45,
                         delta_target_low=0.20, delta_target_high=0.30,
                         vix_floor=15, vix_ceiling=30, sentiment_floor=-0.3,
                         iv_rank_floor=30, wsb_spike_multiplier=2.0,
                         min_premium_abs=0.20, min_open_interest=100,
                         take_profit_pct=0.50, dte_force_close=21,
                         delta_breach_csp=0.45, delta_breach_cc=0.55,
                         max_rolls_per_cycle=2)
    deps.option_alpaca = MagicMock()
    deps.option_alpaca.get_chain.return_value = [_put(190)]
    deps.option_alpaca.sell_to_open.return_value = "ord-1"
    deps.option_alpaca.get_option_positions.return_value = []
    deps.alpaca_client = MagicMock()
    acct = MagicMock(equity=Decimal("100000"))
    deps.alpaca_client.get_account.return_value = acct
    deps.risk_manager = MagicMock()
    deps.risk_manager.option_collateral_ok.return_value = (True, "")
    deps.intelligence_macro = MagicMock()
    deps.intelligence_macro.snapshot.return_value = MagicMock(vix=20.0)
    deps.regime_detector = MagicMock()
    deps.regime_detector.detect.return_value = "trending_up"
    deps.universe_filter = MagicMock(return_value={"AAPL"})
    deps.iv_rank_for = MagicMock(return_value=55.0)
    deps.spot_for = MagicMock(return_value=200.0)
    deps.sentiment_for = MagicMock(return_value=0.1)
    deps.finnhub = MagicMock(); deps.finnhub.has_earnings_in_window.return_value = False
    deps.apewisdom = MagicMock(); deps.apewisdom.is_spike.return_value = False
    deps.alert_queue = MagicMock()

    # 1) Open CSP
    run_wheel_scan(deps)
    repo = WheelStateRepo(engine)
    cyc = repo.get_active(symbol="AAPL")
    assert cyc is not None and cyc.phase == "csp_open"

    # 2) Simulate assignment
    mark_assigned(repo, cycle_id=cyc.cycle_id, when=dt.datetime.now(dt.timezone.utc))

    # 3) Open CC — chain returns calls now
    today = dt.date.today()
    cc = ChainContract(
        contract_symbol=f"AAPL{(today+dt.timedelta(days=35)):%y%m%d}C00200000",
        underlying="AAPL", expiration=today + dt.timedelta(days=35),
        kind="C", strike=200, bid=1.10, ask=1.20, last=1.15, volume=50,
        open_interest=300, implied_volatility=0.28, delta=0.27,
    )
    deps.option_alpaca.get_chain.return_value = [cc]
    deps.option_alpaca.sell_to_open.return_value = "ord-cc-1"
    run_wheel_scan(deps)
    cyc = repo.get_active(symbol="AAPL")
    assert cyc.phase == "cc_open"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_integration_wheel.py -v`
Expected: FAIL on first run only if any wiring is missing.

- [ ] **Step 3: Wire the deps factory in daemon.py**

```python
# src/trading_bot/daemon.py — locate the place where runners is built and
# the scheduler is started. Add:
from trading_bot.intelligence_apewisdom import ApeWisdomClient
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.iv_rank import compute_iv_rank
from trading_bot.options.wheel_runner import (
    run_wheel_scan, run_wheel_manage, WheelDeps,
)
from trading_bot.options.wheel_universe import filter_universe, UniverseInputs


def _build_wheel_deps(settings, app_cfg, state_engine, alpaca_client, risk_manager,
                      intelligence_macro, regime_detector, queue_alert) -> WheelDeps:
    finnhub = FinnhubClient(api_key=settings.finnhub_api_key)
    ape = ApeWisdomClient()
    opt = OptionAlpacaClient(settings)

    def _universe() -> set[str]:
        candidates = list(opt.list_optionable_us_equities())
        return filter_universe(
            UniverseInputs(
                candidates=candidates,
                optionable_set=set(candidates),
                avg_dollar_volume_50d={},  # populated by screener integration
                avg_option_volume_30d={},
                finnhub=finnhub, blocklist=set(), allowlist=set(),
            ),
            cfg=app_cfg.wheel, engine=state_engine,
            today=dt.date.today(),
        )

    def _iv_rank_for(symbol: str) -> float | None:
        return compute_iv_rank(state_engine, symbol, current_iv=0.0, min_history=30)

    return WheelDeps(
        cfg=app_cfg.wheel, engine=state_engine, option_alpaca=opt,
        alpaca_client=alpaca_client, risk_manager=risk_manager,
        intelligence_macro=intelligence_macro, regime_detector=regime_detector,
        universe_filter=_universe, iv_rank_for=_iv_rank_for,
        spot_for=lambda s: None, sentiment_for=lambda s: None,
        finnhub=finnhub, apewisdom=ape, alert_queue=queue_alert,
    )
```

(`spot_for` and `sentiment_for` start as no-ops that wire to `market_data.py` and `news_sentiment.py` in the same edit — read those modules to find the existing accessors used by the equity orchestrator and reuse them. Do not invent new functions.)

- [ ] **Step 4: Run integration test, verify pass**

Run: `pytest tests/test_integration_wheel.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/daemon.py tests/test_integration_wheel.py
git commit -m "feat(daemon): wire WheelDeps + integration smoke for cycle lifecycle"
```

---

### Task 6.2: Run the full test suite + lint

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -x --tb=short`
Expected: all PASS. If any unrelated tests broke, fix them in this task.

- [ ] **Step 2: Run any existing linters / type checks**

Run: `python -m ruff check src/trading_bot tests`
Run: `python -m mypy src/trading_bot 2>&1 | grep -v "no module" | head -50`
Expected: no new errors introduced (preexisting errors are out of scope).

- [ ] **Step 3: Manual smoke (optional, requires keys)**

Run: `bot wheel-scan` (with `wheel.enabled: false` set in config — verifies wiring without trading)
Run: `bot wheel-status`
Expected: clean exit, no traceback.

- [ ] **Step 4: Commit any small fixes from test-suite run**

```bash
git add -A
git commit -m "chore: lint + test fixes from full-suite run after wheel feature"
```

---

## Final Checklist

- [ ] All 6 phases complete, all tasks committed
- [ ] `wheel.enabled: false` left in `strategy/config.yaml` — operator flips to true after manual smoke
- [ ] `FINNHUB_API_KEY` documented (add to `.env.example` if it exists)
- [ ] Spec referenced in plan ↔ plan referenced in spec is symmetric

When ready to flip live: set `wheel.enabled: true` and `wheel_scan_enabled: true` (the latter via cadence config), then redeploy.
