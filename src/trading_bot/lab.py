"""Lab process entrypoint. Long-running process under launchd.

Usage:
    python -m trading_bot.lab

Runs nightly param search + auto-promote alongside daemon and supervisor.
SIGTERM gracefully stops the scheduler. Three cron jobs:

    02:00 ET daily — param_search (ParamOptimizerRole)
    02:45 ET daily — auto_promote (PromoterRole)
    05:00 ET daily — calibrate    (CalibratorRole, Phase 3.5)
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
    from trading_bot.roles.calibrator import CalibratorRole
    from trading_bot.roles.code_reviewer import CodeReviewerRole
    from trading_bot.roles.param_optimizer import ParamOptimizerRole
    from trading_bot.roles.promoter import PromoterRole
    from trading_bot.roles.strategy_architect import StrategyArchitectRole
    from trading_bot.state_db import get_engine

    engine = get_engine(STATE_DB)
    optimizer = ParamOptimizerRole(engine=engine)
    promoter = PromoterRole(engine=engine, active_path=CONFIG_PATH, notify=True)
    calibrator = CalibratorRole(engine=engine, config_path=CONFIG_PATH)
    architect = StrategyArchitectRole(engine=engine)
    reviewer = CodeReviewerRole(engine=engine)

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

    def _saturday_evolve():
        """Run Architect, then Reviewer if Architect produced any proposals."""
        log.event("saturday_evolve_start")
        try:
            arch_result = architect.safe_run(ctx={})
            log.event(
                "strategy_architect_finish",
                status=getattr(arch_result.status, "value", str(arch_result.status)),
                outputs=arch_result.outputs,
            )
            if arch_result.outputs.get("n_proposals", 0) > 0:
                rev_result = reviewer.safe_run(ctx={})
                log.event(
                    "code_reviewer_finish",
                    status=getattr(rev_result.status, "value", str(rev_result.status)),
                    outputs=rev_result.outputs,
                )
        except Exception as e:
            log.error("saturday_evolve_failed", error=e)

    return {
        "param_search": _wrap(
            "param_search", lambda: optimizer.safe_run(ctx={"template": "momentum"})
        ),
        "auto_promote": _wrap(
            "auto_promote", lambda: promoter.safe_run(ctx={})
        ),
        "calibrate": _wrap(
            "calibrate", lambda: calibrator.safe_run(ctx={})
        ),
        "saturday_evolve": _saturday_evolve,
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
    scheduler.add_job(
        runners["calibrate"],
        trigger=CronTrigger(hour=5, minute=0, timezone="America/New_York"),
        id="calibrate",
        replace_existing=True,
    )
    # Phase 5: Saturday weekly Architect → Reviewer pipeline.
    if "saturday_evolve" in runners:
        scheduler.add_job(
            runners["saturday_evolve"],
            trigger=CronTrigger(
                hour=6, minute=0, day_of_week="sat", timezone="America/New_York"
            ),
            id="saturday_evolve",
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
