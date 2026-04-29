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


def test_wheel_tables_present_after_migration(tmp_path, monkeypatch):
    """Smoke-test: after `alembic upgrade head`, the four wheel tables exist."""
    import os
    from alembic import command
    from alembic.config import Config
    db = tmp_path / "test_state.db"
    monkeypatch.setenv("STATE_DB_URL", f"sqlite:///{db}")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(repo, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo, "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")
    import sqlite3
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    for t in ("option_fills", "option_iv_history", "wheel_cycles", "wheel_universe_cache"):
        assert t in tables, f"missing table {t}"


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
