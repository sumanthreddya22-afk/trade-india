"""APScheduler wiring for the v4 daemon.

Cadence (Plan v4 §6 + §10):
  * boot_check          — on startup, then every 6 hours
  * market_data_ingest  — every 1 minute during equity session, 5 min otherwise
  * position_snapshot   — every 5 minutes
  * orphan_loop         — every 30 seconds
  * reconciliation      — nightly at 23:00 ET (close + buffer)
  * drift_monitor       — nightly at 23:30 ET
  * mutation_cycle      — monthly on the 1st at 02:00 ET

Wall-clock gates are *not* shortened: cadence above is operational, not
validation. MVP-OP / ALPHA windows are tracked separately in the ledger.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from trading_bot.daemon.jobs import (
    DaemonContext, job_account_snapshot, job_boot_check, job_drift_monitor,
    job_market_data_ingest, job_mutation_cycle, job_orphan_loop,
    job_position_snapshot, job_reconciliation, job_strategy_runner,
)
from trading_bot.daemon.logging_setup import setup_logging

log = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    """Operator-tunable cadence. Defaults match Plan v4 §6/§10."""
    boot_check_interval_min: int = 360                # 6h
    market_data_interval_seconds: int = 60            # every 1 min
    position_snapshot_interval_min: int = 5
    account_snapshot_interval_min: int = 5
    orphan_loop_interval_seconds: int = 30
    reconciliation_cron: str = "0 23 * * *"           # 23:00 daily, local TZ
    drift_monitor_cron: str = "30 23 * * *"
    strategy_runner_cron: str = "30 15 * * 1-5"       # 15:30 ET weekdays
    mutation_cycle_cron: str = "0 2 1 * *"            # 02:00 on the 1st
    timezone: str = "America/New_York"
    run_boot_check_on_startup: bool = True
    enable_file_logging: bool = True


def build_scheduler(
    ctx: DaemonContext, config: Optional[DaemonConfig] = None,
) -> BackgroundScheduler:
    """Return a configured (but not started) scheduler."""
    config = config or DaemonConfig()
    sched = BackgroundScheduler(timezone=config.timezone)

    sched.add_job(
        job_boot_check, IntervalTrigger(minutes=config.boot_check_interval_min),
        args=(ctx,), id="boot_check", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_market_data_ingest,
        IntervalTrigger(seconds=config.market_data_interval_seconds),
        args=(ctx,), id="market_data_ingest", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_position_snapshot,
        IntervalTrigger(minutes=config.position_snapshot_interval_min),
        args=(ctx,), id="position_snapshot", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_account_snapshot,
        IntervalTrigger(minutes=config.account_snapshot_interval_min),
        args=(ctx,), id="account_snapshot", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_orphan_loop,
        IntervalTrigger(seconds=config.orphan_loop_interval_seconds),
        args=(ctx,), id="orphan_loop", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_reconciliation, CronTrigger.from_crontab(config.reconciliation_cron),
        args=(ctx,), id="reconciliation", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_drift_monitor, CronTrigger.from_crontab(config.drift_monitor_cron),
        args=(ctx,), id="drift_monitor", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_strategy_runner, CronTrigger.from_crontab(config.strategy_runner_cron),
        args=(ctx,), id="strategy_runner", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    sched.add_job(
        job_mutation_cycle, CronTrigger.from_crontab(config.mutation_cycle_cron),
        args=(ctx,), id="mutation_cycle", coalesce=True, max_instances=1,
        replace_existing=True,
    )
    return sched


_stop_event = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        log.info("daemon: received signal %s, shutting down", signum)
        _stop_event.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run_daemon(
    ctx: Optional[DaemonContext] = None,
    config: Optional[DaemonConfig] = None,
    once: bool = False,
) -> int:
    """Start the daemon. Blocks until SIGTERM/SIGINT (or returns 0 after
    a single tick of every job if ``once=True`` — used by smoke tests)."""
    ctx = ctx or DaemonContext()
    config = config or DaemonConfig()

    if config.enable_file_logging:
        setup_logging()
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    # Reconcile additive schema growth against the live DBs before the
    # boot check. Same-version DDL is IF-NOT-EXISTS everywhere, so this
    # only adds tables/indexes/triggers that shipped since the DB was
    # last initialised. Across an incompatible SCHEMA_VERSION bump this
    # is a no-op — boot_check then surfaces the mismatch and the operator
    # runs the proper migration. See ``ledger.ensure_schema`` for the
    # full safety contract.
    try:
        from trading_bot.ledger import connect_writer, ensure_schema
        for db_path in (ctx.ledger_db, ctx.mirror_db):
            if not db_path.exists():
                continue
            conn = connect_writer(db_path)
            try:
                status = ensure_schema(conn)
            finally:
                conn.close()
            log.info(
                "daemon: ensure_schema(%s) → %s", db_path.name, status,
            )
    except Exception:  # noqa: BLE001
        log.exception("daemon: ensure_schema failed; continuing to boot_check")

    if config.run_boot_check_on_startup:
        log.info("daemon: running startup boot check")
        result = job_boot_check(ctx)
        if result == "error":
            log.error("daemon: startup boot check failed — refusing to start")
            return 2

    if once:
        log.info("daemon: --once mode, ticking each job exactly once")
        for fn in (
            job_market_data_ingest, job_position_snapshot,
            job_account_snapshot, job_orphan_loop,
            job_reconciliation, job_drift_monitor,
            job_strategy_runner, job_mutation_cycle,
        ):
            log.info("daemon: tick %s", fn.__name__)
            fn(ctx)
        return 0

    sched = build_scheduler(ctx, config)
    _install_signal_handlers()
    sched.start()
    log.info("daemon: started; %d jobs scheduled", len(sched.get_jobs()))

    try:
        while not _stop_event.is_set():
            time.sleep(1.0)
    finally:
        log.info("daemon: stopping scheduler")
        sched.shutdown(wait=False)
    return 0


__all__ = ["DaemonConfig", "build_scheduler", "run_daemon"]
