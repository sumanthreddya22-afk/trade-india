"""Lab process entrypoint. Long-running process under launchd.

Usage:
    python -m trading_bot.lab

Runs nightly param search + auto-promote alongside daemon and supervisor.
SIGTERM gracefully stops the scheduler. Cron jobs:

    02:00 ET daily — param_search       (ParamOptimizerRole)
    02:45 ET daily — auto_promote       (PromoterRole)
    03:30 ET daily — decision_reflect   (DecisionReflectorRole; latency-tolerant,
                                          reads via mailbox if env flag is set)
    05:00 ET daily — calibrate          (CalibratorRole, Phase 3.5)
    06:00 ET Sat   — saturday_evolve    (Architect → Reviewer)
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
from trading_bot.scheduler_history import attach_listener as _attach_history_listener

CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))


def _build_runners(log: StructuredLogger):
    from trading_bot.roles.calibrator import CalibratorRole
    from trading_bot.roles.code_reviewer import CodeReviewerRole
    from trading_bot.roles.decision_reflector import DecisionReflectorRole
    from trading_bot.roles.param_optimizer import ParamOptimizerRole
    from trading_bot.roles.promoter import PromoterRole
    from trading_bot.roles.strategy_architect import StrategyArchitectRole
    from trading_bot.roles.intel_ingestor import IntelIngestorRole
    from trading_bot.roles.threshold_tuner import ThresholdTunerRole
    from trading_bot.state_db import get_engine

    engine = get_engine(STATE_DB)
    optimizer = ParamOptimizerRole(engine=engine)
    promoter = PromoterRole(engine=engine, active_path=CONFIG_PATH, notify=True)
    calibrator = CalibratorRole(engine=engine, config_path=CONFIG_PATH)
    architect = StrategyArchitectRole(engine=engine)
    reviewer = CodeReviewerRole(engine=engine)
    reflector = DecisionReflectorRole(engine=engine)
    # Continuous intel ingestion — every 30 min market hours, hourly
    # after-hours. Source preferences feed the daemon's universe
    # construction at every scan.
    ingestor = IntelIngestorRole(engine=engine)
    # Adaptive thresholds — runs nightly post-reconciler, before pre-market.
    # Reads cfg from the YAML for static fallbacks; tries to load and pass
    # an EmailSender so the operator gets a summary email each morning.
    tuner_cfg = _load_app_cfg_safely()
    tuner_sender = _build_tuner_email_sender(tuner_cfg)
    tuner = ThresholdTunerRole(
        engine=engine,
        cfg=tuner_cfg,
        sender=tuner_sender,
    )

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
        "decision_reflect": _wrap(
            "decision_reflect", lambda: reflector.safe_run(ctx={})
        ),
        "threshold_tune": _wrap(
            "threshold_tune", lambda: tuner.safe_run(ctx={})
        ),
        "intel_ingest": _wrap(
            "intel_ingest", lambda: ingestor.safe_run(ctx={})
        ),
        "saturday_evolve": _saturday_evolve,
    }


def _load_app_cfg_safely():
    """Best-effort load of strategy/config.yaml. Returns None if anything
    goes wrong — the threshold tuner only needs cfg for static fallbacks
    on knobs that haven't tuned yet, so a missing cfg just means more
    knobs sit at their pydantic defaults."""
    try:
        from pathlib import Path
        from trading_bot.shared.config import load_config
        return load_config(Path("strategy/config.yaml"))
    except Exception:
        return None


def _build_tuner_email_sender(cfg):
    """Construct an EmailSender if SMTP creds + a recipient are configured.
    Returns None to disable email cleanly when anything is missing — the
    tuner role will skip the email step but still write overrides.
    """
    if cfg is None:
        return None
    try:
        from trading_bot.shared.config import Settings
        from trading_bot.email_sender import EmailSender
        settings = Settings()
        # Settings exposes Gmail SMTP creds via env vars; if either is unset,
        # bail out rather than constructing a sender that will crash on send.
        user = getattr(settings, "gmail_user", None) or os.environ.get("GMAIL_USER")
        pw = getattr(settings, "gmail_app_password", None) or os.environ.get("GMAIL_APP_PASSWORD")
        if not user or not pw:
            return None
        return EmailSender(user=user, app_password=pw, to=cfg.email.to)
    except Exception:
        return None


def _register_lab_jobs(scheduler: BackgroundScheduler, runners: dict) -> None:
    _attach_history_listener(scheduler)
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
    # Nightly post-mortem on closed trades. Runs after the daemon's
    # evening reconciler (21:55 ET) has settled, before pre-market
    # premarket_rank job kicks in. Latency-tolerant — when the mailbox
    # env flag is set, lessons are produced via the Claude Code
    # subscription routine instead of the API.
    scheduler.add_job(
        runners["decision_reflect"],
        trigger=CronTrigger(hour=3, minute=30, timezone="America/New_York"),
        id="decision_reflect",
        replace_existing=True,
    )
    # Adaptive thresholds — nightly tuner runs at 04:00 ET, after the
    # decision_reflector (03:30) has produced lessons and before the
    # daemon's pre-market jobs start. Output: rows in
    # ``threshold_overrides`` for auto-mode knobs, recommendations JSON
    # for the operator on recommend-mode knobs.
    if "threshold_tune" in runners:
        scheduler.add_job(
            runners["threshold_tune"],
            trigger=CronTrigger(hour=4, minute=0, timezone="America/New_York"),
            id="threshold_tune",
            replace_existing=True,
        )
    # Continuous intel ingestion. Two cron jobs share the same callable:
    #   * Every 30 min during US market hours (09:00-16:00 ET, weekdays).
    #   * Every hour outside that window (so overnight news still rolls in).
    # Cron-level split keeps the daemon honest and lets the operator see
    # exactly when each tick fires; APScheduler registers them as two
    # distinct jobs.
    if "intel_ingest" in runners:
        scheduler.add_job(
            runners["intel_ingest"],
            trigger=CronTrigger(
                day_of_week="mon-fri", hour="9-15", minute="0,30",
                timezone="America/New_York",
            ),
            id="intel_ingest_market",
            replace_existing=True,
        )
        scheduler.add_job(
            runners["intel_ingest"],
            trigger=CronTrigger(
                hour="0-8,16-23", minute=0,
                timezone="America/New_York",
            ),
            id="intel_ingest_offhours",
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
