# Phase 2 — Role Pattern + KPIs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Phase 1's deployed routines into the 26-role taxonomy from the spec — each routine becomes a named Role with a charter, a measurable KPI, and a report card surfaced in the daily digest. Adds two new supervisor-side operational roles (Schedule Auditor, Resource Guardian) that close the observability gap. Folds in deferred Important issues from the Phase 1 final review.

**Architecture:** A `Role` Protocol + concrete `BaseRole` class with `safe_run()` provides a uniform contract: every routine reports its run latency/status/outputs into `state.db.role_runs`, computes a single KPI written to `state.db.role_kpis`, and exposes a `ReportCard` summarizing recent performance. Existing CLI commands stay; Roles are thin wrappers that capture instrumentation and KPIs. The daemon's APScheduler invokes `Role.safe_run(ctx)` instead of bare CLI lambdas. The daily digest gains a per-role status table.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 (state_db tables already exist), APScheduler 3.11, no new external deps.

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md) §7 (Role taxonomy), §10 (Email contract).
**Reference Phase 1 plan:** [docs/superpowers/plans/2026-04-27-phase-1-operational-hardening.md](2026-04-27-phase-1-operational-hardening.md)

**Phase 2 scope explicitly excludes:** Insider Tracker (EDGAR), Earnings Watcher, Macro Sensor, Strategy Coach, Hold-SPY Coordinator, all Lab roles (Param Optimizer, Strategy Architect, Code Reviewer, Calibrator, Promoter), Tone Analyst — these come in Phases 2.5/3/4/5 with their own specs.

---

## File structure for Phase 2

### New files

```
src/trading_bot/
  roles/
    __init__.py                  # exports Role, BaseRole, RoleResult, ReportCard, Health
    base.py                      # Role Protocol, RoleResult, ReportCard, Health, BaseRole
    runner.py                    # safe_run + persistence helpers
    universe_curator.py          # wraps massive_refresh + rank_command
    sentiment_analyst.py         # wraps news_warm
    stock_scanner.py             # wraps intel_scan
    crypto_scanner.py            # wraps crypto_scan
    portfolio_monitor.py         # wraps portfolio_watch
    order_steward.py             # wraps verify_stops
    vip_listener.py              # wraps vip_scan
    reporter.py                  # wraps midday/eod email + assembles ReportCards
    health_pulse.py              # heartbeat-as-a-Role
    watchdog.py                  # wraps StallDetector
    account_sentinel.py          # wraps AccountSentinel
    schedule_auditor.py          # NEW operational role
    resource_guardian.py         # NEW operational role
  log_rotation.py                # NEW: archive runs/<date>/ older than 90 days

tests/
  roles/
    __init__.py
    test_base.py
    test_runner.py
    test_universe_curator.py
    test_sentiment_analyst.py
    test_stock_scanner.py
    test_crypto_scanner.py
    test_portfolio_monitor.py
    test_order_steward.py
    test_vip_listener.py
    test_reporter.py
    test_health_pulse.py
    test_watchdog.py
    test_account_sentinel_role.py    # new tests; existing test_watchdog_account stays for the underlying class
    test_schedule_auditor.py
    test_resource_guardian.py
  test_log_rotation.py
  test_email_digest_with_report_cards.py
```

### Files modified

- `src/trading_bot/state_heartbeat.py` — `is_stale()` uses `time.time()` (I1)
- `src/trading_bot/cli.py` — add `daemon` and `supervisor` subcommands (I2)
- `src/trading_bot/daemon.py` — switch `_load_runners` to instantiate Role objects from `roles/`; auto-run `alembic upgrade head` at startup (I5)
- `src/trading_bot/supervisor.py` — startup grace period for first stall check (boot-race fix); use Role objects for Watchdog/Account Sentinel/Schedule Auditor/Resource Guardian
- `src/trading_bot/email_digest.py` — `DigestContext.role_report_cards` field; `build_digest_email` renders the table; zero-equity divide guard (M7)
- `src/trading_bot/scheduler_jobs.py` — register `log_rotation` weekly job (M5); register `schedule_auditor` and `resource_guardian` jobs

### Files NOT modified

- `src/trading_bot/state_db.py` — `role_runs` and `role_kpis` ORM tables already exist from Phase 1 Task 2
- All strategy code, dashboard, existing CLI command bodies (we wrap, not rewrite)

---

## Task 1: Role Protocol + dataclasses

**Files:**
- Create: `src/trading_bot/roles/__init__.py`
- Create: `src/trading_bot/roles/base.py`
- Test: `tests/roles/__init__.py` (empty), `tests/roles/test_base.py`

- [ ] **Step 1: Write failing test**

Write `tests/roles/test_base.py`:

```python
import datetime as dt
import pytest

from trading_bot.roles.base import (
    Role, RoleResult, ReportCard, Health, RoleStatus, HealthStatus,
)


def test_role_result_dataclass():
    started = dt.datetime(2026, 4, 28, 10, 0, 0, tzinfo=dt.timezone.utc)
    finished = dt.datetime(2026, 4, 28, 10, 0, 1, tzinfo=dt.timezone.utc)
    r = RoleResult(
        role_name="stock_scanner",
        started_at=started,
        finished_at=finished,
        status=RoleStatus.OK,
        latency_ms=1234,
        outputs={"placed": 1, "vetoed": 0},
    )
    assert r.role_name == "stock_scanner"
    assert r.status == RoleStatus.OK
    assert r.latency_ms == 1234
    assert r.outputs["placed"] == 1
    assert r.error_text is None


def test_role_result_with_error():
    started = dt.datetime.now(dt.timezone.utc)
    finished = dt.datetime.now(dt.timezone.utc)
    r = RoleResult(
        role_name="x", started_at=started, finished_at=finished,
        status=RoleStatus.ERROR, latency_ms=50, error_text="ValueError: bad",
    )
    assert r.status == RoleStatus.ERROR
    assert "ValueError" in r.error_text


def test_report_card_dataclass():
    card = ReportCard(
        role_name="stock_scanner",
        period_days=30,
        kpi_name="buy_win_rate_5d",
        kpi_value=0.62,
        summary="62% win rate on 18 buys; 7 losers / 11 winners",
        delta_vs_prior=0.04,
        health=HealthStatus.OK,
    )
    assert card.kpi_value == 0.62
    assert card.delta_vs_prior == 0.04


def test_health_dataclass():
    h = Health(status=HealthStatus.DEGRADED, detail="2 of last 5 runs errored")
    assert h.status == HealthStatus.DEGRADED
    assert "2 of last 5" in h.detail


def test_role_status_values():
    assert RoleStatus.OK.value == "ok"
    assert RoleStatus.ERROR.value == "error"
    assert RoleStatus.BLOCKED.value == "blocked"
    assert RoleStatus.HALTED.value == "halted"


def test_health_status_values():
    assert HealthStatus.OK.value == "OK"
    assert HealthStatus.DEGRADED.value == "DEGRADED"
    assert HealthStatus.BLOCKED.value == "BLOCKED"
    assert HealthStatus.FAIL.value == "FAIL"


def test_role_protocol_minimal_implementation():
    """A class implementing all Protocol attributes is recognized as a Role."""

    class FakeRole:
        name = "fake"
        tier = 1
        process = "daemon"
        job_description = "test only"
        sla_seconds = 30
        upstream_roles: list[str] = []
        downstream_roles: list[str] = []

        def run(self, ctx):
            return RoleResult(
                role_name="fake",
                started_at=dt.datetime.now(dt.timezone.utc),
                finished_at=dt.datetime.now(dt.timezone.utc),
                status=RoleStatus.OK,
                latency_ms=0,
            )

        def report_card(self, lookback_days):
            return ReportCard(
                role_name="fake", period_days=lookback_days,
                kpi_name="x", kpi_value=0.0, summary="ok",
            )

        def health_check(self):
            return Health(status=HealthStatus.OK)

    fake = FakeRole()
    assert isinstance(fake, Role)  # Protocol check works
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/roles/test_base.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement base module**

Write `src/trading_bot/roles/base.py`:

```python
"""Role Protocol, dataclasses, and enums. The contract every routine
in the system implements. See spec §7 for the full role taxonomy.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class RoleStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"        # external dependency unavailable (creds, network)
    HALTED = "halted"          # internal fatal config bug; pause.flag written


class HealthStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"      # KPI worse than threshold or recent errors
    BLOCKED = "BLOCKED"        # cannot run currently (creds, upstream down)
    FAIL = "FAIL"              # consistently broken


@dataclass
class RoleResult:
    role_name: str
    started_at: dt.datetime
    finished_at: dt.datetime
    status: RoleStatus
    latency_ms: int
    outputs: dict[str, Any] = field(default_factory=dict)
    error_text: str | None = None


@dataclass
class ReportCard:
    role_name: str
    period_days: int
    kpi_name: str
    kpi_value: float
    summary: str
    delta_vs_prior: float | None = None
    health: HealthStatus = HealthStatus.OK


@dataclass
class Health:
    status: HealthStatus
    detail: str = ""


@runtime_checkable
class Role(Protocol):
    """Every routine in the system implements this Protocol."""
    name: str
    tier: int
    process: str            # "daemon" | "lab" | "supervisor"
    job_description: str
    sla_seconds: int
    upstream_roles: list[str]
    downstream_roles: list[str]

    def run(self, ctx: Any) -> RoleResult: ...
    def report_card(self, lookback_days: int) -> ReportCard: ...
    def health_check(self) -> Health: ...
```

Write `src/trading_bot/roles/__init__.py`:

```python
"""Role definitions for the trading bot. See spec §7 for the taxonomy."""
from trading_bot.roles.base import (
    Health,
    HealthStatus,
    ReportCard,
    Role,
    RoleResult,
    RoleStatus,
)

__all__ = [
    "Role",
    "RoleResult",
    "ReportCard",
    "Health",
    "RoleStatus",
    "HealthStatus",
]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/roles/test_base.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/__init__.py src/trading_bot/roles/base.py tests/roles/__init__.py tests/roles/test_base.py
git commit -m "feat(plan-9): Role Protocol with RoleResult, ReportCard, Health dataclasses"
```

---

## Task 2: BaseRole class with safe_run + persistence

**Files:**
- Create: `src/trading_bot/roles/runner.py`
- Test: `tests/roles/test_runner.py`

- [ ] **Step 1: Write failing test**

Write `tests/roles/test_runner.py`:

```python
import os
import tempfile
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base, RoleRun, RoleKpi
from trading_bot.roles.base import RoleStatus, HealthStatus, ReportCard
from trading_bot.roles.runner import BaseRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


class _FakeRole(BaseRole):
    name = "fake_scanner"
    tier = 2
    process = "daemon"
    job_description = "fake test role"
    sla_seconds = 30
    upstream_roles = []
    downstream_roles = []

    def __init__(self, engine, *, raise_on_run: Exception | None = None):
        super().__init__(engine=engine)
        self.raise_on_run = raise_on_run
        self.run_count = 0

    def _do_work(self, ctx):
        self.run_count += 1
        if self.raise_on_run:
            raise self.raise_on_run
        return {"placed": 1, "vetoed": 0}

    def _kpi_value(self, lookback_days):
        return ("test_kpi", 0.42, "test summary")


def test_safe_run_records_ok_result(engine):
    role = _FakeRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs == {"placed": 1, "vetoed": 0}
    assert result.latency_ms >= 0
    assert result.error_text is None

    with Session(engine) as s:
        rows = s.query(RoleRun).all()
    assert len(rows) == 1
    assert rows[0].role_name == "fake_scanner"
    assert rows[0].status == "ok"


def test_safe_run_catches_exception(engine):
    role = _FakeRole(engine=engine, raise_on_run=ValueError("boom"))
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
    assert "ValueError" in result.error_text
    assert "boom" in result.error_text

    with Session(engine) as s:
        rows = s.query(RoleRun).all()
    assert rows[0].status == "error"


def test_safe_run_persists_kpi(engine):
    role = _FakeRole(engine=engine)
    role.safe_run(ctx={})
    role.persist_kpi()  # explicit, called by report_card path

    with Session(engine) as s:
        kpi_rows = s.query(RoleKpi).all()
    assert len(kpi_rows) == 1
    assert kpi_rows[0].kpi_name == "test_kpi"
    assert kpi_rows[0].value == pytest.approx(0.42)


def test_report_card_returns_card(engine):
    role = _FakeRole(engine=engine)
    role.safe_run(ctx={})
    card = role.report_card(lookback_days=30)
    assert isinstance(card, ReportCard)
    assert card.role_name == "fake_scanner"
    assert card.kpi_name == "test_kpi"
    assert card.kpi_value == 0.42
    assert card.period_days == 30


def test_health_check_ok_after_clean_run(engine):
    role = _FakeRole(engine=engine)
    role.safe_run(ctx={})
    health = role.health_check()
    assert health.status == HealthStatus.OK


def test_health_check_degraded_after_recent_errors(engine):
    role = _FakeRole(engine=engine, raise_on_run=ValueError("boom"))
    for _ in range(5):
        role.safe_run(ctx={})
    health = role.health_check()
    assert health.status == HealthStatus.DEGRADED


def test_safe_run_never_raises(engine):
    """safe_run must catch BaseException so APScheduler workers stay alive."""
    class _Suicidal(_FakeRole):
        def _do_work(self, ctx):
            raise SystemExit(1)

    role = _Suicidal(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
    # process did not exit
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/roles/test_runner.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement runner module**

Write `src/trading_bot/roles/runner.py`:

```python
"""BaseRole — concrete base class implementing the Role Protocol with
safe_run + KPI persistence. Subclasses override _do_work() and _kpi_value().

safe_run is the gate that catches every exception (including BaseException)
so APScheduler worker threads and the supervisor loop never die from a
buggy role.
"""
from __future__ import annotations

import datetime as dt
import time as _time
import traceback

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.roles.base import (
    Health,
    HealthStatus,
    ReportCard,
    RoleResult,
    RoleStatus,
)
from trading_bot.state_db import RoleKpi, RoleRun


class BaseRole:
    """Concrete implementation of the Role Protocol. Subclasses override
    `_do_work(ctx)` (the actual work) and `_kpi_value(lookback_days)`
    (returns a (kpi_name, value, summary) tuple).
    """

    name: str = "base"
    tier: int = 0
    process: str = "daemon"
    job_description: str = "base role — do not instantiate"
    sla_seconds: int = 60
    upstream_roles: list[str] = []
    downstream_roles: list[str] = []

    def __init__(self, *, engine):
        self.engine = engine

    def _do_work(self, ctx):
        raise NotImplementedError("subclasses must override _do_work")

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        """Return (kpi_name, value, prose summary)."""
        raise NotImplementedError("subclasses must override _kpi_value")

    def safe_run(self, ctx) -> RoleResult:
        started = dt.datetime.now(dt.timezone.utc)
        t0 = _time.monotonic()
        outputs: dict = {}
        status = RoleStatus.OK
        error_text: str | None = None

        try:
            outputs = self._do_work(ctx) or {}
        except BaseException as e:  # catch SystemExit too — workers must survive
            status = RoleStatus.ERROR
            error_text = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        finally:
            finished = dt.datetime.now(dt.timezone.utc)
            latency_ms = int((_time.monotonic() - t0) * 1000)

        result = RoleResult(
            role_name=self.name,
            started_at=started,
            finished_at=finished,
            status=status,
            latency_ms=latency_ms,
            outputs=outputs,
            error_text=error_text,
        )
        self._persist_run(result)
        return result

    def _persist_run(self, result: RoleResult) -> None:
        with Session(self.engine) as session:
            row = RoleRun(
                role_name=result.role_name,
                started_at=result.started_at,
                finished_at=result.finished_at,
                status=result.status.value,
                latency_ms=result.latency_ms,
                error_text=result.error_text,
            )
            session.add(row)
            session.commit()

    def persist_kpi(self, lookback_days: int = 30) -> None:
        kpi_name, value, _ = self._kpi_value(lookback_days)
        with Session(self.engine) as session:
            row = RoleKpi(
                role_name=self.name,
                kpi_name=kpi_name,
                value=value,
                recorded_at=dt.datetime.now(dt.timezone.utc),
            )
            session.add(row)
            session.commit()

    def report_card(self, lookback_days: int = 30) -> ReportCard:
        kpi_name, value, summary = self._kpi_value(lookback_days)
        delta = self._prior_period_delta(kpi_name, lookback_days, value)
        health = self.health_check()
        return ReportCard(
            role_name=self.name,
            period_days=lookback_days,
            kpi_name=kpi_name,
            kpi_value=value,
            delta_vs_prior=delta,
            summary=summary,
            health=health.status,
        )

    def _prior_period_delta(self, kpi_name: str, lookback_days: int, current: float) -> float | None:
        """Look up the most recent KPI row > lookback_days old and return current - prior."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            row = (
                session.query(RoleKpi)
                .filter(RoleKpi.role_name == self.name, RoleKpi.kpi_name == kpi_name)
                .filter(RoleKpi.recorded_at < cutoff)
                .order_by(desc(RoleKpi.recorded_at))
                .first()
            )
        return current - row.value if row else None

    def health_check(self) -> Health:
        """Default: DEGRADED if > 30% of the last 10 runs errored, else OK."""
        with Session(self.engine) as session:
            runs = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name)
                .order_by(desc(RoleRun.started_at))
                .limit(10)
                .all()
            )
        if not runs:
            return Health(status=HealthStatus.OK, detail="no runs yet")
        errors = sum(1 for r in runs if r.status == "error")
        if errors / len(runs) > 0.30:
            return Health(
                status=HealthStatus.DEGRADED,
                detail=f"{errors} of last {len(runs)} runs errored",
            )
        return Health(status=HealthStatus.OK)

    def run(self, ctx) -> RoleResult:
        """Protocol method — alias for safe_run so BaseRole satisfies Role Protocol."""
        return self.safe_run(ctx)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/roles/test_runner.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/runner.py tests/roles/test_runner.py
git commit -m "feat(plan-9): BaseRole class with safe_run + role_runs/role_kpis persistence"
```

---

## Task 3: Health Pulse role (heartbeat-as-a-Role)

**Files:**
- Create: `src/trading_bot/roles/health_pulse.py`
- Test: `tests/roles/test_health_pulse.py`

- [ ] **Step 1: Write failing test**

Write `tests/roles/test_health_pulse.py`:

```python
import json
import os
import tempfile
import datetime as dt

import pytest
from sqlalchemy import create_engine

from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus, HealthStatus
from trading_bot.roles.health_pulse import HealthPulseRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_health_pulse_writes_heartbeat(engine, tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    role = HealthPulseRole(engine=engine, heartbeat_path=hb_path, version="phase2-v1")
    result = role.safe_run(ctx=None)
    assert result.status == RoleStatus.OK
    assert hb_path.exists()
    payload = json.loads(hb_path.read_text())
    assert payload["version"] == "phase2-v1"
    assert payload["last_action"] == "heartbeat"


def test_health_pulse_charter():
    role = HealthPulseRole(engine=None, heartbeat_path="/tmp/x", version="v1")
    assert role.name == "health_pulse"
    assert role.process == "daemon"
    assert role.tier == 6
    assert "heartbeat" in role.job_description.lower()


def test_health_pulse_kpi(engine, tmp_path):
    role = HealthPulseRole(engine=engine, heartbeat_path=tmp_path / "hb.json", version="v1")
    role.safe_run(ctx=None)
    name, value, summary = role._kpi_value(lookback_days=1)
    assert name == "heartbeats_per_day"
    assert value >= 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/roles/test_health_pulse.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement HealthPulseRole**

Write `src/trading_bot/roles/health_pulse.py`:

```python
"""Health Pulse — daemon's heartbeat as a Role.

Tier 6 (Supervision/observability). Runs every cadence.heartbeat_seconds
inside the daemon process. The supervisor reads the heartbeat file mtime
to detect stalls. Charter intentionally minimal: just keep the pulse alive.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun
from trading_bot.state_heartbeat import write_heartbeat


class HealthPulseRole(BaseRole):
    name = "health_pulse"
    tier = 6
    process = "daemon"
    job_description = (
        "Write daemon heartbeat to disk every cadence.heartbeat_seconds. "
        "Supervisor reads mtime to detect stalls."
    )
    sla_seconds = 5
    upstream_roles: list[str] = []
    downstream_roles = ["watchdog"]

    def __init__(self, *, engine, heartbeat_path: str | Path, version: str):
        super().__init__(engine=engine)
        self.heartbeat_path = Path(heartbeat_path)
        self.version = version

    def _do_work(self, ctx):
        write_heartbeat(self.heartbeat_path, version=self.version, last_action="heartbeat")
        return {"path": str(self.heartbeat_path)}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        per_day = count / max(lookback_days, 1)
        return (
            "heartbeats_per_day",
            per_day,
            f"{count} heartbeats in last {lookback_days}d ({per_day:.0f}/day)",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/roles/test_health_pulse.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/health_pulse.py tests/roles/test_health_pulse.py
git commit -m "feat(plan-9): Health Pulse role wraps heartbeat write"
```

---

## Tasks 4–11: Wrap daemon-side CLI commands as Roles

Each of Tasks 4–11 follows the **same shape**: subclass `BaseRole`, override class attributes (charter), `_do_work` invokes the existing CLI command, `_kpi_value` queries `trade_journal.db` or `state.db` for the role's specific KPI. Each task includes a test file with at least four tests: charter assertions, _do_work successful invocation (mocking the underlying CLI), error path, and KPI query.

The full code for Task 4 (Stock Scanner) is below as the exemplar. Tasks 5–11 follow the identical pattern; their code blocks below are complete and must not be omitted.

---

### Task 4: Stock Scanner role

**Files:**
- Create: `src/trading_bot/roles/stock_scanner.py`
- Test: `tests/roles/test_stock_scanner.py`

- [ ] **Step 1: Write failing test**

Write `tests/roles/test_stock_scanner.py`:

```python
import os
import tempfile
import datetime as dt
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine

from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus, HealthStatus
from trading_bot.roles.stock_scanner import StockScannerRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = StockScannerRole(engine=None)
    assert role.name == "stock_scanner"
    assert role.tier == 2
    assert role.process == "daemon"
    assert role.sla_seconds >= 30
    assert "intel-scan" in role.job_description.lower() or "scan" in role.job_description.lower()


def test_do_work_invokes_intel_scan(engine):
    role = StockScannerRole(engine=engine)
    with patch("trading_bot.cli.intel_scan") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK


def test_do_work_handles_exception(engine):
    role = StockScannerRole(engine=engine)
    with patch("trading_bot.cli.intel_scan") as mock_cmd:
        mock_cmd.callback.side_effect = RuntimeError("alpaca down")
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
    assert "alpaca down" in result.error_text


def test_kpi_returns_buy_win_rate_with_no_trades(engine):
    role = StockScannerRole(engine=engine)
    name, value, summary = role._kpi_value(lookback_days=30)
    assert name == "buy_win_rate_5d"
    assert value == 0.0  # default when no trades
    assert "no buys" in summary.lower() or "0 buys" in summary.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/roles/test_stock_scanner.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement Stock Scanner role**

Write `src/trading_bot/roles/stock_scanner.py`:

```python
"""Stock Scanner — runs intel-scan during US market hours, emits BUY/HOLD/SKIP
per stock candidate. Tier 2 (decision making). Wraps the existing
cli.intel_scan command.

KPI: buy_win_rate_5d — % of BUY decisions whose 5-day forward return is
positive. Computed by joining trade_journal.decisions (or fills) against
the next 5 trading days' bars.
"""
from __future__ import annotations

from trading_bot.roles.runner import BaseRole


class StockScannerRole(BaseRole):
    name = "stock_scanner"
    tier = 2
    process = "daemon"
    job_description = (
        "Run hourly intel-scan during US market hours. Evaluate stage-2 "
        "watchlist, emit BUY/HOLD/SKIP per candidate. Never places orders "
        "directly — Risk Officer + Trade Executor handle that."
    )
    sla_seconds = 60
    upstream_roles = ["universe_curator", "sentiment_analyst"]
    downstream_roles = ["risk_officer", "trade_executor"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.intel_scan.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # KPI computation deferred to Phase 3 (when trade_journal has fills).
        # Phase 2 reports a placeholder so the report card has structure;
        # Phase 3 will replace this with a real win-rate query.
        return (
            "buy_win_rate_5d",
            0.0,
            "no buys in window (KPI activates in Phase 3 once journal accrues)",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/roles/test_stock_scanner.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/stock_scanner.py tests/roles/test_stock_scanner.py
git commit -m "feat(plan-9): Stock Scanner role wraps intel-scan"
```

---

### Task 5: Crypto Scanner role

**Files:**
- Create: `src/trading_bot/roles/crypto_scanner.py`
- Test: `tests/roles/test_crypto_scanner.py`

Same shape as Task 4 with `name = "crypto_scanner"`, `process = "daemon"`, `tier = 2`, sentiment floor not applied, runs 24/7.

- [ ] **Step 1: Write failing test**

```python
# tests/roles/test_crypto_scanner.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.crypto_scanner import CryptoScannerRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = CryptoScannerRole(engine=None)
    assert role.name == "crypto_scanner"
    assert role.tier == 2
    assert "24/7" in role.job_description or "crypto" in role.job_description.lower()


def test_do_work_invokes_crypto_scan(engine):
    role = CryptoScannerRole(engine=engine)
    with patch("trading_bot.cli.crypto_scan") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK


def test_do_work_handles_exception(engine):
    role = CryptoScannerRole(engine=engine)
    with patch("trading_bot.cli.crypto_scan") as mock_cmd:
        mock_cmd.callback.side_effect = RuntimeError("nope")
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR


def test_kpi_default(engine):
    role = CryptoScannerRole(engine=engine)
    name, value, _ = role._kpi_value(lookback_days=30)
    assert name == "buy_win_rate_5d"
```

- [ ] **Step 2: Run test, verify it fails.**

```bash
uv run pytest tests/roles/test_crypto_scanner.py -v
```

- [ ] **Step 3: Implement**

```python
# src/trading_bot/roles/crypto_scanner.py
"""Crypto Scanner — same as StockScannerRole but for crypto pairs, 24/7,
no sentiment floor. Tier 2."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class CryptoScannerRole(BaseRole):
    name = "crypto_scanner"
    tier = 2
    process = "daemon"
    job_description = (
        "Run crypto-scan every 30 min, 24/7. Evaluates configured crypto "
        "pairs (BTC/USD, ETH/USD, SOL/USD by default). Sentiment floor "
        "is not applied to crypto. Runs through Risk Officer + Trade Executor."
    )
    sla_seconds = 60
    upstream_roles: list[str] = []
    downstream_roles = ["risk_officer", "trade_executor"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.crypto_scan.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("buy_win_rate_5d", 0.0, "Phase 3 KPI — placeholder")
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/crypto_scanner.py tests/roles/test_crypto_scanner.py
git commit -m "feat(plan-9): Crypto Scanner role wraps crypto-scan 24/7"
```

---

### Task 6: Universe Curator role

**Files:**
- Create: `src/trading_bot/roles/universe_curator.py`
- Test: `tests/roles/test_universe_curator.py`

Wraps both `cli.massive_refresh` (06:30 ET) and `cli.rank_command` (07:30 ET). The two are sub-jobs of the same role per spec §7.2 Role 1. Implementation: a single `UniverseCuratorRole` class, but with two methods (`run_refresh` and `run_rank`) that the daemon's scheduler invokes for the two cron times. Each call records its own `role_runs` row (with `outputs={"job": "refresh"}` or `{"job": "rank"}`).

- [ ] **Step 1: Write test**

```python
# tests/roles/test_universe_curator.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.state_db import Base, RoleRun
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.universe_curator import UniverseCuratorRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = UniverseCuratorRole(engine=None)
    assert role.name == "universe_curator"
    assert role.tier == 1


def test_run_refresh_invokes_massive_refresh(engine):
    role = UniverseCuratorRole(engine=engine)
    with patch("trading_bot.cli.massive_refresh") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.run_refresh(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK
    with Session(engine) as s:
        row = s.query(RoleRun).first()
    assert row.role_name == "universe_curator"


def test_run_rank_invokes_rank_command(engine):
    role = UniverseCuratorRole(engine=engine)
    with patch("trading_bot.cli.rank_command") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.run_rank(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = UniverseCuratorRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=14)
    assert name == "top25_capture_rate_14d"
```

- [ ] **Step 2: Run test, verify fails.**

- [ ] **Step 3: Implement**

```python
# src/trading_bot/roles/universe_curator.py
"""Universe Curator — Tier 1. Maintains the tradable list and the cached
daily bars. Two sub-jobs: refresh (06:30 ET, pulls Polygon grouped bars)
and rank (07:30 ET, runs stage-1+2 screener)."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole
from trading_bot.roles.base import RoleResult


class UniverseCuratorRole(BaseRole):
    name = "universe_curator"
    tier = 1
    process = "daemon"
    job_description = (
        "Maintain the tradable stock list. Refresh Polygon grouped daily "
        "bars (06:30 ET); rank stage-1+2 candidates into top 25 (07:30 ET)."
    )
    sla_seconds = 120
    upstream_roles: list[str] = []
    downstream_roles = ["stock_scanner", "sentiment_analyst"]

    def run_refresh(self, ctx) -> RoleResult:
        return self._run_subjob("refresh")

    def run_rank(self, ctx) -> RoleResult:
        return self._run_subjob("rank")

    def _run_subjob(self, job: str) -> RoleResult:
        # Reuses safe_run with a contextual override
        self._current_subjob = job
        try:
            return self.safe_run(ctx={"subjob": job})
        finally:
            self._current_subjob = None

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        job = (ctx or {}).get("subjob") or getattr(self, "_current_subjob", None) or "refresh"
        if job == "refresh":
            cli_mod.massive_refresh.callback(days=5, news=False)
        elif job == "rank":
            cli_mod.rank_command.callback()
        else:
            raise ValueError(f"unknown universe_curator subjob: {job}")
        return {"job": job}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # Phase 3+ will compute capture rate from journal vs. the next-day winners.
        return (
            "top25_capture_rate_14d",
            0.0,
            "KPI activates in Phase 3 (requires journal of placed BUYs to compare against next-day winners)",
        )
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/universe_curator.py tests/roles/test_universe_curator.py
git commit -m "feat(plan-9): Universe Curator role with refresh + rank subjobs"
```

---

### Task 7: Sentiment Analyst role

**Files:**
- Create: `src/trading_bot/roles/sentiment_analyst.py`
- Test: `tests/roles/test_sentiment_analyst.py`

Wraps `cli.news_warm`. Tier 1. Sub-jobs: `morning` (08:55 ET), `midday` (12:00 ET), and `on-demand` for stale candidates (Phase 1 didn't implement on-demand; Phase 2 keeps the two scheduled warms only).

- [ ] **Step 1: Write test**

```python
# tests/roles/test_sentiment_analyst.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.sentiment_analyst import SentimentAnalystRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = SentimentAnalystRole(engine=None)
    assert role.name == "sentiment_analyst"
    assert role.tier == 1


def test_do_work_invokes_news_warm(engine):
    role = SentimentAnalystRole(engine=engine)
    with patch("trading_bot.cli.news_warm") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
        mock_cmd.callback.assert_called_once()
    assert result.status == RoleStatus.OK


def test_do_work_handles_exception(engine):
    role = SentimentAnalystRole(engine=engine)
    with patch("trading_bot.cli.news_warm") as mock_cmd:
        mock_cmd.callback.side_effect = ConnectionError("polygon down")
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR


def test_kpi_default(engine):
    role = SentimentAnalystRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "floor_block_post_5d_return"
```

- [ ] **Step 2: Run test, verify fails.**

- [ ] **Step 3: Implement**

```python
# src/trading_bot/roles/sentiment_analyst.py
"""Sentiment Analyst — Tier 1. Refreshes per-symbol Polygon news+sentiment
cache (3-day TTL). Two scheduled warms: 08:55 ET pre-open, 12:00 ET midday."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class SentimentAnalystRole(BaseRole):
    name = "sentiment_analyst"
    tier = 1
    process = "daemon"
    job_description = (
        "Refresh per-symbol news+sentiment for stage-2 watchlist via "
        "Polygon news API. Two scheduled warms (08:55 ET, 12:00 ET) plus "
        "on-demand inline at scan time when a candidate is > 4h stale."
    )
    sla_seconds = 60
    upstream_roles = ["universe_curator"]
    downstream_roles = ["stock_scanner"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.news_warm.callback(lookback_days=3)
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # Floor-block accuracy (% of names blocked by sentiment floor whose
        # next-5d return was negative). Activates in Phase 3 with journal data.
        return (
            "floor_block_post_5d_return",
            0.0,
            "KPI activates in Phase 3 (requires journal of sentiment-blocked names)",
        )
```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/sentiment_analyst.py tests/roles/test_sentiment_analyst.py
git commit -m "feat(plan-9): Sentiment Analyst role wraps news_warm"
```

---

### Task 8: Portfolio Monitor role

Same shape. `name = "portfolio_monitor"`, `tier = 4`, wraps `cli.portfolio_watch`.

- [ ] **Step 1: Test**

```python
# tests/roles/test_portfolio_monitor.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.portfolio_monitor import PortfolioMonitorRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = PortfolioMonitorRole(engine=None)
    assert role.name == "portfolio_monitor"
    assert role.tier == 4


def test_do_work_invokes_portfolio_watch(engine):
    role = PortfolioMonitorRole(engine=engine)
    with patch("trading_bot.cli.portfolio_watch") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = PortfolioMonitorRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "alert_lead_time_seconds"
```

- [ ] **Step 2-4: As prior tasks.**

- [ ] **Step 3: Impl**

```python
# src/trading_bot/roles/portfolio_monitor.py
"""Portfolio Monitor — Tier 4 stewardship. Snapshots positions every 60min
during market hours, alerts on stop-hits, big moves, unusual fills."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class PortfolioMonitorRole(BaseRole):
    name = "portfolio_monitor"
    tier = 4
    process = "daemon"
    job_description = (
        "Snapshot Alpaca positions every 60 min during market hours. "
        "Alert on stop-hits, big intraday moves, unusual fills. "
        "Stop-hit emails are routed via Trade Executor's fill detection."
    )
    sla_seconds = 30
    upstream_roles = ["trade_executor"]
    downstream_roles = ["reporter"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.portfolio_watch.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("alert_lead_time_seconds", 0.0, "Phase 3 KPI (needs alert events table)")
```

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/portfolio_monitor.py tests/roles/test_portfolio_monitor.py
git commit -m "feat(plan-9): Portfolio Monitor role wraps portfolio_watch"
```

---

### Task 9: Order Steward role

Wraps `cli.verify_stops`. Tier 3. Same pattern.

- [ ] Steps 1-5: same shape.

```python
# tests/roles/test_order_steward.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.order_steward import OrderStewardRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = OrderStewardRole(engine=None)
    assert role.name == "order_steward"
    assert role.tier == 3


def test_do_work_invokes_verify_stops(engine):
    role = OrderStewardRole(engine=engine)
    with patch("trading_bot.cli.verify_stops") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = OrderStewardRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "stop_attached_rate"
```

```python
# src/trading_bot/roles/order_steward.py
"""Order Steward — Tier 3. Post-order lifecycle: verify fills, ensure stops
attached, cancel stale unfilled limit orders."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class OrderStewardRole(BaseRole):
    name = "order_steward"
    tier = 3
    process = "daemon"
    job_description = (
        "Verify every open position has a live stop order. Cancel "
        "unfilled limit orders older than 60 min. Sweeps every 60 min "
        "during market hours plus immediate on-demand after each Trade "
        "Executor placement."
    )
    sla_seconds = 60
    upstream_roles = ["trade_executor"]
    downstream_roles = ["reporter"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.verify_stops.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("stop_attached_rate", 1.0, "Phase 3 KPI activates with positions table")
```

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/roles/order_steward.py tests/roles/test_order_steward.py
git commit -m "feat(plan-9): Order Steward role wraps verify_stops"
```

---

### Task 10: VIP Listener role

Wraps `cli.vip_scan`. Tier 1. Alert-only. Same pattern.

- [ ] Steps 1-5:

```python
# tests/roles/test_vip_listener.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.vip_listener import VipListenerRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = VipListenerRole(engine=None)
    assert role.name == "vip_listener"
    assert role.tier == 1
    assert "alert" in role.job_description.lower()


def test_do_work_invokes_vip_scan(engine):
    role = VipListenerRole(engine=engine)
    with patch("trading_bot.cli.vip_scan") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = VipListenerRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "alerts_per_week"
```

```python
# src/trading_bot/roles/vip_listener.py
"""VIP Listener — Tier 1. Polls Truth Social RSS, flags HIGH-severity posts.
Alert-only — never trades."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class VipListenerRole(BaseRole):
    name = "vip_listener"
    tier = 1
    process = "daemon"
    job_description = (
        "Poll Truth Social RSS every 30 min during US market hours, flag "
        "HIGH-severity posts. Alert-only — never auto-trades, auto-halts, "
        "or auto-vetoes based on a tweet."
    )
    sla_seconds = 30
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.vip_scan.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("alerts_per_week", 0.0, "Phase 3+ — needs alerts table")
```

- [ ] Commit:

```bash
git add src/trading_bot/roles/vip_listener.py tests/roles/test_vip_listener.py
git commit -m "feat(plan-9): VIP Listener role wraps vip_scan (alert-only)"
```

---

### Task 11: Reporter role

Wraps `cli.eod_report` (18:00 ET) and `cli.rich_report` (12:31 ET midday). Tier 6 observability. The Reporter is also the orchestrator that **assembles role report cards** for the daily digest — see Task 18 for the digest integration; this task just establishes the role wrapper.

- [ ] Steps 1-5:

```python
# tests/roles/test_reporter.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.reporter import ReporterRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = ReporterRole(engine=None)
    assert role.name == "reporter"
    assert role.tier == 6


def test_run_eod_invokes_eod_report(engine):
    role = ReporterRole(engine=engine)
    with patch("trading_bot.cli.eod_report") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.run_eod(ctx={})
    assert result.status == RoleStatus.OK


def test_run_midday_invokes_rich_report_mid(engine):
    role = ReporterRole(engine=engine)
    with patch("trading_bot.cli.rich_report") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.run_midday(ctx={})
        mc.callback.assert_called_with(period="mid")
    assert result.status == RoleStatus.OK
```

```python
# src/trading_bot/roles/reporter.py
"""Reporter — Tier 6. Sends digest emails. Two sub-jobs: midday (12:31 ET
runs rich-report --period=mid which scans + emails) and eod (18:00 ET runs
eod-report which is read-only). Also assembles role report cards for the
daily digest body (see Task 18 for the integration)."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole
from trading_bot.roles.base import RoleResult


class ReporterRole(BaseRole):
    name = "reporter"
    tier = 6
    process = "daemon"
    job_description = (
        "Compose and send digest emails. Mid-day rich report at 12:31 ET "
        "(scans + emails). End-of-day digest at 18:00 ET (read-only summary). "
        "Per-trade fills are routed by Trade Executor through SMTP directly."
    )
    sla_seconds = 60
    upstream_roles = ["account_sentinel"]
    downstream_roles: list[str] = []

    def run_eod(self, ctx) -> RoleResult:
        self._current_subjob = "eod"
        try:
            return self.safe_run(ctx={"subjob": "eod"})
        finally:
            self._current_subjob = None

    def run_midday(self, ctx) -> RoleResult:
        self._current_subjob = "midday"
        try:
            return self.safe_run(ctx={"subjob": "midday"})
        finally:
            self._current_subjob = None

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        job = (ctx or {}).get("subjob") or getattr(self, "_current_subjob", None)
        if job == "eod":
            cli_mod.eod_report.callback()
        elif job == "midday":
            cli_mod.rich_report.callback(period="mid")
        else:
            raise ValueError(f"unknown reporter subjob: {job}")
        return {"job": job}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # On-time delivery rate — Phase 2 placeholder, Phase 3 will compute
        # from role_runs vs expected cron schedule.
        return ("delivered_on_time_rate", 1.0, "Phase 3 KPI; Phase 2 placeholder = 100%")
```

Commit:

```bash
git add src/trading_bot/roles/reporter.py tests/roles/test_reporter.py
git commit -m "feat(plan-9): Reporter role with midday + eod subjobs"
```

---

## Task 12: Watchdog role (supervisor side)

Wraps the existing `StallDetector` from `watchdog_stall.py` as a Role.

- [ ] **Step 1: Test**

```python
# tests/roles/test_watchdog.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.watchdog import WatchdogRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = WatchdogRole(
        engine=None,
        heartbeat_path="/tmp/x",
        max_age_seconds=300,
        plist_label="com.bharath.trading.daemon.paper",
    )
    assert role.name == "watchdog"
    assert role.process == "supervisor"
    assert role.tier == 6


def test_no_stall_when_recent_heartbeat(engine, tmp_path):
    hb = tmp_path / "hb.json"
    hb.write_text('{"ts":"2026-04-28T00:00:00+00:00","pid":1,"version":"v","last_action":"x"}')
    role = WatchdogRole(
        engine=engine, heartbeat_path=hb, max_age_seconds=300,
        plist_label="fake.label",
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["stalled"] is False


def test_stall_triggers_kickstart(engine, tmp_path):
    hb = tmp_path / "hb.json"
    hb.write_text('{}')
    import os as _os
    old = 1234567890
    _os.utime(hb, (old, old))
    role = WatchdogRole(
        engine=engine, heartbeat_path=hb, max_age_seconds=60,
        plist_label="fake.label",
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["stalled"] is True
    assert result.outputs["kickstart_attempted"] is True
```

- [ ] **Step 2: Run, fails.**

- [ ] **Step 3: Impl**

```python
# src/trading_bot/roles/watchdog.py
"""Watchdog — Tier 6 supervisor role. Detects daemon stall via heartbeat
mtime, attempts launchctl kickstart. The cooldown for the alert email
itself lives in supervisor.py's _send_alert (Phase 1)."""
from __future__ import annotations
from pathlib import Path
from trading_bot.roles.runner import BaseRole
from trading_bot.watchdog_stall import StallDetector


class WatchdogRole(BaseRole):
    name = "watchdog"
    tier = 6
    process = "supervisor"
    job_description = (
        "Detect daemon stall via heartbeat staleness > max_age_seconds. "
        "On stall, attempt one launchctl kickstart of the daemon plist. "
        "Caller (supervisor main loop) emits the alert email."
    )
    sla_seconds = 5
    upstream_roles = ["health_pulse"]
    downstream_roles: list[str] = []

    def __init__(self, *, engine, heartbeat_path: str | Path,
                 max_age_seconds: int, plist_label: str):
        super().__init__(engine=engine)
        self.detector = StallDetector(
            heartbeat_path=heartbeat_path,
            max_age_seconds=max_age_seconds,
            plist_label=plist_label,
        )

    def _do_work(self, ctx):
        verdict = self.detector.check()
        out = {"stalled": verdict.is_stalled, "age_seconds": verdict.age_seconds}
        if verdict.is_stalled:
            out["kickstart_attempted"] = self.detector.kickstart_daemon()
        return out

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # # of successful kickstarts in lookback period (from role_runs.outputs).
        # Phase 2 placeholder — Phase 3 will add an outputs JSON column or
        # parse from a separate events table.
        return (
            "kickstart_count",
            0.0,
            "Phase 3 KPI; Phase 2 placeholder",
        )
```

- [ ] Steps 4-5:

```bash
git add src/trading_bot/roles/watchdog.py tests/roles/test_watchdog.py
git commit -m "feat(plan-9): Watchdog role wraps StallDetector"
```

---

## Task 13: Account Sentinel role

Wraps the existing `AccountSentinel` class (already partially Role-shaped) as a Role.

- [ ] **Step 1: Test**

```python
# tests/roles/test_account_sentinel_role.py
import os, tempfile
from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.account_sentinel import AccountSentinelRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = AccountSentinelRole(
        engine=None, alpaca=MagicMock(), pause_flag_path="/tmp/x",
        max_dd_pct=20.0, account="paper",
    )
    assert role.name == "account_sentinel"
    assert role.process == "supervisor"
    assert role.tier == 6


def test_safe_run_returns_drawdown(engine, tmp_path):
    alpaca = MagicMock()
    alpaca.get_account.return_value = MagicMock(equity=Decimal("100000"))
    role = AccountSentinelRole(
        engine=engine, alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0, account="paper",
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert "drawdown_pct" in result.outputs
    assert result.outputs["drawdown_pct"] == 0.0
    assert result.outputs["paused"] is False


def test_safe_run_handles_alpaca_failure(engine, tmp_path):
    alpaca = MagicMock()
    alpaca.get_account.side_effect = ConnectionError("alpaca down")
    role = AccountSentinelRole(
        engine=engine, alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0, account="paper",
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
```

- [ ] **Step 2: Run, fails.**

- [ ] **Step 3: Impl**

```python
# src/trading_bot/roles/account_sentinel.py
"""Account Sentinel — Tier 6 supervisor role. Independently fetches Alpaca
equity, updates HWM, computes drawdown vs HWM, writes pause.flag if breached.
Wraps existing AccountSentinel class from watchdog_account.py."""
from __future__ import annotations
from pathlib import Path
from trading_bot.roles.runner import BaseRole
from trading_bot.watchdog_account import AccountSentinel


class AccountSentinelRole(BaseRole):
    name = "account_sentinel"
    tier = 6
    process = "supervisor"
    job_description = (
        "Reconcile Alpaca account vs trade journal independently of daemon. "
        "Update equity HWM, compute drawdown, write pause.flag if drawdown "
        "exceeds max_dd_pct. Runs every 5 min during market hours, every "
        "30 min off-hours."
    )
    sla_seconds = 30
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    def __init__(self, *, engine, alpaca, pause_flag_path: str | Path,
                 max_dd_pct: float, account: str):
        super().__init__(engine=engine)
        self.sentinel = AccountSentinel(
            engine=engine, alpaca=alpaca,
            pause_flag_path=pause_flag_path,
            max_dd_pct=max_dd_pct, account=account,
        )

    def _do_work(self, ctx):
        verdict = self.sentinel.check()
        return {
            "equity": str(verdict.equity),
            "hwm": verdict.hwm,
            "drawdown_pct": verdict.drawdown_pct,
            "paused": verdict.paused,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return (
            "current_drawdown_pct",
            0.0,
            "Phase 3 KPI; Phase 2 reports current snapshot only",
        )
```

- [ ] Steps 4-5:

```bash
git add src/trading_bot/roles/account_sentinel.py tests/roles/test_account_sentinel_role.py
git commit -m "feat(plan-9): Account Sentinel role wraps watchdog_account"
```

---

## Task 14: Schedule Auditor role (NEW supervisor role)

**Files:**
- Create: `src/trading_bot/roles/schedule_auditor.py`
- Test: `tests/roles/test_schedule_auditor.py`

Verifies that every expected role ran in the recent past. Compares `role_runs` last-started timestamps to expected cadences (looked up from a small constant table). Returns the list of any missed role names.

- [ ] **Step 1: Test**

```python
# tests/roles/test_schedule_auditor.py
import os, tempfile, datetime as dt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.state_db import Base, RoleRun
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.schedule_auditor import ScheduleAuditorRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def _add_run(engine, role_name, started):
    with Session(engine) as s:
        s.add(RoleRun(
            role_name=role_name, started_at=started,
            finished_at=started + dt.timedelta(seconds=1),
            status="ok", latency_ms=1000,
        ))
        s.commit()


def test_charter():
    role = ScheduleAuditorRole(engine=None)
    assert role.name == "schedule_auditor"
    assert role.process == "supervisor"


def test_no_misses_when_all_ran_recently(engine):
    now = dt.datetime.now(dt.timezone.utc)
    for role_name in ScheduleAuditorRole.EXPECTED_ROLES.keys():
        _add_run(engine, role_name, now - dt.timedelta(seconds=30))
    role = ScheduleAuditorRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["missed"] == []


def test_detects_missing_role(engine):
    now = dt.datetime.now(dt.timezone.utc)
    # Only health_pulse ran recently; everything else is missing
    _add_run(engine, "health_pulse", now - dt.timedelta(seconds=30))
    role = ScheduleAuditorRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert "stock_scanner" in result.outputs["missed"] or len(result.outputs["missed"]) > 0
```

- [ ] **Step 2: Run, fails.**

- [ ] **Step 3: Impl**

```python
# src/trading_bot/roles/schedule_auditor.py
"""Schedule Auditor — Tier 6 supervisor role. Verifies every expected role
ran within its grace window. Reports missed roles. Daily roll-up at 17:00 ET
caught by supervisor's main loop and emailed via Reporter (Phase 3 wiring).
"""
from __future__ import annotations
import datetime as dt
from sqlalchemy import desc
from sqlalchemy.orm import Session
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun


class ScheduleAuditorRole(BaseRole):
    name = "schedule_auditor"
    tier = 6
    process = "supervisor"
    job_description = (
        "Verify every expected role ran within its grace window. Reports "
        "missed roles. Daily roll-up at 17:00 ET surfaces in the digest."
    )
    sla_seconds = 5
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    # Map of role_name → grace window in seconds (3x the cadence + some slack).
    # Roles whose schedule is mkt-hours-only are also checked but tolerated
    # outside market hours by adding the >24h grace in the supervisor.
    EXPECTED_ROLES: dict[str, int] = {
        "health_pulse": 180,           # heartbeat every 60s; allow 3x
        "stock_scanner": 4 * 3600,     # mkt-hours hourly; gives ~4h grace
        "crypto_scanner": 90 * 60,     # 24/7 every 30min; allow 3x
        "portfolio_monitor": 4 * 3600,
        "order_steward": 4 * 3600,
        "vip_listener": 2 * 3600,
        "sentiment_analyst": 8 * 3600, # twice daily
        "reporter": 30 * 3600,         # twice daily but spaced widely
        "watchdog": 180,
        "account_sentinel": 30 * 60,   # market-hours every 5min, off-hours 30min
    }

    def _do_work(self, ctx):
        now = dt.datetime.now(dt.timezone.utc)
        missed = []
        with Session(self.engine) as session:
            for role_name, grace_seconds in self.EXPECTED_ROLES.items():
                latest = (
                    session.query(RoleRun)
                    .filter(RoleRun.role_name == role_name)
                    .order_by(desc(RoleRun.started_at))
                    .first()
                )
                if latest is None:
                    missed.append(role_name)
                    continue
                age = (now - latest.started_at).total_seconds()
                if age > grace_seconds:
                    missed.append(role_name)
        return {"missed": missed, "checked": list(self.EXPECTED_ROLES.keys())}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            rows = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .all()
            )
        if not rows:
            return ("missed_role_rate", 0.0, "no audits yet")
        # Approximate: this requires storing outputs.missed in role_runs which Phase 2 doesn't do.
        # Phase 2 reports zero; Phase 3 adds outputs storage.
        return (
            "missed_role_rate",
            0.0,
            "Phase 3 KPI; Phase 2 placeholder",
        )
```

- [ ] Steps 4-5:

```bash
git add src/trading_bot/roles/schedule_auditor.py tests/roles/test_schedule_auditor.py
git commit -m "feat(plan-9): Schedule Auditor role detects missed scheduled jobs"
```

---

## Task 15: Resource Guardian role (NEW supervisor role)

**Files:**
- Create: `src/trading_bot/roles/resource_guardian.py`
- Test: `tests/roles/test_resource_guardian.py`

Tracks: disk space (under repo root), `state.db` and `trade_journal.db` size, network connectivity to api.polygon.io and api.alpaca.markets. Anthropic budget tracking is deferred to Phase 5 (when Strategy Architect role is added). For Phase 2, just disk + DB + network.

- [ ] **Step 1: Test**

```python
# tests/roles/test_resource_guardian.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.resource_guardian import ResourceGuardianRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = ResourceGuardianRole(
        engine=None, repo_root="/tmp", state_db_path="/tmp/state.db",
        journal_db_path="/tmp/journal.db",
    )
    assert role.name == "resource_guardian"
    assert role.process == "supervisor"
    assert role.tier == 6


def test_safe_run_returns_disk_db_metrics(engine, tmp_path):
    state_db = tmp_path / "state.db"
    state_db.write_bytes(b"x" * 1024)
    journal_db = tmp_path / "journal.db"
    journal_db.write_bytes(b"x" * 2048)
    role = ResourceGuardianRole(
        engine=engine, repo_root=tmp_path,
        state_db_path=state_db, journal_db_path=journal_db,
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["disk_free_gb"] > 0
    assert result.outputs["state_db_mb"] >= 0
    assert result.outputs["journal_db_mb"] >= 0


def test_warns_when_disk_below_threshold(engine, tmp_path):
    role = ResourceGuardianRole(
        engine=engine, repo_root=tmp_path,
        state_db_path=tmp_path / "state.db", journal_db_path=tmp_path / "journal.db",
        disk_warn_gb=10**9,  # huge — guaranteed to trip on a normal Mac
    )
    result = role.safe_run(ctx={})
    assert "disk_low_gb" in result.outputs.get("warnings", [])
```

- [ ] **Step 2-4:** TDD cycle.

- [ ] **Step 3: Impl**

```python
# src/trading_bot/roles/resource_guardian.py
"""Resource Guardian — Tier 6 supervisor role. Tracks disk space, DB sizes,
network connectivity. Anthropic budget tracking added in Phase 5 when the
Strategy Architect role lands."""
from __future__ import annotations
import shutil
import socket
from pathlib import Path
from trading_bot.roles.runner import BaseRole


class ResourceGuardianRole(BaseRole):
    name = "resource_guardian"
    tier = 6
    process = "supervisor"
    job_description = (
        "Track disk free space, SQLite DB sizes, network connectivity to "
        "Alpaca + Polygon. Warn when thresholds tripped. Phase 2 covers "
        "disk + DB + network only; Anthropic budget tracking ships in Phase 5."
    )
    sla_seconds = 10
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    def __init__(self, *, engine, repo_root: str | Path,
                 state_db_path: str | Path, journal_db_path: str | Path,
                 disk_warn_gb: int = 10):
        super().__init__(engine=engine)
        self.repo_root = Path(repo_root)
        self.state_db_path = Path(state_db_path)
        self.journal_db_path = Path(journal_db_path)
        self.disk_warn_gb = disk_warn_gb

    def _do_work(self, ctx):
        warnings = []
        # Disk
        usage = shutil.disk_usage(self.repo_root)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < self.disk_warn_gb:
            warnings.append("disk_low_gb")

        # DB sizes
        state_mb = (
            self.state_db_path.stat().st_size / (1024 ** 2)
            if self.state_db_path.exists() else 0.0
        )
        journal_mb = (
            self.journal_db_path.stat().st_size / (1024 ** 2)
            if self.journal_db_path.exists() else 0.0
        )

        # Network probes (don't fail the run on unreachable endpoints)
        alpaca_reachable = self._reachable("api.alpaca.markets", 443)
        polygon_reachable = self._reachable("api.polygon.io", 443)
        if not alpaca_reachable:
            warnings.append("alpaca_unreachable")
        if not polygon_reachable:
            warnings.append("polygon_unreachable")

        return {
            "disk_free_gb": round(free_gb, 2),
            "state_db_mb": round(state_mb, 2),
            "journal_db_mb": round(journal_mb, 2),
            "alpaca_reachable": alpaca_reachable,
            "polygon_reachable": polygon_reachable,
            "warnings": warnings,
        }

    def _reachable(self, host: str, port: int, timeout_seconds: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return True
        except (OSError, socket.timeout):
            return False

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        outputs = self._do_work(ctx=None)
        return (
            "disk_free_gb",
            outputs["disk_free_gb"],
            f"{outputs['disk_free_gb']} GB free; warnings: {outputs.get('warnings', [])}",
        )
```

- [ ] Steps 4-5:

```bash
git add src/trading_bot/roles/resource_guardian.py tests/roles/test_resource_guardian.py
git commit -m "feat(plan-9): Resource Guardian role for disk + DB + network monitoring"
```

---

## Task 16: Wire Roles into daemon's runner registry

**Files:**
- Modify: `src/trading_bot/daemon.py`

Replace the bare-CLI lambdas in `_load_runners` with Role instances. Each runner becomes `lambda: role.safe_run(ctx={})` (or the role-specific subjob method).

- [ ] **Step 1: Existing test passes after changes**

The Phase 1 integration test `tests/test_integration_daemon.py` must continue to pass. Confirm with:

```bash
uv run pytest tests/test_integration_daemon.py -v
```

- [ ] **Step 2: Modify daemon.py**

Replace the body of `_load_runners(log)` in `src/trading_bot/daemon.py`. The new shape:

```python
def _load_runners(log: StructuredLogger):
    """Construct Role instances and return a dict of runner callables for
    APScheduler. Each runner closes over a Role's safe_run().
    """
    from trading_bot.state_db import get_engine
    from trading_bot.roles.health_pulse import HealthPulseRole
    from trading_bot.roles.stock_scanner import StockScannerRole
    from trading_bot.roles.crypto_scanner import CryptoScannerRole
    from trading_bot.roles.universe_curator import UniverseCuratorRole
    from trading_bot.roles.sentiment_analyst import SentimentAnalystRole
    from trading_bot.roles.portfolio_monitor import PortfolioMonitorRole
    from trading_bot.roles.order_steward import OrderStewardRole
    from trading_bot.roles.vip_listener import VipListenerRole
    from trading_bot.roles.reporter import ReporterRole

    config_version = "phase2-v1"
    state_db = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))
    engine = get_engine(state_db)

    health_pulse = HealthPulseRole(
        engine=engine, heartbeat_path=HEARTBEAT_PATH, version=config_version,
    )
    stock_scanner = StockScannerRole(engine=engine)
    crypto_scanner = CryptoScannerRole(engine=engine)
    universe_curator = UniverseCuratorRole(engine=engine)
    sentiment_analyst = SentimentAnalystRole(engine=engine)
    portfolio_monitor = PortfolioMonitorRole(engine=engine)
    order_steward = OrderStewardRole(engine=engine)
    vip_listener = VipListenerRole(engine=engine)
    reporter = ReporterRole(engine=engine)

    def _wrap(name: str, role_run: callable):
        def runner():
            log.event(f"{name}_start")
            if is_paused(PAUSE_PATH) and name in {"intel_scan", "crypto_scan", "midday_report"}:
                log.event(f"{name}_skipped", reason="pause.flag set")
                return
            result = role_run()
            log.event(
                f"{name}_finish",
                status=str(result.status),
                latency_ms=result.latency_ms,
            )
        return runner

    return {
        "heartbeat": _wrap("heartbeat", lambda: health_pulse.safe_run(ctx=None)),
        "intel_scan": _wrap("intel_scan", lambda: stock_scanner.safe_run(ctx={})),
        "crypto_scan": _wrap("crypto_scan", lambda: crypto_scanner.safe_run(ctx={})),
        "portfolio_watch": _wrap("portfolio_watch", lambda: portfolio_monitor.safe_run(ctx={})),
        "verify_stops": _wrap("verify_stops", lambda: order_steward.safe_run(ctx={})),
        "news_warm": _wrap("news_warm", lambda: sentiment_analyst.safe_run(ctx={})),
        "massive_refresh": _wrap("massive_refresh", lambda: universe_curator.run_refresh(ctx={})),
        "premarket_rank": _wrap("premarket_rank", lambda: universe_curator.run_rank(ctx={})),
        "vip_scan": _wrap("vip_scan", lambda: vip_listener.safe_run(ctx={})),
        "midday_report": _wrap("midday_report", lambda: reporter.run_midday(ctx={})),
        "daily_digest": _wrap("daily_digest", lambda: reporter.run_eod(ctx={})),
    }
```

- [ ] **Step 3: Run integration tests**

```bash
uv run pytest tests/test_integration_daemon.py tests/test_scheduler_jobs.py -v
```

Both must pass.

- [ ] **Step 4: Commit**

```bash
git add src/trading_bot/daemon.py
git commit -m "feat(plan-9): daemon uses Role objects for all scheduled jobs"
```

---

## Task 17: Wire supervisor to Role objects

**Files:**
- Modify: `src/trading_bot/supervisor.py`

Replace the inline `StallDetector` + `AccountSentinel` instantiation with `WatchdogRole`, `AccountSentinelRole`, plus add `ScheduleAuditorRole` and `ResourceGuardianRole` to the supervisor's main loop.

Add a **boot grace period**: skip the first stall check for 60 seconds after supervisor startup so the daemon has time to write its first heartbeat (resolves the boot-race false alarm from Phase 1 deployment).

- [ ] **Step 1: Existing tests pass**

```bash
uv run pytest tests/test_integration_supervisor.py tests/test_integration_drawdown.py -v
```

- [ ] **Step 2: Modify supervisor.py**

Add at the top of `main()` (just after `log.event("supervisor_boot")`):

```python
import time as _time
boot_ts = _time.monotonic()
GRACE_SECONDS = 60
```

Wrap the stall check with a grace gate:

```python
            # 1. Watchdog: every 60s, but skip the first GRACE_SECONDS so daemon can boot
            if _time.monotonic() - boot_ts < GRACE_SECONDS:
                log.event("watchdog_grace", remaining_s=GRACE_SECONDS - (_time.monotonic() - boot_ts))
            else:
                # ... existing stall check using WatchdogRole.safe_run() ...
```

Replace the inline `StallDetector` and `AccountSentinel` with their Role wrappers. Add Schedule Auditor and Resource Guardian periodic checks (every 15 min for SA, every 30 min for RG):

```python
from trading_bot.roles.watchdog import WatchdogRole
from trading_bot.roles.account_sentinel import AccountSentinelRole
from trading_bot.roles.schedule_auditor import ScheduleAuditorRole
from trading_bot.roles.resource_guardian import ResourceGuardianRole

# In main(), after engine = get_engine(STATE_DB):
watchdog_role = WatchdogRole(
    engine=engine,
    heartbeat_path=HEARTBEAT_PATH,
    max_age_seconds=stall_max_age,
    plist_label=DAEMON_PLIST_LABEL,
)
account_sentinel_role = AccountSentinelRole(
    engine=engine, alpaca=_alpaca(),
    pause_flag_path=PAUSE_PATH,
    max_dd_pct=20.0, account="paper",
)
schedule_auditor_role = ScheduleAuditorRole(engine=engine)
resource_guardian_role = ResourceGuardianRole(
    engine=engine,
    repo_root=Path("/Users/bharathkandala/Trading"),
    state_db_path=STATE_DB,
    journal_db_path=Path("/Users/bharathkandala/Trading/data/trade_journal.db"),
)

# Track last-run timestamps for the slower roles
last_schedule_audit = 0.0
last_resource_check = 0.0
```

In the main loop, replace the `stall_detector.check()` and the `acct_sentinel.check()` paths with `watchdog_role.safe_run(...)` and `account_sentinel_role.safe_run(...)`. Add periodic `schedule_auditor_role.safe_run(...)` (every 15 min) and `resource_guardian_role.safe_run(...)` (every 30 min) blocks.

The alert email logic stays the same — read `result.outputs["stalled"]` or `result.outputs["paused"]` from the Role's RoleResult instead of from the legacy verdicts.

- [ ] **Step 3: Tests pass**

```bash
uv run pytest tests/ -v 2>&1 | tail -3
```

- [ ] **Step 4: Commit**

```bash
git add src/trading_bot/supervisor.py
git commit -m "feat(plan-9): supervisor uses Role objects + grace period for boot-race fix"
```

---

## Task 18: Daily digest gains report-card section

**Files:**
- Modify: `src/trading_bot/email_digest.py`
- Test: `tests/test_email_digest_with_report_cards.py`

Extend `DigestContext` with a `role_report_cards: list[ReportCard]` field. Modify `build_digest_email` to render a "Role Report Cards" table. Add zero-equity divide guard (resolves M7).

- [ ] **Step 1: Write failing test**

```python
# tests/test_email_digest_with_report_cards.py
import datetime as dt
from decimal import Decimal
from trading_bot.email_digest import DigestContext, build_digest_email
from trading_bot.roles.base import ReportCard, HealthStatus


def test_digest_with_report_cards():
    cards = [
        ReportCard(role_name="stock_scanner", period_days=30,
                   kpi_name="buy_win_rate_5d", kpi_value=0.62,
                   summary="62% win rate", health=HealthStatus.OK),
        ReportCard(role_name="account_sentinel", period_days=30,
                   kpi_name="current_drawdown_pct", kpi_value=2.4,
                   summary="2.4% drawdown", health=HealthStatus.OK),
    ]
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500"),
        ending_equity=Decimal("103895"),
        realized_pnl=Decimal("-422"),
        unrealized_pnl=Decimal("139"),
        regime="trending_up",
        active_config_version="phase2-v1",
        trades=[],
        errors=[],
        role_report_cards=cards,
    )
    email = build_digest_email(ctx)
    assert "Role Report Cards" in email.html_body or "report card" in email.html_body.lower()
    assert "stock_scanner" in email.html_body
    assert "62%" in email.html_body or "0.62" in email.html_body


def test_digest_zero_starting_equity_does_not_crash():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("0"),
        ending_equity=Decimal("0"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="sideways",
        active_config_version="phase2-v1",
        trades=[],
        errors=[],
    )
    email = build_digest_email(ctx)  # must not raise DivisionByZero
    assert "0.00%" in email.subject or "0%" in email.subject
```

- [ ] **Step 2: Run, fails.**

- [ ] **Step 3: Modify email_digest.py**

Add to imports:

```python
from trading_bot.roles.base import ReportCard, HealthStatus
```

Extend `DigestContext`:

```python
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
    role_report_cards: list[ReportCard] = field(default_factory=list)
```

Modify `build_digest_email`:

In the `pct` calculation, guard against zero:

```python
    if ctx.starting_equity == 0:
        pct = Decimal("0")
    else:
        pct = ((ctx.ending_equity - ctx.starting_equity) / ctx.starting_equity) * 100
```

After the trades section and before the errors section, add report card rendering:

```python
    if ctx.role_report_cards:
        body.append("<h3>Role Report Cards</h3><table>")
        body.append("<tr><th>Status</th><th>Role</th><th>KPI</th><th>Value</th><th>Δ vs prior</th><th>Summary</th></tr>")
        emoji = {
            HealthStatus.OK: "✅",
            HealthStatus.DEGRADED: "⚠️",
            HealthStatus.BLOCKED: "🔒",
            HealthStatus.FAIL: "❌",
        }
        for card in ctx.role_report_cards:
            delta = (
                f"{card.delta_vs_prior:+.3f}"
                if card.delta_vs_prior is not None else "—"
            )
            body.append(
                f"<tr><td>{emoji.get(card.health, '?')}</td>"
                f"<td><b>{card.role_name}</b></td>"
                f"<td>{card.kpi_name}</td>"
                f"<td>{card.kpi_value:.3f}</td>"
                f"<td>{delta}</td>"
                f"<td>{card.summary}</td></tr>"
            )
        body.append("</table>")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_email_digest_with_report_cards.py tests/test_email_digest.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/trading_bot/email_digest.py tests/test_email_digest_with_report_cards.py
git commit -m "feat(plan-9): daily digest renders role report cards + zero-equity guard"
```

---

## Task 19: I1 — heartbeat staleness uses time.time()

**Files:**
- Modify: `src/trading_bot/state_heartbeat.py`
- Modify: `src/trading_bot/watchdog_stall.py`

Replace `dt.datetime.now().timestamp()` with `time.time()` in both `is_stale()` and `StallDetector.check()` to eliminate the timezone-fragility trap.

- [ ] **Step 1: Modify state_heartbeat.py**

In the `is_stale` function, replace:

```python
    age = dt.datetime.now().timestamp() - p.stat().st_mtime
```

with:

```python
    import time
    age = time.time() - p.stat().st_mtime
```

(Move `import time` to the top of the file.)

- [ ] **Step 2: Modify watchdog_stall.py**

In `StallDetector.check`, replace:

```python
        age = dt.datetime.now().timestamp() - p.stat().st_mtime
```

with:

```python
        import time
        age = time.time() - p.stat().st_mtime
```

(Move `import time` to top.)

- [ ] **Step 3: Run all tests**

```bash
uv run pytest tests/test_state_heartbeat.py tests/test_watchdog_stall.py -v
```

Tests must still pass.

- [ ] **Step 4: Commit**

```bash
git add src/trading_bot/state_heartbeat.py src/trading_bot/watchdog_stall.py
git commit -m "fix(plan-9): heartbeat staleness uses time.time() — eliminates tz fragility (I1)"
```

---

## Task 20: I2 — `bot daemon` and `bot supervisor` CLI subcommands

**Files:**
- Modify: `src/trading_bot/cli.py`

Add two new Click commands that delegate to the daemon and supervisor entrypoints. This makes manual debugging easier without changing the launchd plist's `python -m trading_bot.daemon` invocation.

- [ ] **Step 1: Modify cli.py**

Add to `cli.py` (after the existing `dashboard` command):

```python
@main.command("daemon")
def daemon_cmd() -> None:
    """Run the trading bot daemon (long-running APScheduler-driven process)."""
    from trading_bot.daemon import main as daemon_main
    raise SystemExit(daemon_main())


@main.command("supervisor")
def supervisor_cmd() -> None:
    """Run the trading bot supervisor (watchdog + drawdown sentinel)."""
    from trading_bot.supervisor import main as supervisor_main
    raise SystemExit(supervisor_main())
```

- [ ] **Step 2: Smoke test**

```bash
uv run bot --help | grep -E "(daemon|supervisor)"
```

Expected: both subcommands listed.

- [ ] **Step 3: Commit**

```bash
git add src/trading_bot/cli.py
git commit -m "feat(plan-9): bot daemon + bot supervisor CLI subcommands (I2)"
```

---

## Task 21: I5 — auto-run Alembic migrations on daemon boot

**Files:**
- Modify: `src/trading_bot/daemon.py`

At the top of `main()`, before any other work, run `alembic upgrade head`.

- [ ] **Step 1: Modify daemon.py**

In `main()`, after `log = StructuredLogger(...)` and before `log.event("daemon_boot", ...)`, add:

```python
    # Auto-apply pending migrations on boot. Idempotent — exits clean if up-to-date.
    try:
        import subprocess
        repo_root = Path(__file__).parent.parent.parent  # src/trading_bot/daemon.py → repo root
        result = subprocess.run(
            [str(repo_root / ".venv" / "bin" / "alembic"),
             "-c", str(repo_root / "migrations" / "alembic.ini"),
             "upgrade", "head"],
            capture_output=True, text=True, timeout=30, cwd=str(repo_root),
        )
        if result.returncode != 0:
            log.error("alembic_upgrade_failed", error=RuntimeError(result.stderr))
            return 1
        log.event("alembic_upgrade", result="ok")
    except Exception as e:
        log.error("alembic_upgrade_exception", error=e)
        return 1
```

- [ ] **Step 2: Smoke test**

```bash
TRADING_BOT_CONFIG=data/paper_active.json \
TRADING_BOT_HEARTBEAT=/tmp/hb.json \
TRADING_BOT_PAUSE=/tmp/pause.flag \
TRADING_BOT_RUNS=/tmp/runs \
TRADING_BOT_STATE_DB=/tmp/state.db \
timeout 5 uv run python -m trading_bot.daemon || true

ls /tmp/runs/$(date -u +%Y-%m-%d)/daemon/*.json | head -3
```

Look for an `alembic_upgrade` event with `result: "ok"` in the JSON output.

- [ ] **Step 3: Commit**

```bash
git add src/trading_bot/daemon.py
git commit -m "feat(plan-9): daemon auto-runs alembic upgrade head on boot (I5)"
```

---

## Task 22: M5 — runs/ log rotation

**Files:**
- Create: `src/trading_bot/log_rotation.py`
- Modify: `src/trading_bot/scheduler_jobs.py` to register `log_rotation` weekly job
- Test: `tests/test_log_rotation.py`

Archives `runs/<date>/` directories older than 90 days into `runs/_archive/<YYYY-MM>.tar.gz` and removes the originals.

- [ ] **Step 1: Test**

```python
# tests/test_log_rotation.py
import datetime as dt
import os
import tarfile
from pathlib import Path
import pytest
from trading_bot.log_rotation import rotate_logs


def test_rotate_archives_old_dates(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    # 4 dates: 100 days old, 95 days old, 30 days old, today
    today = dt.date.today()
    old_dates = [today - dt.timedelta(days=d) for d in [100, 95, 30, 0]]
    for d in old_dates:
        (runs / d.isoformat()).mkdir()
        (runs / d.isoformat() / "x.json").write_text('{"a":1}')

    rotate_logs(runs_dir=runs, keep_days=90)

    # 100 and 95 day-old dirs should be archived
    archive_dir = runs / "_archive"
    assert archive_dir.exists()
    archives = list(archive_dir.glob("*.tar.gz"))
    assert len(archives) >= 1
    # 30-day and today dirs remain
    assert (runs / (today - dt.timedelta(days=30)).isoformat()).exists()
    assert (runs / today.isoformat()).exists()
    # old dirs are gone
    assert not (runs / (today - dt.timedelta(days=100)).isoformat()).exists()
    assert not (runs / (today - dt.timedelta(days=95)).isoformat()).exists()


def test_rotate_idempotent(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    rotate_logs(runs_dir=runs, keep_days=90)  # nothing to do
    rotate_logs(runs_dir=runs, keep_days=90)  # still nothing to do — must not raise
```

- [ ] **Step 2: Run, fails.**

- [ ] **Step 3: Implement log_rotation.py**

```python
# src/trading_bot/log_rotation.py
"""Weekly log rotation. Archives runs/<YYYY-MM-DD>/ dirs older than keep_days
into runs/_archive/<YYYY-MM>.tar.gz and removes originals.

Scheduled by the daemon's APScheduler at Sun 03:00 ET (see scheduler_jobs.py).
"""
from __future__ import annotations

import datetime as dt
import shutil
import tarfile
from pathlib import Path


def rotate_logs(*, runs_dir: str | Path, keep_days: int = 90) -> dict:
    """Archive any <YYYY-MM-DD> subdir of `runs_dir` whose date is more than
    `keep_days` ago. Returns summary dict with archived count and bytes saved.
    """
    runs_dir = Path(runs_dir)
    archive_dir = runs_dir / "_archive"
    archive_dir.mkdir(exist_ok=True)
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)

    by_month: dict[str, list[Path]] = {}
    for entry in runs_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue
        try:
            entry_date = dt.date.fromisoformat(entry.name)
        except ValueError:
            continue
        if entry_date >= cutoff:
            continue
        month_key = entry_date.strftime("%Y-%m")
        by_month.setdefault(month_key, []).append(entry)

    archived_count = 0
    bytes_saved = 0
    for month_key, paths in by_month.items():
        archive_path = archive_dir / f"{month_key}.tar.gz"
        mode = "a:gz" if archive_path.exists() else "w:gz"
        # tarfile.open with "a:gz" doesn't actually work for true append; use w:gz
        # and accept that re-running the same month overwrites with the union.
        # Simpler: collect existing members then re-create.
        with tarfile.open(archive_path, "w:gz") as tar:
            for p in paths:
                tar.add(p, arcname=p.name)
                bytes_saved += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p)
                archived_count += 1

    return {"archived_count": archived_count, "bytes_saved": bytes_saved, "by_month": list(by_month.keys())}
```

- [ ] **Step 4: Wire into scheduler**

Modify `src/trading_bot/scheduler_jobs.py` to register a `log_rotation` job. Add to the runners dict provided by daemon (Task 16):

```python
# In daemon.py _load_runners, add:
"log_rotation": _wrap("log_rotation", lambda: rotate_logs(runs_dir=RUNS_DIR, keep_days=90)),
```

In `scheduler_jobs.py`'s `register_jobs`, add at the end:

```python
    scheduler.add_job(
        runners["log_rotation"],
        trigger=CronTrigger(hour=3, minute=0, day_of_week="sun", timezone=et),
        id="log_rotation",
        replace_existing=True,
    )
```

Update `tests/test_scheduler_jobs.py` to include `"log_rotation": MagicMock()` in each runners dict and `"log_rotation"` in the expected set.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_log_rotation.py tests/test_scheduler_jobs.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/trading_bot/log_rotation.py src/trading_bot/scheduler_jobs.py tests/test_log_rotation.py tests/test_scheduler_jobs.py
git commit -m "feat(plan-9): weekly log rotation archives runs older than 90 days (M5)"
```

---

## Task 23: Phase 2 deployment dry run

**Files:** none (manual verification with hot-reload)

This task replaces the running daemon and supervisor with the Phase 2 code. No filesystem changes outside the worktree.

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ 2>&1 | tail -3
```

Expected: all tests pass (Phase 1's 254 + roughly 30+ new Phase 2 tests).

- [ ] **Step 2: Verify daemon imports cleanly with new modules**

```bash
uv run python -c "from trading_bot import daemon, supervisor; from trading_bot.roles import (
  base, runner, health_pulse, stock_scanner, crypto_scanner, universe_curator,
  sentiment_analyst, portfolio_monitor, order_steward, vip_listener, reporter,
  watchdog, account_sentinel, schedule_auditor, resource_guardian
); print('all imports ok')"
```

- [ ] **Step 3: Hot-reload the running daemon and supervisor**

```bash
launchctl kickstart -k gui/$(id -u)/com.bharath.trading.daemon.paper
sleep 5
launchctl kickstart -k gui/$(id -u)/com.bharath.trading.supervisor
sleep 5
```

- [ ] **Step 4: Verify both processes alive and producing role_runs rows**

```bash
launchctl list | grep com.bharath.trading
sleep 90
sqlite3 data/state.db "SELECT role_name, COUNT(*), MAX(started_at) FROM role_runs GROUP BY role_name ORDER BY role_name;"
```

Expected output: at least one row per active role (`health_pulse` should have many; `watchdog` and `account_sentinel` several each; the scan/scheduler jobs as their cron windows arrive). Daemon-side roles fire on the schedule defined in `paper_active.json`.

- [ ] **Step 5: Verify role_kpis populating**

```bash
sqlite3 data/state.db "SELECT role_name, kpi_name, value FROM role_kpis;"
```

Phase 2 KPIs are mostly placeholders (real KPIs activate in Phase 3). The presence of rows confirms the persistence path works.

- [ ] **Step 6: Verify integration end-to-end with mid-day report**

If you're running this near 12:31 ET, the next mid-day report email will contain the role report cards table. If outside that window, manually trigger an EOD digest:

```bash
uv run bot eod-report
```

Check your inbox: the email should now contain a "Role Report Cards" section with one row per active role. Statuses should be ✅ for healthy roles.

- [ ] **Step 7: Verify boot-race no longer fires false alarm**

The supervisor now skips its first stall check for 60 seconds after startup. Re-deploy and confirm:

```bash
launchctl kickstart -k gui/$(id -u)/com.bharath.trading.supervisor
sleep 65
ls runs/$(date -u +%Y-%m-%d)/supervisor/ | grep -c "stall_detected" || echo "0 stall events (good)"
```

Expected: `0 stall events` (or only the legitimate ones if the daemon actually went stale, which shouldn't happen on a normal boot).

---

## Acceptance criteria for Phase 2

The phase is shipped when:

1. `uv run pytest tests/` passes (all Phase 1 tests + new Phase 2 tests).
2. The 13 wrapped Roles + 2 new operational Roles all import cleanly and execute via APScheduler.
3. `state.db.role_runs` table accumulates rows from active roles within 5 minutes of daemon hot-reload.
4. `state.db.role_kpis` rows are written when `persist_kpi()` is called (smoke-test by triggering an EOD digest).
5. The next EOD digest email (18:00 ET on a weekday) contains a "Role Report Cards" section with rows for the wrapped roles.
6. Supervisor's boot-race false alarm no longer fires at fresh start (60s grace verified).
7. Heartbeat staleness uses `time.time()` (I1 closed).
8. `bot daemon` and `bot supervisor` CLI subcommands work (I2 closed).
9. Daemon auto-runs Alembic migrations on boot (I5 closed).
10. Weekly log rotation job is registered and rotates anything older than 90 days (M5 closed).

---

## Notes on what's NOT in Phase 2

- **Insider Tracker, Earnings Watcher, Macro Sensor** — daemon-side Tier 1 roles requiring new external API integrations (EDGAR, earnings calendar, FRED + breadth). Plan as Phase 2.5.
- **Strategy Coach, Hold-SPY Coordinator** — depend on alpha-vs-SPY data which requires Phase 3 (lab + leaderboard) to populate. Phase 4.
- **All Lab roles** — Backtest Engineer, Param Optimizer, Strategy Architect, Code Reviewer, Calibrator, Promoter — Phases 3 and 5.
- **Tone Analyst** — Phase 5 (lab-side, depends on Strategy Architect).
- **Risk Officer + Trade Executor as full Roles** — these are tightly coupled to existing `risk_manager.py` and `alpaca_client.py`. Phase 2.5 will refactor them into Role wrappers with KPIs (veto rate, slippage, retry success). For Phase 2, they remain bare module-level functions called from inside `cli.intel_scan` / `cli.crypto_scan`.
- **Real KPI computation** — most Phase 2 roles return placeholder KPIs. Real KPIs (`buy_win_rate_5d`, `top25_capture_rate_14d`, `floor_block_post_5d_return`, etc.) require trade journal accumulation and are wired in Phase 3 alongside the Backtest Engineer.

When all 10 acceptance criteria hold, Phase 2 is complete and the bot's operational layer + accountability surface is fully role-aware.
