"""Records the last successful run timestamp for every APScheduler job.

APScheduler emits EVENT_JOB_EXECUTED after each successful job run. We attach
a listener that writes the (job_id, ts_utc) into data/scheduler_last_run.json
so the dashboard can show "last fire" alongside "next fire".

Storage is a single JSON file (matches data/heartbeat.json convention). All
writes are atomic via tmp + os.replace so concurrent reads from the dashboard
process never see a half-written file.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apscheduler.schedulers.base import BaseScheduler


LAST_RUN_PATH = Path(
    os.environ.get("TRADING_BOT_SCHED_HISTORY", "data/scheduler_last_run.json")
)

_LOCK = threading.Lock()


def _read_raw(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        # Corrupt file: start clean rather than crash the scheduler.
        return {}


def record_job_run(job_id: str, when_utc: datetime, *, path: Path | None = None) -> None:
    """Persist `job_id`'s last-run timestamp (UTC ISO-8601, Z-suffixed)."""
    p = path or LAST_RUN_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if when_utc.tzinfo is None:
        when_utc = when_utc.replace(tzinfo=timezone.utc)
    iso = when_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    with _LOCK:
        data = _read_raw(p)
        data[job_id] = iso
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, p)


def read_last_runs(*, path: Path | None = None) -> dict[str, datetime]:
    """Return {job_id: aware UTC datetime}. Missing/corrupt → {}."""
    p = path or LAST_RUN_PATH
    raw = _read_raw(p)
    out: dict[str, datetime] = {}
    for job_id, iso in raw.items():
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[job_id] = dt
        except Exception:
            continue
    return out


def attach_listener(scheduler: "BaseScheduler", *, path: Path | None = None) -> None:
    """Hook EVENT_JOB_EXECUTED so every successful run is recorded.

    Failed runs (EVENT_JOB_ERROR) are intentionally skipped — "last fire"
    means "last successful fire" so a stuck role doesn't look healthy.
    """
    from apscheduler.events import EVENT_JOB_EXECUTED

    def _listener(event):
        try:
            ts = getattr(event, "scheduled_run_time", None) or datetime.now(timezone.utc)
            record_job_run(event.job_id, ts, path=path)
        except Exception:
            # Never let history bookkeeping crash the scheduler.
            pass

    scheduler.add_listener(_listener, EVENT_JOB_EXECUTED)
