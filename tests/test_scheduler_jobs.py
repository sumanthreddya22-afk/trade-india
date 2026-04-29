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
        "midday_snapshot": MagicMock(),
        "heartbeat": MagicMock(),
        "log_rotation": MagicMock(),
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
        "midday_snapshot",
        "daily_digest",
        "log_rotation",
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
        "daily_digest": MagicMock(), "midday_snapshot": MagicMock(),
        "heartbeat": MagicMock(),
        "log_rotation": MagicMock(),
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
        "daily_digest": MagicMock(), "midday_snapshot": MagicMock(),
        "heartbeat": MagicMock(),
        "log_rotation": MagicMock(),
    }
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)
    hb = next(j for j in sched.get_jobs() if j.id == "heartbeat")
    assert hb.trigger.interval.total_seconds() == 60


def test_verify_stops_cron_is_24_7_at_20_and_50():
    """order_steward_sweep must fire at :20 and :50 every hour, every day (24/7)."""
    sched = BackgroundScheduler(timezone="America/New_York")
    cadence = CadenceConfig()
    runners = {
        "intel_scan": MagicMock(), "crypto_scan": MagicMock(),
        "portfolio_watch": MagicMock(), "verify_stops": MagicMock(),
        "news_warm": MagicMock(), "massive_refresh": MagicMock(),
        "premarket_rank": MagicMock(), "vip_scan": MagicMock(),
        "daily_digest": MagicMock(), "midday_snapshot": MagicMock(),
        "heartbeat": MagicMock(),
        "log_rotation": MagicMock(),
    }
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)
    job = next(j for j in sched.get_jobs() if j.id == "order_steward_sweep")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["minute"] == "20,50", f"Expected '20,50', got '{fields['minute']}'"
    assert fields["hour"] == "*", f"Expected '*', got '{fields['hour']}'"
    assert fields["day_of_week"] == "*", f"Expected '*', got '{fields['day_of_week']}'"


def test_register_jobs_uses_misfire_grace_and_coalesce():
    """All cron jobs must have misfire_grace_time=300 + coalesce=True so a
    daemon stall during a fire window doesn't drop the job silently."""
    from unittest.mock import MagicMock
    from trading_bot.scheduler_jobs import register_jobs
    from trading_bot.cadence import CadenceConfig

    scheduler = MagicMock()
    runners = {name: MagicMock() for name in (
        "heartbeat", "intel_scan", "crypto_scan", "portfolio_watch",
        "verify_stops", "vip_scan", "news_warm", "massive_refresh",
        "premarket_rank", "midday_rerank", "midday_snapshot",
        "daily_digest", "log_rotation", "hold_spy_coordinator",
        "strategy_coach", "reconciler", "schedule_audit", "alert_drain",
    )}
    cadence = CadenceConfig()

    register_jobs(scheduler=scheduler, cadence=cadence, runners=runners)

    cron_calls = [c for c in scheduler.add_job.call_args_list
                  if "trigger" in c.kwargs and c.kwargs["trigger"].__class__.__name__ == "CronTrigger"]
    assert len(cron_calls) > 0
    for c in cron_calls:
        assert c.kwargs.get("misfire_grace_time") == 300, \
            f"Job {c.kwargs.get('id')} missing misfire_grace_time=300"
        assert c.kwargs.get("coalesce") is True, \
            f"Job {c.kwargs.get('id')} missing coalesce=True"


def test_wheel_scan_and_manage_jobs_registered():
    from trading_bot.scheduler_jobs import register_jobs
    from trading_bot.cadence import CadenceConfig
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler()
    runners = {k: (lambda: None) for k in (
        "heartbeat", "intel_scan", "crypto_scan", "portfolio_watch",
        "verify_stops", "vip_scan", "alerts_drain", "reconcile_post_close",
        "reconcile_pre_digest", "schedule_audit", "wheel_scan", "wheel_manage",
        "iv_capture",
        "news_warm", "massive_refresh", "premarket_rank", "midday_snapshot",
        "daily_digest", "log_rotation",
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
    assert "iv_capture" in ids
