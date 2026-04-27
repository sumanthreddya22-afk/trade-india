# Phase 1 — Operational Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Claude-session-dependent task execution with a self-hosted launchd daemon + supervisor that runs the existing strategies reliably 24/7, sends timely emails, recovers from stalls, and pauses on drawdown breach.

**Architecture:** Two new long-running Python processes (`daemon` and `supervisor`) managed by launchd. The daemon runs APScheduler in-process, calling existing CLI commands as scheduled jobs. The supervisor runs an independent verification loop that detects stalls (via heartbeat staleness), drawdown breaches (via direct Alpaca account queries), and writes a `pause.flag` sentinel that the daemon reads before placing orders. Structured JSON logging replaces ad-hoc print statements; email pipeline is split into per-trade fill, daily digest, and critical alert templates.

**Tech Stack:** Python 3.11, APScheduler 3.10+, SQLAlchemy 2.0, Alembic, Jinja2, launchd plists, existing alpaca-py + Polygon clients.

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md)

---

## File structure for Phase 1

### New files

```
src/trading_bot/
  daemon.py                    # daemon entrypoint (python -m trading_bot.daemon)
  supervisor.py                # supervisor entrypoint
  state_db.py                  # SQLAlchemy ORM for state.db; engine + session factory
  state_heartbeat.py           # heartbeat write/read
  state_pause.py               # pause.flag read/write
  state_hwm.py                 # equity high-water mark + drawdown calc
  cadence.py                   # cadence config reader (reads paper_active.json's cadence: block)
  log_structured.py            # JSON-structured logger writing to runs/<date>/<role>/
  email_fill.py                # per-trade fill email builder
  email_digest.py              # daily digest email builder
  email_critical.py            # critical alert email builder
  watchdog_stall.py            # heartbeat staleness detector
  watchdog_account.py          # Account Sentinel: drawdown + reconciliation
  scheduler_jobs.py            # APScheduler job registration

ops/
  launchd/
    com.bharath.trading.daemon.paper.plist
    com.bharath.trading.supervisor.plist
  install.sh                   # installs both plists to ~/Library/LaunchAgents/

migrations/
  alembic.ini
  env.py
  versions/
    001_initial_schema.py      # creates state.db tables

tests/
  test_state_heartbeat.py
  test_state_pause.py
  test_state_hwm.py
  test_state_db.py
  test_cadence.py
  test_log_structured.py
  test_email_fill.py
  test_email_digest.py
  test_email_critical.py
  test_watchdog_stall.py
  test_watchdog_account.py
  test_scheduler_jobs.py
  test_integration_daemon.py
  test_integration_supervisor.py
  test_integration_drawdown.py
```

### Files modified

- `pyproject.toml` — add APScheduler, alembic dependencies
- `src/trading_bot/cli.py` — add `daemon` and `supervisor` subcommands that delegate to entrypoints
- `paper_active.json` (new file at repo root or `data/paper_active.json`) — initial config with `cadence:` block

### Files NOT modified in Phase 1

- All strategy code (`strategy.py`, `strategy_lanes.py`)
- Risk manager, orchestrator, market data
- Existing email_sender.py SMTP transport (will be wrapped, not replaced)
- Dashboard

---

## Task 1: Add dependencies and Alembic skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `migrations/alembic.ini`
- Create: `migrations/env.py`

- [ ] **Step 1: Add deps to pyproject.toml**

Edit `pyproject.toml` to add the dependencies. In the `[project]` `dependencies` array, add:

```toml
"apscheduler>=3.10.4",
"alembic>=1.13.0",
```

- [ ] **Step 2: Run uv lock**

```bash
uv lock
```

Expected: lockfile updates without errors.

- [ ] **Step 3: Create Alembic config**

Write `migrations/alembic.ini`:

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///data/state.db
file_template = %%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s
timezone = UTC

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 4: Create env.py for Alembic**

Write `migrations/env.py`:

```python
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from trading_bot.state_db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create empty `migrations/versions/` directory.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock migrations/
git commit -m "feat(plan-8): add APScheduler + Alembic deps and migration skeleton"
```

---

## Task 2: state.db schema with SQLAlchemy ORM

**Files:**
- Create: `src/trading_bot/state_db.py`
- Test: `tests/test_state_db.py`

- [ ] **Step 1: Write failing test for schema**

Write `tests/test_state_db.py`:

```python
import os
import tempfile
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import (
    Base,
    Heartbeat,
    EquityHighWaterMark,
    RoleRun,
    RoleKpi,
    RegimeHistory,
    ConfigHistory,
)


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    yield engine
    os.unlink(path)


def test_heartbeat_roundtrip(db):
    with Session(db) as s:
        hb = Heartbeat(
            ts=dt.datetime.now(dt.timezone.utc),
            pid=1234,
            version="2026-04-27-v1",
            last_action="intel-scan",
        )
        s.add(hb)
        s.commit()
        rows = s.query(Heartbeat).all()
    assert len(rows) == 1
    assert rows[0].pid == 1234
    assert rows[0].version == "2026-04-27-v1"


def test_equity_high_water_mark_roundtrip(db):
    with Session(db) as s:
        hwm = EquityHighWaterMark(
            account="paper",
            equity=100500.42,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add(hwm)
        s.commit()
        rows = s.query(EquityHighWaterMark).all()
    assert len(rows) == 1
    assert rows[0].equity == pytest.approx(100500.42)


def test_role_run_with_kpi(db):
    with Session(db) as s:
        run = RoleRun(
            role_name="stock_scanner",
            started_at=dt.datetime.now(dt.timezone.utc),
            finished_at=dt.datetime.now(dt.timezone.utc),
            status="ok",
            latency_ms=1234,
        )
        s.add(run)
        s.flush()
        kpi = RoleKpi(
            role_name="stock_scanner",
            kpi_name="buy_win_rate_30d",
            value=0.62,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add(kpi)
        s.commit()
    with Session(db) as s:
        assert s.query(RoleRun).count() == 1
        assert s.query(RoleKpi).count() == 1


def test_regime_history_roundtrip(db):
    with Session(db) as s:
        r = RegimeHistory(
            regime="trending_up",
            vix=18.4,
            spy_breadth=0.61,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add(r)
        s.commit()
    with Session(db) as s:
        rows = s.query(RegimeHistory).all()
        assert rows[0].regime == "trending_up"


def test_config_history_roundtrip(db):
    with Session(db) as s:
        c = ConfigHistory(
            account="paper",
            version="v17",
            git_sha="abc1234",
            promoted_at=dt.datetime.now(dt.timezone.utc),
            promoted_by="auto-promote",
            payload_json='{"params": {}}',
        )
        s.add(c)
        s.commit()
    with Session(db) as s:
        assert s.query(ConfigHistory).first().version == "v17"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_state_db.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.state_db'`.

- [ ] **Step 3: Implement state_db.py**

Write `src/trading_bot/state_db.py`:

```python
"""state.db ORM models. Shared coordination surface for daemon, lab, supervisor.
WAL mode is enabled at engine creation in get_engine() so concurrent reads are safe.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class Heartbeat(Base):
    __tablename__ = "heartbeats"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    pid = Column(Integer, nullable=False)
    version = Column(String(64), nullable=False)
    last_action = Column(String(128), nullable=True)


class EquityHighWaterMark(Base):
    __tablename__ = "equity_high_water_mark"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(16), nullable=False, index=True)  # "paper" | "live"
    equity = Column(Float, nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class RoleRun(Base):
    __tablename__ = "role_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(64), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=False)  # ok | error | blocked | halted
    latency_ms = Column(Integer, nullable=True)
    error_text = Column(Text, nullable=True)


class RoleKpi(Base):
    __tablename__ = "role_kpis"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(64), nullable=False, index=True)
    kpi_name = Column(String(64), nullable=False)
    value = Column(Float, nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class RegimeHistory(Base):
    __tablename__ = "regime_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    regime = Column(String(32), nullable=False)
    vix = Column(Float, nullable=True)
    spy_breadth = Column(Float, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class ConfigHistory(Base):
    __tablename__ = "config_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(16), nullable=False)
    version = Column(String(64), nullable=False)
    git_sha = Column(String(64), nullable=True)
    promoted_at = Column(DateTime(timezone=True), nullable=False)
    promoted_by = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)


def get_engine(db_path: str | Path = "data/state.db"):
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def session_for(db_path: str | Path = "data/state.db") -> Session:
    return Session(get_engine(db_path))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_state_db.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Generate Alembic initial migration**

```bash
mkdir -p data
alembic -c migrations/alembic.ini revision --autogenerate -m "initial schema"
mv migrations/versions/*_initial_schema.py migrations/versions/001_initial_schema.py
```

Verify the file exists and contains `op.create_table(...)` for each of the 6 tables.

- [ ] **Step 6: Apply migration locally and confirm**

```bash
alembic -c migrations/alembic.ini upgrade head
sqlite3 data/state.db ".schema" | head -30
```

Expected: schema printout shows all 6 tables.

- [ ] **Step 7: Commit**

```bash
git add src/trading_bot/state_db.py tests/test_state_db.py migrations/versions/001_initial_schema.py
git commit -m "feat(plan-8): state.db ORM schema + Alembic migration"
```

---

## Task 3: Heartbeat module

**Files:**
- Create: `src/trading_bot/state_heartbeat.py`
- Test: `tests/test_state_heartbeat.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_state_heartbeat.py`:

```python
import json
import os
import tempfile
import datetime as dt
from pathlib import Path

import pytest

from trading_bot.state_heartbeat import write_heartbeat, read_heartbeat, is_stale


@pytest.fixture
def hb_path(tmp_path):
    return tmp_path / "heartbeat.json"


def test_write_heartbeat_creates_file_with_required_fields(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="intel-scan")
    payload = json.loads(hb_path.read_text())
    assert "ts" in payload
    assert payload["pid"] == os.getpid()
    assert payload["version"] == "v1"
    assert payload["last_action"] == "intel-scan"


def test_read_heartbeat_returns_dict(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    data = read_heartbeat(hb_path)
    assert data["version"] == "v1"


def test_is_stale_false_when_just_written(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    assert is_stale(hb_path, max_age_seconds=300) is False


def test_is_stale_true_when_file_old(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    old = dt.datetime.now().timestamp() - 600
    os.utime(hb_path, (old, old))
    assert is_stale(hb_path, max_age_seconds=300) is True


def test_is_stale_true_when_file_missing(hb_path):
    assert is_stale(hb_path, max_age_seconds=300) is True


def test_atomic_write_via_tmp_rename(hb_path, monkeypatch):
    """Verify the heartbeat is written via tmp+rename so a reader never sees half-written file."""
    write_heartbeat(hb_path, version="v1", last_action="boot")
    # The writer should never leave a .tmp file behind
    assert not hb_path.with_suffix(".json.tmp").exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_state_heartbeat.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement heartbeat module**

Write `src/trading_bot/state_heartbeat.py`:

```python
"""Heartbeat write/read. The daemon writes every 60s; the supervisor reads
mtime to detect stalls. Atomic via tmp+rename so a reader never observes
a half-written file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


def write_heartbeat(path: str | Path, *, version: str, last_action: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": os.getpid(),
        "version": version,
        "last_action": last_action,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def read_heartbeat(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def is_stale(path: str | Path, *, max_age_seconds: int) -> bool:
    p = Path(path)
    if not p.exists():
        return True
    age = dt.datetime.now().timestamp() - p.stat().st_mtime
    return age > max_age_seconds
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_state_heartbeat.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/state_heartbeat.py tests/test_state_heartbeat.py
git commit -m "feat(plan-8): heartbeat write/read with atomic tmp+rename"
```

---

## Task 4: Pause flag module

**Files:**
- Create: `src/trading_bot/state_pause.py`
- Test: `tests/test_state_pause.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_state_pause.py`:

```python
import pytest
from trading_bot.state_pause import is_paused, set_pause, clear_pause


@pytest.fixture
def flag_path(tmp_path):
    return tmp_path / "pause.flag"


def test_is_paused_false_when_no_flag(flag_path):
    assert is_paused(flag_path) is False


def test_set_pause_creates_flag_with_reason(flag_path):
    set_pause(flag_path, reason="drawdown breach 21.4%")
    assert is_paused(flag_path) is True
    assert "drawdown breach 21.4%" in flag_path.read_text()


def test_clear_pause_removes_flag(flag_path):
    set_pause(flag_path, reason="test")
    clear_pause(flag_path)
    assert is_paused(flag_path) is False


def test_clear_pause_idempotent(flag_path):
    clear_pause(flag_path)  # already absent — should not raise
    clear_pause(flag_path)
    assert is_paused(flag_path) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_state_pause.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement pause module**

Write `src/trading_bot/state_pause.py`:

```python
"""Pause flag sentinel. If file exists, daemon must not place new orders."""
from __future__ import annotations

import datetime as dt
from pathlib import Path


def is_paused(path: str | Path) -> bool:
    return Path(path).exists()


def set_pause(path: str | Path, *, reason: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{reason}\n"
    p.write_text(payload)


def clear_pause(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_state_pause.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/state_pause.py tests/test_state_pause.py
git commit -m "feat(plan-8): pause.flag sentinel with set/clear/check"
```

---

## Task 5: Equity high-water mark module

**Files:**
- Create: `src/trading_bot/state_hwm.py`
- Test: `tests/test_state_hwm.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_state_hwm.py`:

```python
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base, EquityHighWaterMark
from trading_bot.state_hwm import update_hwm, current_hwm, drawdown_pct


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    yield engine
    os.unlink(path)


def test_current_hwm_none_when_empty(db):
    with Session(db) as s:
        assert current_hwm(s, account="paper") is None


def test_update_hwm_writes_and_returns(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
        assert current_hwm(s, account="paper") == pytest.approx(100_000.0)


def test_update_hwm_only_advances(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
        update_hwm(s, account="paper", equity=99_000.0)  # below — should not advance
        assert current_hwm(s, account="paper") == pytest.approx(100_000.0)
        update_hwm(s, account="paper", equity=101_000.0)
        assert current_hwm(s, account="paper") == pytest.approx(101_000.0)


def test_drawdown_pct(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
    with Session(db) as s:
        assert drawdown_pct(s, account="paper", current_equity=80_000.0) == pytest.approx(20.0)
        assert drawdown_pct(s, account="paper", current_equity=100_000.0) == pytest.approx(0.0)
        assert drawdown_pct(s, account="paper", current_equity=110_000.0) == pytest.approx(0.0)


def test_drawdown_pct_no_hwm_returns_zero(db):
    with Session(db) as s:
        # No HWM written yet — no drawdown can be computed
        assert drawdown_pct(s, account="paper", current_equity=80_000.0) == 0.0


def test_accounts_isolated(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
        update_hwm(s, account="live", equity=5_000.0)
    with Session(db) as s:
        assert current_hwm(s, account="paper") == pytest.approx(100_000.0)
        assert current_hwm(s, account="live") == pytest.approx(5_000.0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_state_hwm.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement HWM module**

Write `src/trading_bot/state_hwm.py`:

```python
"""Equity high-water mark tracker. The HWM only advances; it never decreases.
Drawdown is computed as (HWM - current) / HWM, expressed as a positive percentage.
Returns 0.0 when current >= HWM or when no HWM has been recorded yet.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.state_db import EquityHighWaterMark


def current_hwm(session: Session, *, account: str) -> float | None:
    row = (
        session.query(EquityHighWaterMark)
        .filter_by(account=account)
        .order_by(desc(EquityHighWaterMark.equity))
        .first()
    )
    return row.equity if row else None


def update_hwm(session: Session, *, account: str, equity: float) -> None:
    """Record a new HWM only if equity exceeds the current HWM. No-op otherwise."""
    existing = current_hwm(session, account=account)
    if existing is not None and equity <= existing:
        return
    session.add(
        EquityHighWaterMark(
            account=account,
            equity=equity,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.commit()


def drawdown_pct(session: Session, *, account: str, current_equity: float) -> float:
    hwm = current_hwm(session, account=account)
    if hwm is None or current_equity >= hwm:
        return 0.0
    return (hwm - current_equity) / hwm * 100.0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_state_hwm.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/state_hwm.py tests/test_state_hwm.py
git commit -m "feat(plan-8): equity HWM tracker with drawdown calc"
```

---

## Task 6: Cadence config reader

**Files:**
- Create: `src/trading_bot/cadence.py`
- Test: `tests/test_cadence.py`
- Create: `data/paper_active.json` (initial config)

- [ ] **Step 1: Write failing test**

Write `tests/test_cadence.py`:

```python
import json
import pytest
from trading_bot.cadence import CadenceConfig, load_cadence


def test_load_cadence_returns_dataclass_with_defaults(tmp_path):
    cfg_path = tmp_path / "paper_active.json"
    cfg_path.write_text(json.dumps({
        "version": "v1",
        "active_template": "momentum_v3",
        "params": {},
        "risk_caps": {"max_position_pct": 10, "daily_loss_pct": 3, "max_drawdown_pct": 20},
        "cadence": {
            "heartbeat_seconds": 60,
            "watchdog_seconds": 60,
            "account_sentinel_minutes_market": 5,
            "account_sentinel_minutes_offhours": 30,
            "schedule_auditor_minutes": 15,
            "resource_guardian_minutes": 30,
            "stock_scanner_minutes": 60,
            "crypto_scanner_minutes": 30,
            "portfolio_monitor_minutes": 60,
            "order_steward_sweep_minutes": 60,
            "vip_listener_minutes": 30,
            "sentiment_warm_times_et": ["08:55", "12:00"],
            "sentiment_stale_hours_for_on_demand": 4,
        },
    }))
    c = load_cadence(cfg_path)
    assert isinstance(c, CadenceConfig)
    assert c.heartbeat_seconds == 60
    assert c.crypto_scanner_minutes == 30
    assert c.sentiment_warm_times_et == ["08:55", "12:00"]


def test_load_cadence_missing_block_uses_defaults(tmp_path):
    cfg_path = tmp_path / "paper_active.json"
    cfg_path.write_text(json.dumps({
        "version": "v1", "active_template": "momentum_v3", "params": {},
        "risk_caps": {"max_position_pct": 10, "daily_loss_pct": 3, "max_drawdown_pct": 20},
        # No cadence block
    }))
    c = load_cadence(cfg_path)
    # Defaults match the spec §9
    assert c.heartbeat_seconds == 60
    assert c.stock_scanner_minutes == 60


def test_cadence_config_is_frozen():
    c = CadenceConfig()
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        c.heartbeat_seconds = 120
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_cadence.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement cadence module**

Write `src/trading_bot/cadence.py`:

```python
"""Reads the cadence: block from paper_active.json (or live_active.json).
Defaults match the values in spec §9. Frozen dataclass so callers can't
mutate it accidentally.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CadenceConfig:
    heartbeat_seconds: int = 60
    watchdog_seconds: int = 60
    account_sentinel_minutes_market: int = 5
    account_sentinel_minutes_offhours: int = 30
    schedule_auditor_minutes: int = 15
    resource_guardian_minutes: int = 30
    stock_scanner_minutes: int = 60
    crypto_scanner_minutes: int = 30
    portfolio_monitor_minutes: int = 60
    order_steward_sweep_minutes: int = 60
    vip_listener_minutes: int = 30
    sentiment_warm_times_et: tuple[str, ...] = ("08:55", "12:00")
    sentiment_stale_hours_for_on_demand: int = 4


def load_cadence(path: str | Path) -> CadenceConfig:
    payload = json.loads(Path(path).read_text())
    block = payload.get("cadence", {})
    times = block.get("sentiment_warm_times_et")
    return CadenceConfig(
        heartbeat_seconds=block.get("heartbeat_seconds", 60),
        watchdog_seconds=block.get("watchdog_seconds", 60),
        account_sentinel_minutes_market=block.get("account_sentinel_minutes_market", 5),
        account_sentinel_minutes_offhours=block.get("account_sentinel_minutes_offhours", 30),
        schedule_auditor_minutes=block.get("schedule_auditor_minutes", 15),
        resource_guardian_minutes=block.get("resource_guardian_minutes", 30),
        stock_scanner_minutes=block.get("stock_scanner_minutes", 60),
        crypto_scanner_minutes=block.get("crypto_scanner_minutes", 30),
        portfolio_monitor_minutes=block.get("portfolio_monitor_minutes", 60),
        order_steward_sweep_minutes=block.get("order_steward_sweep_minutes", 60),
        vip_listener_minutes=block.get("vip_listener_minutes", 30),
        sentiment_warm_times_et=tuple(times) if times else ("08:55", "12:00"),
        sentiment_stale_hours_for_on_demand=block.get("sentiment_stale_hours_for_on_demand", 4),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cadence.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Create initial paper_active.json**

Write `data/paper_active.json`:

```json
{
  "version": "2026-04-27-phase1-v1",
  "git_sha": "phase1-bootstrap",
  "promoted_at": "2026-04-27T00:00:00Z",
  "promoted_by": "manual-bootstrap",
  "active_template": "momentum_v3",
  "template_path": "trading_bot.strategy",
  "params": {
    "rsi_low": 55,
    "rsi_high": 70,
    "ema_period": 20,
    "stop_pct": 5.0,
    "sentiment_floor": -0.5
  },
  "fitness_at_promotion": null,
  "risk_caps": {
    "max_position_pct": 10,
    "daily_loss_pct": 3,
    "max_drawdown_pct": 20
  },
  "universe": {
    "stocks_filter": "stage1_top100",
    "crypto_pairs": ["BTC/USD", "ETH/USD", "SOL/USD"]
  },
  "fallback_when_no_alpha": "hold_spy",
  "cadence": {
    "heartbeat_seconds": 60,
    "watchdog_seconds": 60,
    "account_sentinel_minutes_market": 5,
    "account_sentinel_minutes_offhours": 30,
    "schedule_auditor_minutes": 15,
    "resource_guardian_minutes": 30,
    "stock_scanner_minutes": 60,
    "crypto_scanner_minutes": 30,
    "portfolio_monitor_minutes": 60,
    "order_steward_sweep_minutes": 60,
    "vip_listener_minutes": 30,
    "sentiment_warm_times_et": ["08:55", "12:00"],
    "sentiment_stale_hours_for_on_demand": 4
  }
}
```

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/cadence.py tests/test_cadence.py data/paper_active.json
git commit -m "feat(plan-8): cadence config reader + initial paper_active.json"
```

---

## Task 7: Structured JSON logger

**Files:**
- Create: `src/trading_bot/log_structured.py`
- Test: `tests/test_log_structured.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_log_structured.py`:

```python
import json
import datetime as dt
from pathlib import Path

import pytest

from trading_bot.log_structured import StructuredLogger, get_run_path


def test_get_run_path_format(tmp_path):
    p = get_run_path(
        base=tmp_path,
        date=dt.date(2026, 4, 28),
        role="stock_scanner",
        ts=dt.datetime(2026, 4, 28, 10, 0, 14, tzinfo=dt.timezone.utc),
    )
    assert p == tmp_path / "2026-04-28" / "stock_scanner" / "10-00-14.json"


def test_logger_writes_json_event(tmp_path):
    log = StructuredLogger(base=tmp_path, role="stock_scanner")
    log.event("scan_start", symbols=25, regime="trending_up")
    log.event("scan_finish", placed=1, vetoed=2)

    files = list((tmp_path / dt.date.today().isoformat() / "stock_scanner").glob("*.json"))
    assert len(files) == 2
    payload = json.loads(files[0].read_text())
    assert "ts" in payload
    assert payload["role"] == "stock_scanner"
    # First file written is the first event call
    assert payload["event"] in {"scan_start", "scan_finish"}


def test_logger_event_includes_arbitrary_kwargs(tmp_path):
    log = StructuredLogger(base=tmp_path, role="stock_scanner")
    log.event("decision", symbol="AAPL", action="buy", conviction=0.82)
    files = sorted((tmp_path / dt.date.today().isoformat() / "stock_scanner").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["symbol"] == "AAPL"
    assert payload["action"] == "buy"
    assert payload["conviction"] == 0.82


def test_logger_event_handles_exception(tmp_path):
    log = StructuredLogger(base=tmp_path, role="stock_scanner")
    try:
        raise ValueError("boom")
    except ValueError as e:
        log.error("scan_failed", error=e)
    files = list((tmp_path / dt.date.today().isoformat() / "stock_scanner").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["event"] == "scan_failed"
    assert payload["error_type"] == "ValueError"
    assert "boom" in payload["error_message"]
    assert "Traceback" in payload["traceback"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_log_structured.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement structured logger**

Write `src/trading_bot/log_structured.py`:

```python
"""Per-role-run JSON logging. One file per .event() / .error() call,
under runs/<YYYY-MM-DD>/<role>/<HH-MM-SS>.json. Multiple events at the
same wall-clock second get suffixed with a microsecond fragment.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import traceback
from pathlib import Path


_lock = threading.Lock()


def get_run_path(*, base: Path, date: dt.date, role: str, ts: dt.datetime) -> Path:
    fname = ts.strftime("%H-%M-%S") + ".json"
    return Path(base) / date.isoformat() / role / fname


class StructuredLogger:
    def __init__(self, *, base: str | Path = "runs", role: str):
        self.base = Path(base)
        self.role = role

    def _write(self, payload: dict) -> None:
        ts = dt.datetime.now(dt.timezone.utc)
        path = get_run_path(base=self.base, date=ts.date(), role=self.role, ts=ts)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve same-second collisions by appending microseconds.
        with _lock:
            target = path
            if target.exists():
                target = target.with_suffix(f".{ts.microsecond}.json")
            target.write_text(json.dumps(payload))

        # Also echo to stdout for launchd capture.
        print(json.dumps(payload), file=sys.stdout, flush=True)

    def event(self, name: str, **kwargs) -> None:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "role": self.role,
            "event": name,
            "level": "info",
            **kwargs,
        }
        self._write(payload)

    def error(self, name: str, *, error: Exception, **kwargs) -> None:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "role": self.role,
            "event": name,
            "level": "error",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc(),
            **kwargs,
        }
        self._write(payload)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_log_structured.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/log_structured.py tests/test_log_structured.py
git commit -m "feat(plan-8): structured JSON logger with per-role run files"
```

---

## Task 8: Per-trade fill email builder

**Files:**
- Create: `src/trading_bot/email_fill.py`
- Test: `tests/test_email_fill.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_email_fill.py`:

```python
from decimal import Decimal
from trading_bot.email_fill import build_fill_email, FillContext


def test_fill_email_subject():
    ctx = FillContext(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        fill_price=Decimal("190.24"), expected_price=Decimal("190.20"),
        strategy="momentum_v3", stop_price=Decimal("180.69"),
        account_equity=Decimal("103950.00"),
    )
    email = build_fill_email(ctx)
    assert "BUY AAPL" in email.subject
    assert "190.24" in email.subject


def test_fill_email_body_contains_all_fields():
    ctx = FillContext(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        fill_price=Decimal("190.24"), expected_price=Decimal("190.20"),
        strategy="momentum_v3", stop_price=Decimal("180.69"),
        account_equity=Decimal("103950.00"),
    )
    email = build_fill_email(ctx)
    body = email.html_body
    assert "AAPL" in body
    assert "41" in body
    assert "190.24" in body
    assert "180.69" in body
    assert "momentum_v3" in body
    assert "103,950" in body or "103950" in body


def test_fill_email_includes_slippage():
    ctx = FillContext(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        fill_price=Decimal("190.24"), expected_price=Decimal("190.20"),
        strategy="momentum_v3", stop_price=Decimal("180.69"),
        account_equity=Decimal("103950.00"),
    )
    email = build_fill_email(ctx)
    # Slippage = +$0.04 (worse for buyer; positive number)
    assert "0.04" in email.html_body


def test_stop_hit_subject_and_loss_amount():
    ctx = FillContext(
        side="STOP", symbol="BTC/USD", qty=Decimal("0.0935"),
        fill_price=Decimal("84920.00"), expected_price=Decimal("89440.00"),
        strategy="momentum_v3", stop_price=None,
        account_equity=Decimal("103527.38"), realized_pnl=Decimal("-422.62"),
    )
    email = build_fill_email(ctx)
    assert "STOP HIT" in email.subject
    assert "BTC/USD" in email.subject
    assert "-$422.62" in email.html_body or "-422.62" in email.html_body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_email_fill.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement fill email builder**

Write `src/trading_bot/email_fill.py`:

```python
"""Per-trade fill email builder. Phase 1 version: symbol, qty, fill price,
slippage, strategy, stop, account equity. Phase 3 will enrich with
leaderboard rank + conviction once those exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class FillContext:
    side: str  # "BUY" | "SELL" | "STOP"
    symbol: str
    qty: Decimal
    fill_price: Decimal
    expected_price: Decimal
    strategy: str
    stop_price: Optional[Decimal]
    account_equity: Decimal
    realized_pnl: Optional[Decimal] = None


@dataclass
class Email:
    subject: str
    html_body: str


def _fmt_money(x: Decimal) -> str:
    return f"${x:,.2f}"


def build_fill_email(ctx: FillContext) -> Email:
    slippage = ctx.fill_price - ctx.expected_price
    if ctx.side == "STOP":
        subject = f"STOP HIT {ctx.symbol} {ctx.qty} @ {_fmt_money(ctx.fill_price)}"
    else:
        subject = f"{ctx.side} {ctx.symbol} {ctx.qty} @ {_fmt_money(ctx.fill_price)}"

    body_lines = [
        f"<h2>{subject}</h2>",
        f"<table>",
        f"<tr><td>Symbol</td><td><b>{ctx.symbol}</b></td></tr>",
        f"<tr><td>Qty</td><td>{ctx.qty}</td></tr>",
        f"<tr><td>Fill price</td><td>{_fmt_money(ctx.fill_price)}</td></tr>",
        f"<tr><td>Expected price</td><td>{_fmt_money(ctx.expected_price)}</td></tr>",
        f"<tr><td>Slippage</td><td>{_fmt_money(slippage)}</td></tr>",
        f"<tr><td>Strategy</td><td>{ctx.strategy}</td></tr>",
    ]
    if ctx.stop_price is not None:
        body_lines.append(f"<tr><td>Stop</td><td>{_fmt_money(ctx.stop_price)}</td></tr>")
    if ctx.realized_pnl is not None:
        sign = "-" if ctx.realized_pnl < 0 else ""
        body_lines.append(
            f"<tr><td>Realized P&amp;L</td><td>{sign}{_fmt_money(abs(ctx.realized_pnl))}</td></tr>"
        )
    body_lines.append(
        f"<tr><td>Account equity</td><td>{_fmt_money(ctx.account_equity)}</td></tr>"
    )
    body_lines.append("</table>")

    return Email(subject=subject, html_body="\n".join(body_lines))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_email_fill.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_fill.py tests/test_email_fill.py
git commit -m "feat(plan-8): per-trade fill email builder"
```

---

## Task 9: Daily digest email builder

**Files:**
- Create: `src/trading_bot/email_digest.py`
- Test: `tests/test_email_digest.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_email_digest.py`:

```python
import datetime as dt
from decimal import Decimal
from trading_bot.email_digest import build_digest_email, DigestContext, TradeRow


def test_digest_subject_with_pnl_and_equity():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500.00"),
        ending_equity=Decimal("103895.00"),
        realized_pnl=Decimal("-422.62"),
        unrealized_pnl=Decimal("139.72"),
        regime="trending_up",
        active_config_version="v17",
        trades=[],
        errors=[],
    )
    email = build_digest_email(ctx)
    assert "Apr 28" in email.subject
    assert "-0.58%" in email.subject or "-0.6%" in email.subject


def test_digest_body_includes_trades():
    trade = TradeRow(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        price=Decimal("190.24"), strategy="momentum_v3",
        time=dt.time(10, 0), status="open",
    )
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500.00"),
        ending_equity=Decimal("103895.00"),
        realized_pnl=Decimal("-422.62"),
        unrealized_pnl=Decimal("139.72"),
        regime="trending_up",
        active_config_version="v17",
        trades=[trade],
        errors=[],
    )
    email = build_digest_email(ctx)
    assert "AAPL" in email.html_body
    assert "190.24" in email.html_body


def test_digest_body_zero_trades():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("100000"),
        ending_equity=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="sideways",
        active_config_version="v17",
        trades=[],
        errors=[],
    )
    email = build_digest_email(ctx)
    assert "no trades" in email.html_body.lower() or "0 trades" in email.html_body.lower()


def test_digest_body_includes_errors():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("100000"),
        ending_equity=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="trending_up",
        active_config_version="v17",
        trades=[],
        errors=["14:23 — Polygon API timeout, auto-restarted"],
    )
    email = build_digest_email(ctx)
    assert "Polygon API timeout" in email.html_body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_email_digest.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement digest email builder**

Write `src/trading_bot/email_digest.py`:

```python
"""Daily digest email builder. Sent at 18:00 ET Mon-Fri by Reporter role.
Phase 1 version. Phase 2 will add role report cards; Phase 3 adds
leaderboard summary.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from trading_bot.email_fill import Email, _fmt_money


@dataclass
class TradeRow:
    side: str
    symbol: str
    qty: Decimal
    price: Decimal
    strategy: str
    time: dt.time
    status: str  # "open" | "closed" | "stopped"


@dataclass
class DigestContext:
    date: dt.date
    starting_equity: Decimal
    ending_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    regime: str
    active_config_version: str
    trades: list[TradeRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def build_digest_email(ctx: DigestContext) -> Email:
    pct = ((ctx.ending_equity - ctx.starting_equity) / ctx.starting_equity) * 100
    sign = "+" if pct >= 0 else ""
    subject = (
        f"Daily Digest | {ctx.date.strftime('%b %d')} | "
        f"{sign}{pct:.2f}% | {_fmt_money(ctx.ending_equity)}"
    )

    body = [f"<h2>{subject}</h2>"]

    body.append(f"<p><b>Regime:</b> {ctx.regime}<br>")
    body.append(f"<b>Active config:</b> {ctx.active_config_version}<br>")
    body.append(
        f"<b>Equity:</b> {_fmt_money(ctx.starting_equity)} &rarr; "
        f"{_fmt_money(ctx.ending_equity)} ({sign}{pct:.2f}%)<br>"
    )
    body.append(f"<b>Realized:</b> {_fmt_money(ctx.realized_pnl)}<br>")
    body.append(f"<b>Unrealized:</b> {_fmt_money(ctx.unrealized_pnl)}</p>")

    if ctx.trades:
        body.append("<h3>Today's trades</h3><table>")
        body.append(
            "<tr><th>Time</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th>"
            "<th>Strategy</th><th>Status</th></tr>"
        )
        for t in ctx.trades:
            body.append(
                f"<tr><td>{t.time.strftime('%H:%M')}</td><td>{t.side}</td>"
                f"<td>{t.symbol}</td><td>{t.qty}</td><td>{_fmt_money(t.price)}</td>"
                f"<td>{t.strategy}</td><td>{t.status}</td></tr>"
            )
        body.append("</table>")
    else:
        body.append("<p><i>No trades today (0 trades placed).</i></p>")

    if ctx.errors:
        body.append("<h3>Errors today</h3><ul>")
        for err in ctx.errors:
            body.append(f"<li>{err}</li>")
        body.append("</ul>")

    return Email(subject=subject, html_body="\n".join(body))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_email_digest.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_digest.py tests/test_email_digest.py
git commit -m "feat(plan-8): daily digest email builder"
```

---

## Task 10: Critical alert email builder

**Files:**
- Create: `src/trading_bot/email_critical.py`
- Test: `tests/test_email_critical.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_email_critical.py`:

```python
from trading_bot.email_critical import build_critical_email


def test_critical_subject_has_prefix():
    email = build_critical_email(
        title="Daemon stalled",
        detail="No heartbeat in 5 min",
    )
    assert email.subject.startswith("[CRITICAL]")
    assert "Daemon stalled" in email.subject


def test_critical_body_includes_detail():
    email = build_critical_email(
        title="Drawdown breach",
        detail="20.4% from HWM $104,500. Pause flag written. Trading halted.",
    )
    assert "20.4%" in email.html_body
    assert "Pause flag written" in email.html_body


def test_critical_severity_high_marker():
    email = build_critical_email(title="X", detail="Y", severity="HIGH")
    assert "HIGH" in email.html_body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_email_critical.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement critical email builder**

Write `src/trading_bot/email_critical.py`:

```python
"""Critical alert email builder. Used by Supervisor on drawdown breach,
daemon stall, account corruption, etc.
"""
from __future__ import annotations

import datetime as dt
from trading_bot.email_fill import Email


def build_critical_email(*, title: str, detail: str, severity: str = "HIGH") -> Email:
    subject = f"[CRITICAL] {title}"
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    body = (
        f"<h2 style='color:#c00'>[{severity}] {title}</h2>"
        f"<p><b>Timestamp (UTC):</b> {ts}</p>"
        f"<pre style='background:#f5f5f5;padding:12px;border-radius:6px'>{detail}</pre>"
        f"<p><i>This alert was generated by the trading bot supervisor. "
        f"It will be repeated up to once per hour while the condition persists.</i></p>"
    )
    return Email(subject=subject, html_body=body)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_email_critical.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_critical.py tests/test_email_critical.py
git commit -m "feat(plan-8): critical alert email builder"
```

---

## Task 11: Stall detector

**Files:**
- Create: `src/trading_bot/watchdog_stall.py`
- Test: `tests/test_watchdog_stall.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_watchdog_stall.py`:

```python
import os
import datetime as dt
from unittest.mock import MagicMock, patch
import pytest

from trading_bot.watchdog_stall import StallDetector, StallVerdict
from trading_bot.state_heartbeat import write_heartbeat


@pytest.fixture
def hb_path(tmp_path):
    return tmp_path / "heartbeat.json"


def test_no_stall_when_recent(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    d = StallDetector(heartbeat_path=hb_path, max_age_seconds=300)
    v = d.check()
    assert v.is_stalled is False
    assert v.age_seconds < 5


def test_stall_when_file_old(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    old = dt.datetime.now().timestamp() - 600
    os.utime(hb_path, (old, old))
    d = StallDetector(heartbeat_path=hb_path, max_age_seconds=300)
    v = d.check()
    assert v.is_stalled is True
    assert v.age_seconds >= 600


def test_stall_when_file_missing(hb_path):
    d = StallDetector(heartbeat_path=hb_path, max_age_seconds=300)
    v = d.check()
    assert v.is_stalled is True


def test_kickstart_calls_launchctl(hb_path):
    d = StallDetector(
        heartbeat_path=hb_path,
        max_age_seconds=300,
        plist_label="com.bharath.trading.daemon.paper",
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        ok = d.kickstart_daemon()
    assert ok is True
    args, kwargs = run.call_args
    cmd = args[0]
    assert "launchctl" in cmd
    assert "kickstart" in cmd
    assert "com.bharath.trading.daemon.paper" in " ".join(cmd)


def test_kickstart_returns_false_on_nonzero_exit(hb_path):
    d = StallDetector(
        heartbeat_path=hb_path,
        max_age_seconds=300,
        plist_label="com.bharath.trading.daemon.paper",
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=1)
        ok = d.kickstart_daemon()
    assert ok is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_watchdog_stall.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement stall detector**

Write `src/trading_bot/watchdog_stall.py`:

```python
"""Heartbeat-based stall detection. Watchdog's job is to:
1. Periodically check heartbeat.json mtime.
2. If stale > max_age_seconds, attempt one launchctl kickstart of the daemon plist.
3. Caller (Supervisor) emits the email.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass
from pathlib import Path

from trading_bot.state_heartbeat import is_stale


@dataclass
class StallVerdict:
    is_stalled: bool
    age_seconds: float


class StallDetector:
    def __init__(
        self,
        *,
        heartbeat_path: str | Path,
        max_age_seconds: int,
        plist_label: str | None = None,
    ):
        self.heartbeat_path = Path(heartbeat_path)
        self.max_age_seconds = max_age_seconds
        self.plist_label = plist_label

    def check(self) -> StallVerdict:
        p = self.heartbeat_path
        if not p.exists():
            return StallVerdict(is_stalled=True, age_seconds=float("inf"))
        age = dt.datetime.now().timestamp() - p.stat().st_mtime
        return StallVerdict(
            is_stalled=age > self.max_age_seconds,
            age_seconds=age,
        )

    def kickstart_daemon(self) -> bool:
        if self.plist_label is None:
            return False
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{_uid()}/{self.plist_label}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0


def _uid() -> int:
    import os
    return os.getuid()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_watchdog_stall.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/watchdog_stall.py tests/test_watchdog_stall.py
git commit -m "feat(plan-8): stall detector with launchctl kickstart"
```

---

## Task 12: Account Sentinel (drawdown + reconciliation)

**Files:**
- Create: `src/trading_bot/watchdog_account.py`
- Test: `tests/test_watchdog_account.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_watchdog_account.py`:

```python
import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base
from trading_bot.state_hwm import update_hwm
from trading_bot.watchdog_account import AccountSentinel, ReconcileVerdict


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    yield engine
    os.unlink(path)


@pytest.fixture
def alpaca():
    """Fake Alpaca client returning a stubbed account."""
    a = MagicMock()
    return a


def test_no_drawdown_when_equity_at_hwm(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("100000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    v = s.check()
    assert v.drawdown_pct == 0.0
    assert v.paused is False
    assert not (tmp_path / "pause.flag").exists()


def test_pauses_on_drawdown_breach(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("78000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    v = s.check()
    assert v.drawdown_pct > 20.0
    assert v.paused is True
    assert (tmp_path / "pause.flag").exists()


def test_does_not_pause_below_threshold(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("82000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    v = s.check()
    assert 17.0 < v.drawdown_pct < 20.0
    assert v.paused is False


def test_advances_hwm_on_new_high(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("105000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    s.check()
    with Session(db) as sess:
        from trading_bot.state_hwm import current_hwm
        assert current_hwm(sess, account="paper") == pytest.approx(105_000.0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_watchdog_account.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement Account Sentinel**

Write `src/trading_bot/watchdog_account.py`:

```python
"""Account Sentinel — independent verification path. Queries Alpaca directly,
updates equity HWM, computes drawdown vs HWM, writes pause.flag if breached.
Does NOT trust the daemon's view of state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.state_pause import set_pause
from trading_bot.state_hwm import current_hwm, update_hwm, drawdown_pct


@dataclass
class ReconcileVerdict:
    equity: Decimal
    hwm: float | None
    drawdown_pct: float
    paused: bool


class AccountSentinel:
    def __init__(
        self,
        *,
        engine,
        alpaca,
        pause_flag_path: str | Path,
        max_dd_pct: float,
        account: str,
    ):
        self.engine = engine
        self.alpaca = alpaca
        self.pause_flag_path = Path(pause_flag_path)
        self.max_dd_pct = max_dd_pct
        self.account = account

    def check(self) -> ReconcileVerdict:
        # Independent fetch. Don't trust daemon's equity number.
        acct = self.alpaca.get_account()
        equity = Decimal(str(acct.equity))

        with Session(self.engine) as session:
            update_hwm(session, account=self.account, equity=float(equity))
            hwm = current_hwm(session, account=self.account)
            dd = drawdown_pct(
                session, account=self.account, current_equity=float(equity)
            )

        paused = False
        if dd > self.max_dd_pct:
            set_pause(
                self.pause_flag_path,
                reason=f"drawdown {dd:.2f}% from HWM ${hwm:,.2f}; equity ${equity:,.2f}",
            )
            paused = True

        return ReconcileVerdict(
            equity=equity,
            hwm=hwm,
            drawdown_pct=dd,
            paused=paused,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_watchdog_account.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/watchdog_account.py tests/test_watchdog_account.py
git commit -m "feat(plan-8): account sentinel with drawdown breach detection"
```

---

## Task 13: APScheduler job registration

**Files:**
- Create: `src/trading_bot/scheduler_jobs.py`
- Test: `tests/test_scheduler_jobs.py`

- [ ] **Step 1: Write failing test**

Write `tests/test_scheduler_jobs.py`:

```python
from unittest.mock import MagicMock
import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from trading_bot.cadence import CadenceConfig
from trading_bot.scheduler_jobs import register_jobs


def test_register_jobs_creates_expected_jobs():
    sched = BackgroundScheduler(timezone="America/New_York")
    cadence = CadenceConfig()
    runners = {
        "intel_scan": MagicMock(),
        "crypto_scan": MagicMock(),
        "portfolio_watch": MagicMock(),
        "verify_stops": MagicMock(),
        "news_warm": MagicMock(),
        "massive_refresh": MagicMock(),
        "premarket_rank": MagicMock(),
        "vip_scan": MagicMock(),
        "daily_digest": MagicMock(),
        "heartbeat": MagicMock(),
    }
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)
    job_ids = {j.id for j in sched.get_jobs()}
    expected = {
        "heartbeat",
        "stock_scanner",
        "crypto_scanner",
        "portfolio_monitor",
        "order_steward_sweep",
        "vip_listener",
        "news_warm_morning",
        "news_warm_midday",
        "massive_refresh",
        "premarket_rank",
        "daily_digest",
    }
    assert expected.issubset(job_ids)


def test_register_jobs_uses_cadence_minutes():
    sched = BackgroundScheduler(timezone="America/New_York")
    cadence = CadenceConfig(crypto_scanner_minutes=15)  # override default 30
    runners = {
        "intel_scan": MagicMock(), "crypto_scan": MagicMock(),
        "portfolio_watch": MagicMock(), "verify_stops": MagicMock(),
        "news_warm": MagicMock(), "massive_refresh": MagicMock(),
        "premarket_rank": MagicMock(), "vip_scan": MagicMock(),
        "daily_digest": MagicMock(), "heartbeat": MagicMock(),
    }
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)
    crypto = next(j for j in sched.get_jobs() if j.id == "crypto_scanner")
    # IntervalTrigger exposes interval as a timedelta
    assert crypto.trigger.interval.total_seconds() == 15 * 60


def test_heartbeat_job_runs_every_60s():
    sched = BackgroundScheduler(timezone="America/New_York")
    cadence = CadenceConfig()
    runners = {
        "intel_scan": MagicMock(), "crypto_scan": MagicMock(),
        "portfolio_watch": MagicMock(), "verify_stops": MagicMock(),
        "news_warm": MagicMock(), "massive_refresh": MagicMock(),
        "premarket_rank": MagicMock(), "vip_scan": MagicMock(),
        "daily_digest": MagicMock(), "heartbeat": MagicMock(),
    }
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)
    hb = next(j for j in sched.get_jobs() if j.id == "heartbeat")
    assert hb.trigger.interval.total_seconds() == 60
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_scheduler_jobs.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement scheduler job registration**

Write `src/trading_bot/scheduler_jobs.py`:

```python
"""APScheduler job registration. Each scheduled routine is wired up here.
The `runners` dict maps logical names to callables; this lets daemon.py
inject the existing CLI command functions (or test mocks).
"""
from __future__ import annotations

from typing import Callable, Mapping

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from trading_bot.cadence import CadenceConfig


def register_jobs(
    *,
    scheduler: BaseScheduler,
    cadence: CadenceConfig,
    runners: Mapping[str, Callable[[], None]],
) -> None:
    et = "America/New_York"

    # Continuous: heartbeat
    scheduler.add_job(
        runners["heartbeat"],
        trigger=IntervalTrigger(seconds=cadence.heartbeat_seconds),
        id="heartbeat",
        replace_existing=True,
    )

    # Stock Scanner: every 60 min during market hours, weekdays
    scheduler.add_job(
        runners["intel_scan"],
        trigger=CronTrigger(
            hour="9-15",
            minute=f"*/{cadence.stock_scanner_minutes}" if cadence.stock_scanner_minutes < 60 else "30",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="stock_scanner",
        replace_existing=True,
    )

    # Crypto Scanner: 24/7 at configured cadence
    scheduler.add_job(
        runners["crypto_scan"],
        trigger=IntervalTrigger(minutes=cadence.crypto_scanner_minutes),
        id="crypto_scanner",
        replace_existing=True,
    )

    # Portfolio Monitor: every N min during market hours
    scheduler.add_job(
        runners["portfolio_watch"],
        trigger=CronTrigger(
            hour="9-16",
            minute=f"*/{cadence.portfolio_monitor_minutes}",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="portfolio_monitor",
        replace_existing=True,
    )

    # Order Steward sweep
    scheduler.add_job(
        runners["verify_stops"],
        trigger=CronTrigger(
            hour="9-16",
            minute=f"*/{cadence.order_steward_sweep_minutes}",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="order_steward_sweep",
        replace_existing=True,
    )

    # VIP Listener: every N min during market hours
    scheduler.add_job(
        runners["vip_scan"],
        trigger=CronTrigger(
            hour="9-16",
            minute=f"*/{cadence.vip_listener_minutes}",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="vip_listener",
        replace_existing=True,
    )

    # Sentiment warm: at configured ET times
    for label, time_str in (("morning", cadence.sentiment_warm_times_et[0]),
                             ("midday", cadence.sentiment_warm_times_et[1])):
        h, m = time_str.split(":")
        scheduler.add_job(
            runners["news_warm"],
            trigger=CronTrigger(hour=h, minute=m, day_of_week="mon-fri", timezone=et),
            id=f"news_warm_{label}",
            replace_existing=True,
        )

    # Pre-market: massive-refresh + rank
    scheduler.add_job(
        runners["massive_refresh"],
        trigger=CronTrigger(hour=6, minute=30, day_of_week="mon-fri", timezone=et),
        id="massive_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        runners["premarket_rank"],
        trigger=CronTrigger(hour=7, minute=30, day_of_week="mon-fri", timezone=et),
        id="premarket_rank",
        replace_existing=True,
    )

    # Daily digest: 18:00 ET weekdays
    scheduler.add_job(
        runners["daily_digest"],
        trigger=CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=et),
        id="daily_digest",
        replace_existing=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_scheduler_jobs.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/scheduler_jobs.py tests/test_scheduler_jobs.py
git commit -m "feat(plan-8): APScheduler job registration with cadence config"
```

---

## Task 14: Daemon entrypoint

**Files:**
- Create: `src/trading_bot/daemon.py`

- [ ] **Step 1: Write the daemon entrypoint**

Write `src/trading_bot/daemon.py`:

```python
"""Daemon entrypoint. Long-running process under launchd.

Usage:
    python -m trading_bot.daemon

Reads paper_active.json, runs Alembic migrations, registers APScheduler
jobs, runs forever. Heartbeat fires every cadence.heartbeat_seconds.
On SIGTERM, gracefully stops scheduler and exits 0.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from trading_bot.cadence import load_cadence
from trading_bot.log_structured import StructuredLogger
from trading_bot.scheduler_jobs import register_jobs
from trading_bot.state_heartbeat import write_heartbeat
from trading_bot.state_pause import is_paused


CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
HEARTBEAT_PATH = Path(os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"))
PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))


def _load_runners(log: StructuredLogger):
    """Wraps existing CLI command functions, plus heartbeat."""
    # Lazy imports so daemon module can be imported in tests without side effects
    from trading_bot import cli as cli_mod

    config_version = "phase1-v1"

    def _heartbeat():
        write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action="heartbeat")

    def _wrap(name: str, fn: callable):
        def runner():
            log.event(f"{name}_start")
            if is_paused(PAUSE_PATH) and name in {"intel_scan", "crypto_scan"}:
                log.event(f"{name}_skipped", reason="pause.flag set")
                write_heartbeat(HEARTBEAT_PATH, version=config_version,
                                last_action=f"{name}_skipped_paused")
                return
            try:
                fn()
                log.event(f"{name}_finish")
            except Exception as e:
                log.error(f"{name}_failed", error=e)
            finally:
                write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action=name)
        return runner

    # Click command callbacks. Each is a callable that does its work.
    return {
        "heartbeat": _heartbeat,
        "intel_scan": _wrap("intel_scan", lambda: cli_mod.intel_scan.callback()),
        "crypto_scan": _wrap("crypto_scan", lambda: cli_mod.crypto_scan.callback()),
        "portfolio_watch": _wrap("portfolio_watch", lambda: cli_mod.portfolio_watch.callback()),
        "verify_stops": _wrap("verify_stops", lambda: cli_mod.verify_stops.callback()),
        "news_warm": _wrap("news_warm", lambda: cli_mod.news_warm.callback()),
        "massive_refresh": _wrap("massive_refresh", lambda: cli_mod.massive_refresh.callback()),
        "premarket_rank": _wrap("premarket_rank", lambda: cli_mod.rank.callback()),
        "vip_scan": _wrap("vip_scan", lambda: cli_mod.vip_scan.callback()),
        "daily_digest": _wrap("daily_digest", lambda: cli_mod.full_run.callback()),
    }


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="daemon")
    log.event("daemon_boot", config_path=str(CONFIG_PATH))

    if not CONFIG_PATH.exists():
        log.error(
            "daemon_no_config",
            error=FileNotFoundError(f"config missing: {CONFIG_PATH}"),
        )
        return 1

    cadence = load_cadence(CONFIG_PATH)
    log.event("cadence_loaded",
              heartbeat=cadence.heartbeat_seconds,
              stock_scanner_minutes=cadence.stock_scanner_minutes)

    sched = BackgroundScheduler(timezone="America/New_York")
    runners = _load_runners(log)
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)

    # Initial heartbeat before scheduler runs (so supervisor doesn't see stale boot)
    runners["heartbeat"]()

    sched.start()
    log.event("scheduler_started", jobs=[j.id for j in sched.get_jobs()])

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("daemon_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        log.event("daemon_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke-test the daemon imports cleanly**

```bash
python -c "from trading_bot import daemon; print(daemon.CONFIG_PATH)"
```

Expected: prints `data/paper_active.json` (or whatever env var sets).

- [ ] **Step 3: Smoke-test daemon boots and exits on SIGINT (no jobs run)**

```bash
mkdir -p data runs
TRADING_BOT_CONFIG=data/paper_active.json \
TRADING_BOT_HEARTBEAT=/tmp/hb.json \
TRADING_BOT_PAUSE=/tmp/pause.flag \
TRADING_BOT_RUNS=/tmp/runs \
timeout 5 python -m trading_bot.daemon || echo "Exit code: $?"
ls /tmp/hb.json
cat /tmp/runs/*/daemon/*.json | head -3
```

Expected:
- Exit code: 124 (timeout) — daemon ran for 5s then timeout killed it
- `/tmp/hb.json` exists
- At least one `daemon_boot` event in /tmp/runs

- [ ] **Step 4: Commit**

```bash
git add src/trading_bot/daemon.py
git commit -m "feat(plan-8): daemon entrypoint with APScheduler + heartbeat"
```

---

## Task 15: Supervisor entrypoint

**Files:**
- Create: `src/trading_bot/supervisor.py`

- [ ] **Step 1: Write the supervisor entrypoint**

Write `src/trading_bot/supervisor.py`:

```python
"""Supervisor entrypoint. Independent verification process under launchd.

Usage:
    python -m trading_bot.supervisor

Runs every 60s:
- Watchdog: heartbeat staleness → kickstart daemon + email.
- Account Sentinel (every 5 min during market hours): drawdown breach → pause.flag + email.
- Independently queries Alpaca, does not trust daemon's view.
"""
from __future__ import annotations

import datetime as dt
import os
import signal
import sys
import time
from pathlib import Path

from trading_bot.cadence import load_cadence
from trading_bot.log_structured import StructuredLogger
from trading_bot.email_critical import build_critical_email
from trading_bot.email_sender import send_email
from trading_bot.state_db import get_engine
from trading_bot.watchdog_account import AccountSentinel
from trading_bot.watchdog_stall import StallDetector


CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
HEARTBEAT_PATH = Path(os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"))
PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))
DAEMON_PLIST_LABEL = os.environ.get(
    "TRADING_BOT_DAEMON_PLIST", "com.bharath.trading.daemon.paper"
)
ALERT_RECIPIENT = os.environ.get("TRADING_BOT_ALERT_TO", "bharath8887@gmail.com")


def _is_market_hours_et() -> bool:
    """09:30-16:00 ET, Mon-Fri. Approximate via UTC offset; APScheduler handles DST."""
    import zoneinfo
    now = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="supervisor")
    log.event("supervisor_boot")

    cadence = load_cadence(CONFIG_PATH)
    stall_max_age = 5 * 60  # spec: > 5 min stale triggers kickstart

    # Lazy-build Alpaca client when needed (handle absent creds in tests)
    def _alpaca():
        from trading_bot.alpaca_client import build_alpaca_client
        return build_alpaca_client()

    stall_detector = StallDetector(
        heartbeat_path=HEARTBEAT_PATH,
        max_age_seconds=stall_max_age,
        plist_label=DAEMON_PLIST_LABEL,
    )

    engine = get_engine(STATE_DB)
    last_account_check = 0.0

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("supervisor_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    while not stop["flag"]:
        try:
            # 1. Watchdog: every 60s
            verdict = stall_detector.check()
            if verdict.is_stalled:
                log.event("stall_detected", age_seconds=verdict.age_seconds)
                kicked = stall_detector.kickstart_daemon()
                log.event("kickstart_attempted", success=kicked)
                email = build_critical_email(
                    title="Daemon stalled",
                    detail=(
                        f"Heartbeat last seen {verdict.age_seconds:.0f}s ago "
                        f"(threshold {stall_max_age}s).\n"
                        f"Auto-restart attempted via launchctl: "
                        f"{'success' if kicked else 'failed'}."
                    ),
                )
                send_email(
                    to=ALERT_RECIPIENT,
                    subject=email.subject,
                    html_body=email.html_body,
                )

            # 2. Account Sentinel: 5 min during market hours, 30 min off-hours
            interval = (
                cadence.account_sentinel_minutes_market
                if _is_market_hours_et()
                else cadence.account_sentinel_minutes_offhours
            )
            now = time.time()
            if now - last_account_check >= interval * 60:
                try:
                    acct_sentinel = AccountSentinel(
                        engine=engine,
                        alpaca=_alpaca(),
                        pause_flag_path=PAUSE_PATH,
                        max_dd_pct=20.0,
                        account="paper",
                    )
                    av = acct_sentinel.check()
                    log.event(
                        "account_check",
                        equity=str(av.equity),
                        hwm=av.hwm,
                        drawdown_pct=av.drawdown_pct,
                        paused=av.paused,
                    )
                    if av.paused:
                        email = build_critical_email(
                            title="Drawdown breach — trading paused",
                            detail=(
                                f"Drawdown {av.drawdown_pct:.2f}% from HWM ${av.hwm:,.2f}.\n"
                                f"Current equity ${av.equity:,.2f}.\n"
                                f"pause.flag written. Daemon will not place new orders."
                            ),
                        )
                        send_email(
                            to=ALERT_RECIPIENT,
                            subject=email.subject,
                            html_body=email.html_body,
                        )
                except Exception as e:
                    log.error("account_check_failed", error=e)
                last_account_check = now

        except Exception as e:
            log.error("supervisor_loop_error", error=e)

        time.sleep(cadence.watchdog_seconds)

    log.event("supervisor_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify supervisor imports cleanly**

```bash
python -c "from trading_bot import supervisor; print(supervisor.STATE_DB)"
```

Expected: prints path; no ImportError.

- [ ] **Step 3: Smoke-test supervisor boots and exits**

```bash
mkdir -p data runs
TRADING_BOT_CONFIG=data/paper_active.json \
TRADING_BOT_HEARTBEAT=/tmp/hb_sup.json \
TRADING_BOT_PAUSE=/tmp/pause_sup.flag \
TRADING_BOT_RUNS=/tmp/runs_sup \
TRADING_BOT_STATE_DB=/tmp/sup_test.db \
TRADING_BOT_ALERT_TO=test@local \
timeout 3 python -m trading_bot.supervisor || echo "Exit code: $?"
ls /tmp/runs_sup/*/supervisor/*.json | head -3
```

Expected: exit 124 after 3s timeout, supervisor_boot event in logs.

- [ ] **Step 4: Commit**

```bash
git add src/trading_bot/supervisor.py
git commit -m "feat(plan-8): supervisor entrypoint with watchdog + account sentinel loop"
```

---

## Task 16: launchd plist for daemon

**Files:**
- Create: `ops/launchd/com.bharath.trading.daemon.paper.plist`

- [ ] **Step 1: Write the plist**

Create `ops/launchd/com.bharath.trading.daemon.paper.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bharath.trading.daemon.paper</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/bharathkandala/Trading/.venv/bin/python</string>
    <string>-m</string>
    <string>trading_bot.daemon</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/bharathkandala/Trading</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>TRADING_BOT_CONFIG</key>
    <string>/Users/bharathkandala/Trading/data/paper_active.json</string>
    <key>TRADING_BOT_HEARTBEAT</key>
    <string>/Users/bharathkandala/Trading/data/heartbeat.json</string>
    <key>TRADING_BOT_PAUSE</key>
    <string>/Users/bharathkandala/Trading/data/pause.flag</string>
    <key>TRADING_BOT_RUNS</key>
    <string>/Users/bharathkandala/Trading/runs</string>
    <key>TRADING_BOT_STATE_DB</key>
    <string>/Users/bharathkandala/Trading/data/state.db</string>
    <key>PYTHONPATH</key>
    <string>/Users/bharathkandala/Trading/src</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/Users/bharathkandala/Trading/runs/_launchd/daemon.stdout.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/bharathkandala/Trading/runs/_launchd/daemon.stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Verify plist syntax**

```bash
plutil -lint ops/launchd/com.bharath.trading.daemon.paper.plist
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add ops/launchd/com.bharath.trading.daemon.paper.plist
git commit -m "feat(plan-8): launchd plist for paper daemon"
```

---

## Task 17: launchd plist for supervisor

**Files:**
- Create: `ops/launchd/com.bharath.trading.supervisor.plist`

- [ ] **Step 1: Write the plist**

Create `ops/launchd/com.bharath.trading.supervisor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bharath.trading.supervisor</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/bharathkandala/Trading/.venv/bin/python</string>
    <string>-m</string>
    <string>trading_bot.supervisor</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/bharathkandala/Trading</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>TRADING_BOT_CONFIG</key>
    <string>/Users/bharathkandala/Trading/data/paper_active.json</string>
    <key>TRADING_BOT_HEARTBEAT</key>
    <string>/Users/bharathkandala/Trading/data/heartbeat.json</string>
    <key>TRADING_BOT_PAUSE</key>
    <string>/Users/bharathkandala/Trading/data/pause.flag</string>
    <key>TRADING_BOT_RUNS</key>
    <string>/Users/bharathkandala/Trading/runs</string>
    <key>TRADING_BOT_STATE_DB</key>
    <string>/Users/bharathkandala/Trading/data/state.db</string>
    <key>TRADING_BOT_DAEMON_PLIST</key>
    <string>com.bharath.trading.daemon.paper</string>
    <key>TRADING_BOT_ALERT_TO</key>
    <string>bharath8887@gmail.com</string>
    <key>PYTHONPATH</key>
    <string>/Users/bharathkandala/Trading/src</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/Users/bharathkandala/Trading/runs/_launchd/supervisor.stdout.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/bharathkandala/Trading/runs/_launchd/supervisor.stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Verify plist syntax**

```bash
plutil -lint ops/launchd/com.bharath.trading.supervisor.plist
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add ops/launchd/com.bharath.trading.supervisor.plist
git commit -m "feat(plan-8): launchd plist for supervisor"
```

---

## Task 18: install / uninstall script

**Files:**
- Create: `ops/install.sh`
- Create: `ops/uninstall.sh`

- [ ] **Step 1: Write install script**

Create `ops/install.sh`:

```bash
#!/usr/bin/env bash
# Install trading bot launchd plists and start them.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
DAEMON_LABEL="com.bharath.trading.daemon.paper"
SUPERVISOR_LABEL="com.bharath.trading.supervisor"

echo "Installing launchd plists from $REPO_ROOT/ops/launchd to $LAUNCHD_DIR"
mkdir -p "$LAUNCHD_DIR"
mkdir -p "$REPO_ROOT/runs/_launchd"
mkdir -p "$REPO_ROOT/data"

# Copy plists (source of truth in repo; runtime copy under LaunchAgents)
cp "$REPO_ROOT/ops/launchd/${DAEMON_LABEL}.plist" "$LAUNCHD_DIR/"
cp "$REPO_ROOT/ops/launchd/${SUPERVISOR_LABEL}.plist" "$LAUNCHD_DIR/"

# Run Alembic migrations to ensure state.db schema is current
cd "$REPO_ROOT"
"$REPO_ROOT/.venv/bin/alembic" -c migrations/alembic.ini upgrade head

# Unload if already loaded (idempotent), then load
launchctl unload "$LAUNCHD_DIR/${DAEMON_LABEL}.plist" 2>/dev/null || true
launchctl unload "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist" 2>/dev/null || true
launchctl load -w "$LAUNCHD_DIR/${DAEMON_LABEL}.plist"
launchctl load -w "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist"

echo "Installed and loaded:"
launchctl list | grep -E "${DAEMON_LABEL}|${SUPERVISOR_LABEL}" || true
echo
echo "Logs at: $REPO_ROOT/runs/_launchd/"
echo "Heartbeat: $REPO_ROOT/data/heartbeat.json"
echo "Done."
```

- [ ] **Step 2: Write uninstall script**

Create `ops/uninstall.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
DAEMON_LABEL="com.bharath.trading.daemon.paper"
SUPERVISOR_LABEL="com.bharath.trading.supervisor"

launchctl unload "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist" 2>/dev/null || true
launchctl unload "$LAUNCHD_DIR/${DAEMON_LABEL}.plist" 2>/dev/null || true
rm -f "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist"
rm -f "$LAUNCHD_DIR/${DAEMON_LABEL}.plist"

echo "Unloaded and removed plists. State databases and logs left intact."
```

- [ ] **Step 3: Make scripts executable**

```bash
chmod +x ops/install.sh ops/uninstall.sh
```

- [ ] **Step 4: Verify scripts pass shellcheck (optional but recommended)**

```bash
which shellcheck && shellcheck ops/install.sh ops/uninstall.sh || echo "shellcheck not installed; skipping"
```

If shellcheck installed: expected no errors. If not: skip.

- [ ] **Step 5: Commit**

```bash
git add ops/install.sh ops/uninstall.sh
git commit -m "feat(plan-8): install and uninstall scripts for launchd plists"
```

---

## Task 19: Email digest persistence — wire daily digest to journal

**Files:**
- Modify: `src/trading_bot/cli.py` (extend `full_run` or add a new `daily-digest` Click command that uses `email_digest.build_digest_email`)
- Test: `tests/test_email_digest_integration.py`

- [ ] **Step 1: Write integration test**

Write `tests/test_email_digest_integration.py`:

```python
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

from trading_bot.email_digest import DigestContext, TradeRow, build_digest_email


def test_send_daily_digest_smtp_call():
    """Verify the digest builder + SMTP transport composition."""
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500.00"),
        ending_equity=Decimal("103895.00"),
        realized_pnl=Decimal("-422.62"),
        unrealized_pnl=Decimal("139.72"),
        regime="trending_up",
        active_config_version="phase1-v1",
        trades=[
            TradeRow(
                side="BUY", symbol="AAPL", qty=Decimal("41"),
                price=Decimal("190.24"), strategy="momentum_v3",
                time=dt.time(10, 0), status="open",
            ),
        ],
        errors=[],
    )
    email = build_digest_email(ctx)

    with patch("smtplib.SMTP_SSL") as smtp:
        smtp.return_value.__enter__.return_value = MagicMock()
        from trading_bot.email_sender import send_email
        ok = send_email(
            to="bharath8887@gmail.com",
            subject=email.subject,
            html_body=email.html_body,
        )
    assert ok is True
    smtp.assert_called_once()
```

- [ ] **Step 2: Run the test (verify it passes if email_sender exists; adjust if needed)**

```bash
pytest tests/test_email_digest_integration.py -v
```

If `send_email` doesn't have the exact signature `(to, subject, html_body)` in the existing `email_sender.py`, adjust either the test or the call site to match. The test goal is: digest builder output is consumable by the SMTP transport.

- [ ] **Step 3: Commit**

```bash
git add tests/test_email_digest_integration.py
git commit -m "test(plan-8): integration test for digest+SMTP composition"
```

---

## Task 20: Integration test — daemon cold-start

**Files:**
- Create: `tests/test_integration_daemon.py`

- [ ] **Step 1: Write integration test**

Write `tests/test_integration_daemon.py`:

```python
import json
import os
import subprocess
import time
from pathlib import Path

import pytest


@pytest.mark.integration
def test_daemon_cold_start_writes_heartbeat(tmp_path):
    """Boot the daemon for ~5s, verify heartbeat.json appears and a
    daemon_boot event lands in runs/."""
    config_path = tmp_path / "paper_active.json"
    config_path.write_text(json.dumps({
        "version": "test-v1",
        "active_template": "momentum_v3",
        "params": {},
        "risk_caps": {"max_position_pct": 10, "daily_loss_pct": 3, "max_drawdown_pct": 20},
        "cadence": {"heartbeat_seconds": 1},  # fast heartbeat for test
    }))
    heartbeat_path = tmp_path / "heartbeat.json"
    pause_path = tmp_path / "pause.flag"
    runs_dir = tmp_path / "runs"

    env = os.environ.copy()
    env.update({
        "TRADING_BOT_CONFIG": str(config_path),
        "TRADING_BOT_HEARTBEAT": str(heartbeat_path),
        "TRADING_BOT_PAUSE": str(pause_path),
        "TRADING_BOT_RUNS": str(runs_dir),
        "PYTHONPATH": "src",
    })

    proc = subprocess.Popen(
        ["python", "-m", "trading_bot.daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait up to 5s for heartbeat to appear
        deadline = time.time() + 5
        while time.time() < deadline and not heartbeat_path.exists():
            time.sleep(0.2)
        assert heartbeat_path.exists(), "Heartbeat not written within 5s"

        # Verify heartbeat content
        hb = json.loads(heartbeat_path.read_text())
        assert "ts" in hb
        assert hb["pid"] == proc.pid

        # Verify daemon_boot event in runs/
        date_dirs = list(runs_dir.glob("*/daemon"))
        assert date_dirs, "No daemon run directory created"
        events = []
        for d in date_dirs:
            for f in d.glob("*.json"):
                events.append(json.loads(f.read_text()))
        boot_events = [e for e in events if e.get("event") == "daemon_boot"]
        assert boot_events, f"No daemon_boot event in {events}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
```

- [ ] **Step 2: Run integration test**

```bash
pytest tests/test_integration_daemon.py -v -m integration
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration_daemon.py
git commit -m "test(plan-8): integration test for daemon cold-start + heartbeat"
```

---

## Task 21: Integration test — supervisor detects stall

**Files:**
- Create: `tests/test_integration_supervisor.py`

- [ ] **Step 1: Write integration test**

Write `tests/test_integration_supervisor.py`:

```python
import json
import os
import subprocess
import time
from pathlib import Path

import pytest


@pytest.mark.integration
def test_supervisor_logs_stall_when_heartbeat_old(tmp_path):
    """Supervisor must observe a stale heartbeat and log a stall_detected event."""
    # Pre-create an old heartbeat
    heartbeat_path = tmp_path / "heartbeat.json"
    heartbeat_path.write_text(json.dumps({
        "ts": "2020-01-01T00:00:00+00:00",
        "pid": 999,
        "version": "fake",
        "last_action": "boot",
    }))
    old = time.time() - 600  # 10 min old
    os.utime(heartbeat_path, (old, old))

    # Minimal config
    config_path = tmp_path / "paper_active.json"
    config_path.write_text(json.dumps({
        "version": "test", "active_template": "x", "params": {},
        "risk_caps": {"max_position_pct": 10, "daily_loss_pct": 3, "max_drawdown_pct": 20},
        "cadence": {"watchdog_seconds": 1},
    }))

    pause_path = tmp_path / "pause.flag"
    runs_dir = tmp_path / "runs"
    state_db = tmp_path / "state.db"

    env = os.environ.copy()
    env.update({
        "TRADING_BOT_CONFIG": str(config_path),
        "TRADING_BOT_HEARTBEAT": str(heartbeat_path),
        "TRADING_BOT_PAUSE": str(pause_path),
        "TRADING_BOT_RUNS": str(runs_dir),
        "TRADING_BOT_STATE_DB": str(state_db),
        "TRADING_BOT_DAEMON_PLIST": "fake.label.that.does.not.exist",
        "TRADING_BOT_ALERT_TO": "test@local",  # SMTP will fail; expected
        "PYTHONPATH": "src",
    })

    proc = subprocess.Popen(
        ["python", "-m", "trading_bot.supervisor"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(3)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # Inspect supervisor logs for stall_detected event
    events = []
    for f in (runs_dir.glob("*/supervisor/*.json")):
        events.append(json.loads(f.read_text()))
    stall_events = [e for e in events if e.get("event") == "stall_detected"]
    assert stall_events, f"No stall_detected event found among {events}"
```

- [ ] **Step 2: Run integration test**

```bash
pytest tests/test_integration_supervisor.py -v -m integration
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration_supervisor.py
git commit -m "test(plan-8): integration test for supervisor stall detection"
```

---

## Task 22: Integration test — drawdown breach writes pause flag

**Files:**
- Create: `tests/test_integration_drawdown.py`

- [ ] **Step 1: Write integration test**

Write `tests/test_integration_drawdown.py`:

```python
import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base
from trading_bot.state_hwm import update_hwm
from trading_bot.watchdog_account import AccountSentinel


@pytest.mark.integration
def test_drawdown_breach_writes_pause_flag(tmp_path):
    """End-to-end: HWM at 100k, current equity 78k -> 22% DD -> pause.flag."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)

        # Seed HWM
        with Session(engine) as s:
            update_hwm(s, account="paper", equity=100_000.0)

        # Fake Alpaca client returning low equity
        alpaca = MagicMock()
        alpaca.get_account.return_value = MagicMock(equity=Decimal("78000"))

        sentinel = AccountSentinel(
            engine=engine,
            alpaca=alpaca,
            pause_flag_path=tmp_path / "pause.flag",
            max_dd_pct=20.0,
            account="paper",
        )
        verdict = sentinel.check()
        assert verdict.paused is True
        assert (tmp_path / "pause.flag").exists()
        assert "drawdown" in (tmp_path / "pause.flag").read_text().lower()
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run integration test**

```bash
pytest tests/test_integration_drawdown.py -v -m integration
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration_drawdown.py
git commit -m "test(plan-8): integration test for drawdown breach -> pause flag"
```

---

## Task 23: Configure pytest integration marker + run full suite

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the integration marker to pytest config**

In `pyproject.toml`, ensure there is a `[tool.pytest.ini_options]` section with:

```toml
[tool.pytest.ini_options]
markers = [
    "integration: integration tests that spawn subprocesses or hit databases",
]
```

If the section already exists, just add the markers entry.

- [ ] **Step 2: Run the full test suite**

```bash
pytest tests/ -v
```

Expected: all Phase 1 tests pass. Existing tests should also pass (we did not modify their dependencies in a breaking way). If any pre-existing test fails due to the schema additions, address it as a subtask before proceeding.

- [ ] **Step 3: Commit pytest config**

```bash
git add pyproject.toml
git commit -m "chore(plan-8): register integration test marker"
```

---

## Task 24: End-to-end deployment dry run on the local Mac

**Files:** none (manual verification)

This task is the user-visible smoke test. Run it once Phase 1 is implementation-complete.

- [ ] **Step 1: Apply migrations**

```bash
cd /Users/bharathkandala/Trading
.venv/bin/alembic -c migrations/alembic.ini upgrade head
sqlite3 data/state.db ".tables"
```

Expected: 6 tables listed.

- [ ] **Step 2: Manual launchd install**

```bash
ops/install.sh
```

Expected output: "Installed and loaded:" followed by the two labels with PIDs.

- [ ] **Step 3: Verify daemon process is running**

```bash
launchctl list | grep com.bharath.trading
ps aux | grep "trading_bot.daemon" | grep -v grep
ps aux | grep "trading_bot.supervisor" | grep -v grep
```

Expected: both processes alive.

- [ ] **Step 4: Verify heartbeat is updating**

```bash
sleep 90
ls -la data/heartbeat.json
cat data/heartbeat.json
```

Expected: mtime within last ~90s; valid JSON with current pid.

- [ ] **Step 5: Verify logs are landing**

```bash
ls runs/$(date +%Y-%m-%d)/daemon/ | head -5
ls runs/$(date +%Y-%m-%d)/supervisor/ | head -5
```

Expected: at least a `daemon_boot` and `supervisor_boot` event each.

- [ ] **Step 6: Test stall recovery (simulated)**

Find the daemon PID and SIGSTOP it (simulates a hang):

```bash
DAEMON_PID=$(pgrep -f "trading_bot.daemon")
kill -STOP "$DAEMON_PID"
echo "Stopped daemon at $(date). Heartbeat will go stale in ~5 min."
```

Wait at least 6 minutes. Then check:

```bash
launchctl list | grep com.bharath.trading.daemon.paper
ls -la data/heartbeat.json
```

Expected: daemon process has been kickstarted by supervisor (PID will have changed; heartbeat mtime fresh).

You can also resume the original frozen process to clean up:

```bash
kill -CONT "$DAEMON_PID" 2>/dev/null || true
```

- [ ] **Step 7: Test drawdown pause (manual write)**

Manually simulate a drawdown breach by writing a too-low equity HWM and checking supervisor reaction. Skip if you don't want to test this end-to-end on the real account; the integration tests cover the unit behavior.

- [ ] **Step 8: Test uninstall**

```bash
ops/uninstall.sh
launchctl list | grep com.bharath.trading || echo "Uninstalled cleanly."
```

Expected: "Uninstalled cleanly."

- [ ] **Step 9: Re-install for ongoing operation**

```bash
ops/install.sh
```

Phase 1 deployed.

---

## Acceptance criteria for Phase 1

The phase is shipped when:

1. `pytest tests/` passes (including integration tests).
2. `ops/install.sh` installs both plists and they show running in `launchctl list`.
3. `data/heartbeat.json` updates every 60s.
4. Stopping the daemon via `kill -STOP` triggers a kickstart from the supervisor within ~5 min.
5. JSON event logs land in `runs/<date>/<role>/`.
6. SMTP-deliverable email is built (verified by `tests/test_email_digest_integration.py`).
7. Drawdown breach writes `pause.flag` (verified by integration test).
8. Daemon refuses to place new orders when `pause.flag` exists (verified by `intel_scan_skipped` event when paused — unit-test'd via `tests/test_state_pause.py` and exercised in daemon's `_wrap` code).

---

## Notes on what's NOT in this plan

These are explicitly deferred to subsequent phase plans:

- **Role Protocol abstraction + charters** — Phase 2.
- **Param Optimizer + Backtest Engineer + Promoter + leaderboard** — Phase 3.
- **Strategy Coach + Hold-SPY Coordinator** — Phase 4.
- **Strategy Architect + Code Reviewer + Calibrator** (LLM lab roles) — Phase 5.
- **`bot promote` CLI + live daemon** — Phase 6.

Phase 1 strictly delivers the operational envelope: existing strategies keep doing what they already do, but now reliably under launchd, with stall detection, drawdown protection, structured logging, and an email pipeline ready to be enriched in subsequent phases.
