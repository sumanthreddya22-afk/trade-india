import datetime as dt
from pathlib import Path

import pytest


@pytest.fixture
def state_db(tmp_path):
    db_path = tmp_path / "state.db"
    from sqlalchemy import create_engine, text
    e = create_engine(f"sqlite:///{db_path}", future=True)
    with e.begin() as c:
        c.execute(text(
            "CREATE TABLE schedule_audits ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "audit_date DATE NOT NULL, "
            "job_id TEXT NOT NULL, "
            "expected_fires INTEGER NOT NULL, "
            "actual_fires INTEGER NOT NULL, "
            "ratio REAL NOT NULL, "
            "audited_at TIMESTAMP NOT NULL, "
            "UNIQUE(audit_date, job_id))"
        ))
    return db_path


def test_count_fires_in_runs_dir(tmp_path):
    """Counts <job>_start events from JSON log files for a given date."""
    from trading_bot.schedule_audit import count_fires_in_logs
    runs = tmp_path / "runs" / "2026-04-28" / "daemon"
    runs.mkdir(parents=True)
    # 3 fires of stock_scan today.
    for h in (9, 10, 11):
        (runs / f"{h:02d}-30-00.json").write_text(
            f'{{"ts": "2026-04-28T{h:02d}:30:00+00:00", "role": "daemon", '
            f'"event": "stock_scan_start", "level": "info"}}\n'
        )
    n = count_fires_in_logs(
        runs_dir=tmp_path / "runs",
        audit_date=dt.date(2026, 4, 28),
        event_name="stock_scan_start",
    )
    assert n == 3


def test_audit_records_warnings(state_db, tmp_path):
    """Jobs whose actual/expected < 0.5 are flagged."""
    from trading_bot.schedule_audit import run_audit, ScheduleAuditStore
    runs = tmp_path / "runs" / "2026-04-28" / "daemon"
    runs.mkdir(parents=True)
    # Crypto scanned 2 times today but expected 48 — ratio 0.04.
    for h in (1, 2):
        (runs / f"{h:02d}-00-00.json").write_text(
            f'{{"event": "crypto_scan_start", "ts": "2026-04-28T{h:02d}:00:00+00:00"}}\n'
        )

    expected = {"crypto_scanner": 48, "stock_scanner": 7, "verify_stops": 48}
    actual_overrides = {"verify_stops": 8}  # provided directly to test

    store = ScheduleAuditStore(state_db)
    report = run_audit(
        audit_date=dt.date(2026, 4, 28),
        runs_dir=tmp_path / "runs",
        expected_fires=expected,
        event_name_for_job={
            "crypto_scanner": "crypto_scan_start",
            "stock_scanner": "stock_scan_start",
            "verify_stops": "verify_stops_start",
        },
        store=store,
        actual_overrides=actual_overrides,
    )

    # 3 jobs audited; ones with ratio < 0.5 are flagged.
    flagged = [r for r in report if r["ratio"] < 0.5]
    flagged_jobs = {r["job_id"] for r in flagged}
    assert "crypto_scanner" in flagged_jobs   # 2/48
    assert "stock_scanner" in flagged_jobs    # 0/7
    # verify_stops 8/48 = 0.166 < 0.5 → also flagged
    assert "verify_stops" in flagged_jobs

    # All written to DB
    from sqlalchemy import create_engine, text
    e = create_engine(f"sqlite:///{state_db}")
    with e.begin() as c:
        rows = c.execute(text("SELECT job_id, actual_fires, ratio FROM schedule_audits "
                              "ORDER BY job_id")).mappings().all()
    assert len(rows) == 3
    by_job = {r["job_id"]: r for r in rows}
    assert by_job["stock_scanner"]["actual_fires"] == 0
