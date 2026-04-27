"""Daemon entrypoint. Long-running process under launchd.

Usage:
    python -m trading_bot.daemon

Reads paper_active.json, runs Alembic migrations, registers APScheduler
jobs, runs forever. Heartbeat fires every cadence.heartbeat_seconds.
On SIGTERM, gracefully stops scheduler and exits 0.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from trading_bot.cadence import load_cadence
from trading_bot.log_structured import StructuredLogger
from trading_bot.scheduler_jobs import register_jobs
from trading_bot.state_heartbeat import write_heartbeat
from trading_bot.state_pause import is_paused


CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
HEARTBEAT_PATH = Path(os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"))
PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))


def _load_runners(log: StructuredLogger):
    """Wraps existing CLI command functions, plus heartbeat."""
    # Lazy imports so daemon module can be imported in tests without side effects
    from trading_bot import cli as cli_mod

    config_version = "phase1-v1"

    def _heartbeat():
        write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action="heartbeat")

    def _wrap(name: str, fn: callable):
        def runner():
            log.event(f"{name}_start")
            if is_paused(PAUSE_PATH) and name in {"intel_scan", "crypto_scan"}:
                log.event(f"{name}_skipped", reason="pause.flag set")
                write_heartbeat(HEARTBEAT_PATH, version=config_version,
                                last_action=f"{name}_skipped_paused")
                return
            try:
                fn()
                log.event(f"{name}_finish")
            except Exception as e:
                log.error(f"{name}_failed", error=e)
            finally:
                write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action=name)
        return runner

    # Click command callbacks. Each is a callable that does its work.
    # NOTE: rank is registered as @main.command("rank") but the Python function is
    # rank_command — use cli_mod.rank_command.callback() not cli_mod.rank.callback().
    # news_warm and massive_refresh have CLI options with defaults; pass them explicitly.
    return {
        "heartbeat": _heartbeat,
        "intel_scan": _wrap("intel_scan", lambda: cli_mod.intel_scan.callback()),
        "crypto_scan": _wrap("crypto_scan", lambda: cli_mod.crypto_scan.callback()),
        "portfolio_watch": _wrap("portfolio_watch", lambda: cli_mod.portfolio_watch.callback()),
        "verify_stops": _wrap("verify_stops", lambda: cli_mod.verify_stops.callback()),
        "news_warm": _wrap("news_warm", lambda: cli_mod.news_warm.callback(lookback_days=3)),
        "massive_refresh": _wrap("massive_refresh", lambda: cli_mod.massive_refresh.callback(days=5, news=False)),
        "premarket_rank": _wrap("premarket_rank", lambda: cli_mod.rank_command.callback()),
        "vip_scan": _wrap("vip_scan", lambda: cli_mod.vip_scan.callback()),
        "daily_digest": _wrap("daily_digest", lambda: cli_mod.full_run.callback()),
    }


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="daemon")
    log.event("daemon_boot", config_path=str(CONFIG_PATH))

    if not CONFIG_PATH.exists():
        log.error(
            "daemon_no_config",
            error=FileNotFoundError(f"config missing: {CONFIG_PATH}"),
        )
        return 1

    cadence = load_cadence(CONFIG_PATH)
    log.event("cadence_loaded",
              heartbeat=cadence.heartbeat_seconds,
              stock_scanner_minutes=cadence.stock_scanner_minutes)

    sched = BackgroundScheduler(timezone="America/New_York")
    runners = _load_runners(log)
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("daemon_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    # Initial heartbeat before scheduler runs (so supervisor doesn't see stale boot)
    runners["heartbeat"]()

    sched.start()
    log.event("scheduler_started", jobs=[j.id for j in sched.get_jobs()])

    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        log.event("daemon_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
