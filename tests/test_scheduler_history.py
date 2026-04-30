"""Tests for scheduler_history: last-run JSON write/read + APScheduler hook."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_record_and_read_roundtrip(tmp_path):
    from trading_bot.scheduler_history import read_last_runs, record_job_run

    p = tmp_path / "last_run.json"
    when = datetime(2026, 4, 30, 12, 34, 56, tzinfo=timezone.utc)
    record_job_run("daily_digest", when, path=p)

    out = read_last_runs(path=p)
    assert "daily_digest" in out
    assert out["daily_digest"] == when


def test_record_overwrites_prior_entry(tmp_path):
    from trading_bot.scheduler_history import read_last_runs, record_job_run

    p = tmp_path / "last_run.json"
    early = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 4, 30, 16, 30, 0, tzinfo=timezone.utc)
    record_job_run("daily_digest", early, path=p)
    record_job_run("daily_digest", late, path=p)

    out = read_last_runs(path=p)
    assert out["daily_digest"] == late


def test_read_missing_file_returns_empty(tmp_path):
    from trading_bot.scheduler_history import read_last_runs

    assert read_last_runs(path=tmp_path / "nope.json") == {}


def test_read_corrupt_file_returns_empty(tmp_path):
    from trading_bot.scheduler_history import read_last_runs

    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert read_last_runs(path=p) == {}


def test_naive_datetime_treated_as_utc(tmp_path):
    from trading_bot.scheduler_history import read_last_runs, record_job_run

    p = tmp_path / "last_run.json"
    naive = datetime(2026, 4, 30, 12, 0, 0)  # no tz
    record_job_run("heartbeat", naive, path=p)

    out = read_last_runs(path=p)
    assert out["heartbeat"].tzinfo is not None
    assert out["heartbeat"] == naive.replace(tzinfo=timezone.utc)


def test_attach_listener_records_executed_jobs(tmp_path, monkeypatch):
    """End-to-end: spin up a real APScheduler, fire a job, assert recorded."""
    import time

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from trading_bot import scheduler_history

    p = tmp_path / "last_run.json"
    monkeypatch.setattr(scheduler_history, "LAST_RUN_PATH", p)

    fired = {"count": 0}

    def _job():
        fired["count"] += 1

    sched = BackgroundScheduler()
    scheduler_history.attach_listener(sched, path=p)
    sched.add_job(_job, IntervalTrigger(seconds=0.1), id="test_job")
    sched.start()
    try:
        # Wait for at least one fire + listener flush.
        for _ in range(40):
            if fired["count"] >= 1 and p.exists():
                break
            time.sleep(0.05)
    finally:
        sched.shutdown(wait=True)

    assert fired["count"] >= 1
    out = scheduler_history.read_last_runs(path=p)
    assert "test_job" in out


def test_dashboard_row_includes_last_run_when_recorded(tmp_path, monkeypatch):
    """_build_scheduled_jobs() must surface last-run from scheduler_history."""
    from trading_bot import scheduler_history
    from trading_bot.dashboard import data as dash_data

    p = tmp_path / "last_run.json"
    monkeypatch.setattr(scheduler_history, "LAST_RUN_PATH", p)

    when = datetime.now(timezone.utc)
    scheduler_history.record_job_run("daily_digest", when, path=p)

    errors: list[str] = []
    rows = dash_data._build_scheduled_jobs(errors)

    by_id = {r.task_id: r for r in rows}
    assert "daily_digest" in by_id
    # When we just recorded, last_run should not be the "—" sentinel.
    assert by_id["daily_digest"].last_run_local != "—"
    # And jobs we never recorded should still show "—" instead of crashing.
    assert by_id["heartbeat"].last_run_local == "—"
