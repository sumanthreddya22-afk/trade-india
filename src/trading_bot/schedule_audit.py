"""Schedule self-test — counts how many times each cron job fired today
vs how many times it should have, flags shortfalls."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


# Maps job_id (the APScheduler id) to the daemon log event emitted on
# each fire. Keep in sync with scheduler_jobs.py.
JOB_EVENT_MAP: dict[str, str] = {
    "stock_scanner": "stock_scan_start",
    "crypto_scanner": "crypto_scan_start",
    "portfolio_monitor": "portfolio_watch_start",
    "order_steward_sweep": "verify_stops_start",
    "vip_listener": "vip_scan_start",
    "news_warm_morning": "news_warm_start",
    "news_warm_midday": "news_warm_start",
    "massive_refresh": "massive_refresh_start",
    "premarket_rank": "premarket_rank_start",
    "midday_rerank": "midday_rerank_start",
    "midday_snapshot": "midday_snapshot_start",
    "daily_digest": "daily_digest_start",
    "reconciler_close": "reconciler_start",
    "reconciler_pre_digest": "reconciler_start",
    "schedule_audit": "schedule_audit_start",
    "alert_drain": "alert_drain_start",
    "hold_spy_coordinator": "hold_spy_start",
    "strategy_coach": "strategy_coach_start",
    "log_rotation": "log_rotation_start",
}


def expected_fires_for_date(*, audit_date: dt.date) -> dict[str, int]:
    """Compute expected fire counts based on the cron expressions in
    scheduler_jobs.py. We hardcode the schedule here rather than parsing
    the cron expressions — simpler, and breaks loudly when schedules
    change without updating the audit."""
    is_weekday = audit_date.weekday() < 5
    return {
        "crypto_scanner": 48,                              # every 30 min, 24/7
        "order_steward_sweep": 48,                         # :20, :50 24/7
        "stock_scanner": 7 if is_weekday else 0,           # :30 of 9-15 ET, mon-fri
        "portfolio_monitor": 8 if is_weekday else 0,       # :00 of 9-16, mon-fri
        "vip_listener": 8 if is_weekday else 0,            # :00 of 9-16, mon-fri
        "news_warm_morning": 1 if is_weekday else 0,
        "news_warm_midday": 1 if is_weekday else 0,
        "massive_refresh": 1 if is_weekday else 0,
        "premarket_rank": 1 if is_weekday else 0,
        "midday_rerank": 1 if is_weekday else 0,
        "midday_snapshot": 1 if is_weekday else 0,
        "daily_digest": 1,                                 # daily
        "reconciler_close": 1 if is_weekday else 0,
        "reconciler_pre_digest": 1,
        "schedule_audit": 1,
        "alert_drain": 24 * 60,                            # every 1 min
        # log_rotation, strategy_coach, hold_spy_coordinator: cadence varies; leave out
    }


def count_fires_in_logs(*, runs_dir: Path, audit_date: dt.date,
                        event_name: str) -> int:
    """Count occurrences of `<event_name>` in JSON logs under
    runs/<YYYY-MM-DD>/{daemon,supervisor}/."""
    n = 0
    date_dir = runs_dir / audit_date.isoformat()
    if not date_dir.exists():
        return 0
    for sub in ("daemon", "supervisor"):
        d = date_dir / sub
        if not d.exists():
            continue
        for path in d.glob("*.json"):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if obj.get("event") == event_name:
                            n += 1
            except Exception:
                continue
    return n


class ScheduleAuditStore:
    def __init__(self, db_path: Path | str = "data/state.db") -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)

    def record(self, *, audit_date: dt.date, job_id: str, expected: int,
               actual: int, ratio: float, audited_at: dt.datetime) -> None:
        with self._engine.begin() as c:
            c.execute(
                text(
                    "INSERT OR REPLACE INTO schedule_audits "
                    "(audit_date, job_id, expected_fires, actual_fires, ratio, audited_at) "
                    "VALUES (:audit_date, :job_id, :expected, :actual, :ratio, :audited_at)"
                ),
                {"audit_date": audit_date, "job_id": job_id,
                 "expected": expected, "actual": actual, "ratio": ratio,
                 "audited_at": audited_at},
            )

    def latest(self, *, audit_date: dt.date) -> list[dict[str, Any]]:
        with self._engine.begin() as c:
            rows = c.execute(
                text("SELECT job_id, expected_fires, actual_fires, ratio "
                     "FROM schedule_audits WHERE audit_date = :d ORDER BY ratio ASC"),
                {"d": audit_date},
            ).mappings().all()
        return [dict(r) for r in rows]


def run_audit(
    *,
    audit_date: dt.date,
    runs_dir: Path,
    expected_fires: dict[str, int] | None = None,
    event_name_for_job: dict[str, str] | None = None,
    store: ScheduleAuditStore | None = None,
    actual_overrides: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Run today's audit. Returns one record per job with
    expected/actual/ratio. Writes to schedule_audits table."""
    expected = expected_fires or expected_fires_for_date(audit_date=audit_date)
    event_map = event_name_for_job or JOB_EVENT_MAP
    store = store or ScheduleAuditStore()
    overrides = actual_overrides or {}

    audited_at = dt.datetime.now(dt.timezone.utc)
    out = []
    for job_id, expected_n in expected.items():
        if job_id in overrides:
            actual = overrides[job_id]
        else:
            event = event_map.get(job_id)
            actual = count_fires_in_logs(
                runs_dir=runs_dir, audit_date=audit_date, event_name=event,
            ) if event else 0
        ratio = (actual / expected_n) if expected_n > 0 else 1.0
        store.record(audit_date=audit_date, job_id=job_id, expected=expected_n,
                     actual=actual, ratio=ratio, audited_at=audited_at)
        out.append({"job_id": job_id, "expected": expected_n, "actual": actual, "ratio": ratio})
    return out
