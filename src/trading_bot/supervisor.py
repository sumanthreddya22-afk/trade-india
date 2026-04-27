"""Supervisor entrypoint. Independent verification process under launchd.

Usage:
    python -m trading_bot.supervisor

Runs every 60s:
- Watchdog: heartbeat staleness → kickstart daemon + email.
- Account Sentinel (every 5 min during market hours): drawdown breach → pause.flag + email.
- Independently queries Alpaca, does not trust daemon's view.
"""
from __future__ import annotations

import datetime as dt
import os
import signal
import sys
import time
from pathlib import Path

from trading_bot.cadence import load_cadence
from trading_bot.log_structured import StructuredLogger
from trading_bot.email_critical import build_critical_email
from trading_bot.state_db import get_engine
from trading_bot.watchdog_account import AccountSentinel
from trading_bot.watchdog_stall import StallDetector


CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
HEARTBEAT_PATH = Path(os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"))
PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))
DAEMON_PLIST_LABEL = os.environ.get(
    "TRADING_BOT_DAEMON_PLIST", "com.bharath.trading.daemon.paper"
)
ALERT_RECIPIENT = os.environ.get("TRADING_BOT_ALERT_TO", "bharath8887@gmail.com")


def _is_market_hours_et() -> bool:
    """09:30-16:00 ET, Mon-Fri. Approximate via UTC offset; APScheduler handles DST."""
    import zoneinfo
    now = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def _alpaca():
    """Lazy-build Alpaca client when needed (handle absent creds in tests)."""
    from trading_bot.alpaca_client import AlpacaClient
    from trading_bot.config import Settings
    return AlpacaClient(Settings())


def _send_alert(log: StructuredLogger, *, to: str, subject: str, html_body: str) -> None:
    """Send an alert email; log and swallow any transport failures."""
    try:
        from trading_bot.config import Settings
        from trading_bot.email_sender import EmailSender
        s = Settings()
        EmailSender(user=s.gmail_user, app_password=s.gmail_app_password, to=to).send(
            subject=subject, html_body=html_body
        )
        log.event("alert_sent", to=to, subject=subject)
    except Exception as e:
        log.error("alert_send_failed", error=e)


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="supervisor")
    log.event("supervisor_boot")

    cadence = load_cadence(CONFIG_PATH)
    stall_max_age = 5 * 60  # spec: > 5 min stale triggers kickstart

    stall_detector = StallDetector(
        heartbeat_path=HEARTBEAT_PATH,
        max_age_seconds=stall_max_age,
        plist_label=DAEMON_PLIST_LABEL,
    )

    engine = get_engine(STATE_DB)
    last_account_check = 0.0

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("supervisor_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    while not stop["flag"]:
        try:
            # 1. Watchdog: every 60s
            verdict = stall_detector.check()
            if verdict.is_stalled:
                log.event("stall_detected", age_seconds=verdict.age_seconds)
                kicked = stall_detector.kickstart_daemon()
                log.event("kickstart_attempted", success=kicked)
                email = build_critical_email(
                    title="Daemon stalled",
                    detail=(
                        f"Heartbeat last seen {verdict.age_seconds:.0f}s ago "
                        f"(threshold {stall_max_age}s).\n"
                        f"Auto-restart attempted via launchctl: "
                        f"{'success' if kicked else 'failed'}."
                    ),
                )
                _send_alert(
                    log,
                    to=ALERT_RECIPIENT,
                    subject=email.subject,
                    html_body=email.html_body,
                )

            # 2. Account Sentinel: 5 min during market hours, 30 min off-hours
            interval = (
                cadence.account_sentinel_minutes_market
                if _is_market_hours_et()
                else cadence.account_sentinel_minutes_offhours
            )
            now = time.time()
            if now - last_account_check >= interval * 60:
                try:
                    acct_sentinel = AccountSentinel(
                        engine=engine,
                        alpaca=_alpaca(),
                        pause_flag_path=PAUSE_PATH,
                        max_dd_pct=20.0,
                        account="paper",
                    )
                    av = acct_sentinel.check()
                    log.event(
                        "account_check",
                        equity=str(av.equity),
                        hwm=av.hwm,
                        drawdown_pct=av.drawdown_pct,
                        paused=av.paused,
                    )
                    if av.paused:
                        email = build_critical_email(
                            title="Drawdown breach — trading paused",
                            detail=(
                                f"Drawdown {av.drawdown_pct:.2f}% from HWM ${av.hwm:,.2f}.\n"
                                f"Current equity ${av.equity:,.2f}.\n"
                                f"pause.flag written. Daemon will not place new orders."
                            ),
                        )
                        _send_alert(
                            log,
                            to=ALERT_RECIPIENT,
                            subject=email.subject,
                            html_body=email.html_body,
                        )
                except Exception as e:
                    log.error("account_check_failed", error=e)
                last_account_check = now

        except Exception as e:
            log.error("supervisor_loop_error", error=e)

        time.sleep(cadence.watchdog_seconds)

    log.event("supervisor_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
