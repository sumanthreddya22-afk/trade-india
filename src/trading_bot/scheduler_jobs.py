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
from trading_bot.scheduler_history import attach_listener


def register_jobs(
    *,
    scheduler: BaseScheduler,
    cadence: CadenceConfig,
    runners: Mapping[str, Callable[[], None]],
) -> None:
    et = "America/New_York"
    attach_listener(scheduler)

    # Continuous: heartbeat. Bucket E: misfire_grace_time + coalesce so a
    # daemon stall doesn't accumulate dozens of queued heartbeat fires.
    scheduler.add_job(
        runners["heartbeat"],
        trigger=IntervalTrigger(seconds=cadence.heartbeat_seconds),
        id="heartbeat",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
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

    # Crypto Scanner: 24/7 at configured cadence. Bucket E: misfire_grace_time
    # + coalesce so a daemon stall during a crypto event can't queue overlapping
    # fires that race the same orchestrator state.
    scheduler.add_job(
        runners["crypto_scan"],
        trigger=IntervalTrigger(minutes=cadence.crypto_scanner_minutes),
        id="crypto_scanner",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
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

    # Verify-stops: every :20 and :50, 24/7. Crypto positions need
    # off-hours protection; stocks ignored gracefully outside RTH by
    # the auto-protect logic. Old cadence (`0 9-16 * * 1-5`) was a
    # weekday-market-only schedule that contradicted the auto-protect
    # spec — fixed 2026-04-28.
    scheduler.add_job(
        runners["verify_stops"],
        trigger=CronTrigger(minute="20,50", timezone=et),
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

    # Midday snapshot: 12:00 ET weekdays. Light intraday digest — KPI grid,
    # trades so far, open positions, watchlist signals, risk gauges.
    # Replaces the old midday_report job that was firing at 16:31 ET due to
    # a misfire accumulation on the old 12:31 cron.
    scheduler.add_job(
        runners["midday_snapshot"],
        trigger=CronTrigger(hour=12, minute=0, day_of_week="mon-fri", timezone=et),
        id="midday_snapshot",
        replace_existing=True,
        misfire_grace_time=300, coalesce=True,
    )

    # Daily digest: 16:30 ET weekdays (30 min after market close, before
    # operator typically reads end-of-day mail)
    scheduler.add_job(
        runners["daily_digest"],
        trigger=CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=et),
        id="daily_digest",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )

    # Bucket G: nightly self-review at 17:00 ET (30 min after the digest, so
    # the operator has time to skim the digest first). Read-only morning
    # brief: decision rollup, drift watch, freshness, risk state, system
    # health. Sent every day (incl. weekends) so a Friday-night job miss
    # surfaces Saturday morning, not Monday afternoon.
    if "nightly_review" in runners:
        scheduler.add_job(
            runners["nightly_review"],
            trigger=CronTrigger(hour=17, minute=0, timezone=et),
            id="nightly_review",
            replace_existing=True,
            misfire_grace_time=600,
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

    # Real-time event bus retention: nightly DELETE of events older than
    # 7 days + WAL truncate. Single writer (daemon) so no race with the
    # other launchd processes. Cheap — usually 0–500 row delete.
    if "event_bus_retention" in runners:
        scheduler.add_job(
            runners["event_bus_retention"],
            trigger=CronTrigger(hour=3, minute=15, timezone=et),
            id="event_bus_retention",
            replace_existing=True,
            misfire_grace_time=600,
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

    # Schedule self-test: 21:55 ET (5 min before daily digest).
    if "schedule_audit" in runners:
        scheduler.add_job(
            runners["schedule_audit"],
            trigger=CronTrigger(hour=21, minute=55, timezone=et),
            id="schedule_audit",
            replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )

    # Alert drain: every 1 min. The throttling logic inside drain_alerts
    # checks whether enough time has passed since the last send.
    # Bucket E: misfire + coalesce so a stall doesn't pile up minute-spaced
    # drain attempts that all try to grab the alerts_pending queue at once.
    if "alert_drain" in runners:
        scheduler.add_job(
            runners["alert_drain"],
            trigger=IntervalTrigger(minutes=1),
            id="alert_drain",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
        )

    # Reconciler: 16:05 ET (post-close) + 21:55 ET (pre-digest).
    if "reconciler" in runners:
        scheduler.add_job(
            runners["reconciler"],
            trigger=CronTrigger(hour=16, minute=5, day_of_week="mon-fri", timezone=et),
            id="reconciler_close",
            replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )
        scheduler.add_job(
            runners["reconciler"],
            trigger=CronTrigger(hour=21, minute=55, timezone=et),
            id="reconciler_pre_digest",
            replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )

    # Wheel scan: 10:15 ET weekdays (single daily entry pass).
    if cadence.wheel_scan_enabled and "wheel_scan" in runners:
        scheduler.add_job(
            runners["wheel_scan"],
            trigger=CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone=et),
            id="wheel_scan", replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )
    # Wheel universe build: 21:30 ET nightly (24/7 — runs even on weekends so
    # corporate actions / new optionable listings are picked up before Monday
    # open). Walks Alpaca's optionable equities, filters via Finnhub
    # (market cap, listing age), writes wheel_universe_cache. First-ever run
    # is ~100 min; subsequent runs only re-check 14d-stale entries (~7 min).
    if cadence.wheel_scan_enabled and "wheel_universe_build" in runners:
        scheduler.add_job(
            runners["wheel_universe_build"],
            trigger=CronTrigger(hour=21, minute=30, timezone=et),
            id="wheel_universe_build", replace_existing=True,
            misfire_grace_time=600, coalesce=True,
        )

    # IV capture: 9:45 ET weekdays. The ONLY place we mass-fetch chains for the
    # eligible set; writes ATM 30-day IV to option_iv_history so the wheel-scan
    # at 10:15 can rank IV without per-symbol chain probes.
    if cadence.wheel_scan_enabled and "iv_capture" in runners:
        scheduler.add_job(
            runners["iv_capture"],
            trigger=CronTrigger(hour=9, minute=45, day_of_week="mon-fri", timezone=et),
            id="iv_capture", replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )
    # Wheel manage: every wheel_manage_interval_minutes within the 10-15 ET hours.
    if cadence.wheel_scan_enabled and "wheel_manage" in runners:
        interval = cadence.wheel_manage_interval_minutes
        scheduler.add_job(
            runners["wheel_manage"],
            trigger=CronTrigger(
                hour="10-15",
                minute="0,30" if interval == 30 else f"*/{interval}",
                day_of_week="mon-fri", timezone=et,
            ),
            id="wheel_manage", replace_existing=True,
            misfire_grace_time=300, coalesce=True,
        )

    # Phase 3 — Options Scanner: poll earnings + skew, roll candidates,
    # run scout debate. Fires daily at 9:30 ET (after the open + before
    # the legacy wheel_scan at 10:15 so scout-elevated underlyings can
    # influence wheel selection in a future fusion). Off when wheel
    # itself is disabled.
    if cadence.wheel_scan_enabled and "options_scanner" in runners:
        scheduler.add_job(
            runners["options_scanner"],
            trigger=CronTrigger(hour=9, minute=30, day_of_week="mon-fri", timezone=et),
            id="options_scanner", replace_existing=True,
            misfire_grace_time=600, coalesce=True,
        )
