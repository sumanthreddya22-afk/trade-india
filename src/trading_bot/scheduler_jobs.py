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
        misfire_grace_time=300,
        coalesce=True,
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
        misfire_grace_time=300,
        coalesce=True,
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
        misfire_grace_time=300,
        coalesce=True,
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
        misfire_grace_time=300,
        coalesce=True,
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
            misfire_grace_time=300,
            coalesce=True,
        )

    # Pre-market: massive-refresh + rank
    scheduler.add_job(
        runners["massive_refresh"],
        trigger=CronTrigger(hour=6, minute=30, day_of_week="mon-fri", timezone=et),
        id="massive_refresh",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    scheduler.add_job(
        runners["premarket_rank"],
        trigger=CronTrigger(hour=7, minute=30, day_of_week="mon-fri", timezone=et),
        id="premarket_rank",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )

    # Midday rerank: 12:00 ET weekdays. Refreshes opportunities.md with the
    # running intraday-aggregated daily bar for every US ticker so symbols
    # that broke out this morning enter the universe before the 12:30 scan.
    if "midday_rerank" in runners:
        scheduler.add_job(
            runners["midday_rerank"],
            trigger=CronTrigger(hour=12, minute=0, day_of_week="mon-fri", timezone=et),
            id="midday_rerank",
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )

    # Midday report: 12:31 ET weekdays (offset 1 min from the 12:30 stock_scanner cycle
    # so the two jobs don't compete for the same APScheduler worker thread).
    scheduler.add_job(
        runners["midday_report"],
        trigger=CronTrigger(hour=12, minute=31, day_of_week="mon-fri", timezone=et),
        id="midday_report",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )

    # Daily digest: 18:00 ET weekdays
    scheduler.add_job(
        runners["daily_digest"],
        trigger=CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=et),
        id="daily_digest",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )

    # Log rotation: weekly Sun 03:00 ET
    scheduler.add_job(
        runners["log_rotation"],
        trigger=CronTrigger(hour=3, minute=0, day_of_week="sun", timezone=et),
        id="log_rotation",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )

    # Phase 4: Strategy Coach — daily 06:00 ET, weekdays. Evaluates 30d
    # paper alpha vs SPY and flips fallback flag with hysteresis.
    if "strategy_coach" in runners:
        scheduler.add_job(
            runners["strategy_coach"],
            trigger=CronTrigger(hour=6, minute=0, day_of_week="mon-fri", timezone=et),
            id="strategy_coach",
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )

    # Phase 4: Hold-SPY Coordinator — 15:55 ET weekdays. Drives 5-day
    # exit/reverse transition when fallback flag toggles.
    if "hold_spy_coordinator" in runners:
        scheduler.add_job(
            runners["hold_spy_coordinator"],
            trigger=CronTrigger(hour=15, minute=55, day_of_week="mon-fri", timezone=et),
            id="hold_spy_coordinator",
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )
