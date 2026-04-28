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
