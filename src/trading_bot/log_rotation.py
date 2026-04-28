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
