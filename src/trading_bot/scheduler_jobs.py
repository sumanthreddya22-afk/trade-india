"""APScheduler job registration. Each scheduled routine is wired up here.
The `runners` dict maps logical names to callables; this lets daemon.py
inject the existing CLI command functions (or test mocks).
"""
from __future__ import annotations

from typing import Callable, Mapping

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from trading_bot.cadence import CadenceConfig


def register_jobs(
    *,
    scheduler: BaseScheduler,
    cadence: CadenceConfig,
    runners: Mapping[str, Callable[[], None]],
) -> None:
    et = "America/New_York"

    # Continuous: heartbeat
    scheduler.add_job(
        runners["heartbeat"],
        trigger=IntervalTrigger(seconds=cadence.heartbeat_seconds),
        id="heartbeat",
        replace_existing=True,
    )

    # Stock Scanner: every 60 min during market hours, weekdays
    scheduler.add_job(
        runners["intel_scan"],
        trigger=CronTrigger(
            hour="9-15",
            minute=f"*/{cadence.stock_scanner_minutes}" if cadence.stock_scanner_minutes < 60 else "30",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="stock_scanner",
        replace_existing=True,
    )

    # Crypto Scanner: 24/7 at configured cadence
    scheduler.add_job(
        runners["crypto_scan"],
        trigger=IntervalTrigger(minutes=cadence.crypto_scanner_minutes),
        id="crypto_scanner",
        replace_existing=True,
    )

    # Portfolio Monitor: every N min during market hours
    pm_min = cadence.portfolio_monitor_minutes
    scheduler.add_job(
        runners["portfolio_watch"],
        trigger=CronTrigger(
            hour="9-16",
            minute="0" if pm_min >= 60 else f"*/{pm_min}",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="portfolio_monitor",
        replace_existing=True,
    )

    # Order Steward sweep
    os_min = cadence.order_steward_sweep_minutes
    scheduler.add_job(
        runners["verify_stops"],
        trigger=CronTrigger(
            hour="9-16",
            minute="0" if os_min >= 60 else f"*/{os_min}",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="order_steward_sweep",
        replace_existing=True,
    )

    # VIP Listener: every N min during market hours
    vip_min = cadence.vip_listener_minutes
    scheduler.add_job(
        runners["vip_scan"],
        trigger=CronTrigger(
            hour="9-16",
            minute="0" if vip_min >= 60 else f"*/{vip_min}",
            day_of_week="mon-fri",
            timezone=et,
        ),
        id="vip_listener",
        replace_existing=True,
    )

    # Sentiment warm: at configured ET times
    for label, time_str in (("morning", cadence.sentiment_warm_times_et[0]),
                             ("midday", cadence.sentiment_warm_times_et[1])):
        h, m = time_str.split(":")
        scheduler.add_job(
            runners["news_warm"],
            trigger=CronTrigger(hour=h, minute=m, day_of_week="mon-fri", timezone=et),
            id=f"news_warm_{label}",
            replace_existing=True,
        )

    # Pre-market: massive-refresh + rank
    scheduler.add_job(
        runners["massive_refresh"],
        trigger=CronTrigger(hour=6, minute=30, day_of_week="mon-fri", timezone=et),
        id="massive_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        runners["premarket_rank"],
        trigger=CronTrigger(hour=7, minute=30, day_of_week="mon-fri", timezone=et),
        id="premarket_rank",
        replace_existing=True,
    )

    # Midday report: 12:31 ET weekdays (offset 1 min from the 12:30 stock_scanner cycle
    # so the two jobs don't compete for the same APScheduler worker thread).
    scheduler.add_job(
        runners["midday_report"],
        trigger=CronTrigger(hour=12, minute=31, day_of_week="mon-fri", timezone=et),
        id="midday_report",
        replace_existing=True,
    )

    # Daily digest: 18:00 ET weekdays
    scheduler.add_job(
        runners["daily_digest"],
        trigger=CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=et),
        id="daily_digest",
        replace_existing=True,
    )
