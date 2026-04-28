"""Lab process entrypoint. Long-running process under launchd.

Usage:
    python -m trading_bot.lab

Runs nightly param search + auto-promote alongside daemon and supervisor.
SIGTERM gracefully stops the scheduler. Phase 3 wires two jobs:

    02:00 ET daily — param_search (ParamOptimizerRole)
    02:45 ET daily — auto_promote (PromoterRole)
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from trading_bot.log_structured import StructuredLogger

CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))


def _build_runners(log: StructuredLogger):
    from trading_bot.roles.param_optimizer import ParamOptimizerRole
    from trading_bot.roles.promoter import PromoterRole
    from trading_bot.state_db import get_engine

    engine = get_engine(STATE_DB)
    optimizer = ParamOptimizerRole(engine=engine)
    promoter = PromoterRole(engine=engine, active_path=CONFIG_PATH)

    def _wrap(name: str, fn):
        def runner():
            log.event(f"{name}_start")
            try:
                result = fn()
                log.event(
                    f"{name}_finish",
                    status=getattr(result.status, "value", str(result.status)),
                    outputs=result.outputs,
                )
            except Exception as e:
                log.error(f"{name}_failed", error=e)

        return runner

    return {
        "param_search": _wrap(
            "param_search", lambda: optimizer.safe_run(ctx={"template": "momentum"})
        ),
        "auto_promote": _wrap(
            "auto_promote", lambda: promoter.safe_run(ctx={})
        ),
    }


def _register_lab_jobs(scheduler: BackgroundScheduler, runners: dict) -> None:
    scheduler.add_job(
        runners["param_search"],
        trigger=CronTrigger(hour=2, minute=0, timezone="America/New_York"),
        id="param_search",
        replace_existing=True,
    )
    scheduler.add_job(
        runners["auto_promote"],
        trigger=CronTrigger(hour=2, minute=45, timezone="America/New_York"),
        id="auto_promote",
        replace_existing=True,
    )


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="lab")
    log.event("lab_boot", config_path=str(CONFIG_PATH))

    sched = BackgroundScheduler(timezone="America/New_York")
    runners = _build_runners(log)
    _register_lab_jobs(sched, runners)

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("lab_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    sched.start()
    log.event("lab_scheduler_started", jobs=[j.id for j in sched.get_jobs()])

    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        log.event("lab_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
