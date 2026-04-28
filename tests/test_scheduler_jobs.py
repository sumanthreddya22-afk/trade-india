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
        "midday_report": MagicMock(),
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
        "midday_report",
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
        "daily_digest": MagicMock(), "midday_report": MagicMock(),
        "heartbeat": MagicMock(),
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
        "daily_digest": MagicMock(), "midday_report": MagicMock(),
        "heartbeat": MagicMock(),
    }
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)
    hb = next(j for j in sched.get_jobs() if j.id == "heartbeat")
    assert hb.trigger.interval.total_seconds() == 60
