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
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))


def _load_runners(log: StructuredLogger):
    """Instantiate Role objects and return runner callables that wrap role.safe_run(ctx)."""
    from trading_bot.state_db import get_engine
    from trading_bot.roles.health_pulse import HealthPulseRole
    from trading_bot.roles.stock_scanner import StockScannerRole
    from trading_bot.roles.crypto_scanner import CryptoScannerRole
    from trading_bot.roles.portfolio_monitor import PortfolioMonitorRole
    from trading_bot.roles.order_steward import OrderStewardRole
    from trading_bot.roles.sentiment_analyst import SentimentAnalystRole
    from trading_bot.roles.universe_curator import UniverseCuratorRole
    from trading_bot.roles.vip_listener import VipListenerRole
    from trading_bot.roles.reporter import ReporterRole

    config_version = "phase2-v1"

    # Build the engine once — roles hold it for KPI persistence across calls.
    engine = get_engine(STATE_DB)

    # Instantiate Role objects once (not per call) so SQLAlchemy engine is stable.
    health_pulse = HealthPulseRole(engine=engine, heartbeat_path=HEARTBEAT_PATH, version=config_version)
    stock_scanner = StockScannerRole(engine=engine)
    crypto_scanner = CryptoScannerRole(engine=engine)
    portfolio_monitor = PortfolioMonitorRole(engine=engine)
    order_steward = OrderStewardRole(engine=engine)
    sentiment_analyst = SentimentAnalystRole(engine=engine)
    universe_curator = UniverseCuratorRole(engine=engine)
    vip_listener = VipListenerRole(engine=engine)
    reporter = ReporterRole(engine=engine)

    def _heartbeat():
        health_pulse.safe_run(ctx={})
        # Also write the legacy heartbeat file so supervisor's StallDetector still works.
        write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action="heartbeat")

    def _wrap(name: str, role_fn):
        """Wrap a role callable with pause-flag check and heartbeat update."""
        def runner():
            log.event(f"{name}_start")
            # Block any job that may place orders.
            # midday_report invokes rich-report which scans + trades.
            # daily_digest invokes eod-report (read-only) so it is safe during pause.
            if is_paused(PAUSE_PATH) and name in {"intel_scan", "crypto_scan", "midday_report"}:
                log.event(f"{name}_skipped", reason="pause.flag set")
                write_heartbeat(HEARTBEAT_PATH, version=config_version,
                                last_action=f"{name}_skipped_paused")
                return
            try:
                role_fn()
                log.event(f"{name}_finish")
            except Exception as e:
                log.error(f"{name}_failed", error=e)
            finally:
                write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action=name)
        return runner

    return {
        "heartbeat": _heartbeat,
        "intel_scan": _wrap("intel_scan", lambda: stock_scanner.safe_run(ctx={})),
        "crypto_scan": _wrap("crypto_scan", lambda: crypto_scanner.safe_run(ctx={})),
        "portfolio_watch": _wrap("portfolio_watch", lambda: portfolio_monitor.safe_run(ctx={})),
        "verify_stops": _wrap("verify_stops", lambda: order_steward.safe_run(ctx={})),
        "news_warm": _wrap("news_warm", lambda: sentiment_analyst.safe_run(ctx={})),
        "massive_refresh": _wrap("massive_refresh", lambda: universe_curator.run_refresh(ctx={})),
        "premarket_rank": _wrap("premarket_rank", lambda: universe_curator.run_rank(ctx={})),
        "vip_scan": _wrap("vip_scan", lambda: vip_listener.safe_run(ctx={})),
        "midday_report": _wrap("midday_report", lambda: reporter.run_midday(ctx={})),
        "daily_digest": _wrap("daily_digest", lambda: reporter.run_eod(ctx={})),
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
