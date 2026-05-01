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
import time as _time_module
import time
from pathlib import Path

from trading_bot.cadence import load_cadence
from trading_bot.log_structured import StructuredLogger
from trading_bot.email_critical import build_critical_email
from trading_bot.state_db import get_engine
from trading_bot.roles.watchdog import WatchdogRole
from trading_bot.roles.account_sentinel import AccountSentinelRole


CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
HEARTBEAT_PATH = Path(os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"))
PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))
DAEMON_PLIST_LABEL = os.environ.get(
    "TRADING_BOT_DAEMON_PLIST", "com.bharath.trading.daemon.paper"
)
ALERT_RECIPIENT = os.environ.get("TRADING_BOT_ALERT_TO", "bharath8887@gmail.com")

_last_alert_at: dict[str, float] = {}
_ALERT_COOLDOWN_SECONDS = 3600


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


def _send_alert(
    log: StructuredLogger,
    *,
    kind: str,
    to: str,
    subject: str,
    html_body: str,
) -> None:
    """Send an alert email, but suppress repeats of the same kind within _ALERT_COOLDOWN_SECONDS.

    For daemon_stall / daemon_critical alerts, routes through queue_alert with
    severity="bad" so the 20-min throttle is bypassed (critical bypass).
    """
    now = _time_module.time()
    last = _last_alert_at.get(kind, 0.0)
    if now - last < _ALERT_COOLDOWN_SECONDS:
        log.event("alert_suppressed", kind=kind, age_seconds=now - last)
        return
    _last_alert_at[kind] = now
    try:
        import datetime as _dt_sa
        from trading_bot.alerts import AlertEvent, queue_alert as _queue_alert_sa
        _queue_alert_sa(AlertEvent(
            kind="daemon_critical",
            severity="bad",
            title=subject,
            detail_html=html_body,
            fired_at=_dt_sa.datetime.now(_dt_sa.timezone.utc),
            dedup_key=f"daemon_critical:{_dt_sa.date.today()}:{kind}",
        ))
    except Exception as e:
        log.error("alert_send_failed", error=e)


def _heartbeat_path() -> Path:
    return Path("data/heartbeat.json")


def _is_heartbeat_fresh(*, max_age_seconds: float = 120.0) -> bool:
    """True if the heartbeat file was updated within max_age_seconds."""
    p = _heartbeat_path()
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    return age < max_age_seconds


def _handle_stall(
    *,
    log: StructuredLogger,
    age_seconds: float,
    kickstart_succeeded: bool,
    send_alert,                # callable(kind, subject, html_body, to)
    is_heartbeat_fresh,        # callable(*, max_age_seconds) -> bool
    sleep,                     # callable(seconds) -> None
) -> None:
    """Decide whether a stall should escalate to an email.

    Quick recoveries (kickstart succeeds and heartbeat resumes within 60s)
    are downgraded to a `daemon_blip_recovered` log event — no email.
    Failed kickstarts and persistent stalls still email immediately.
    """
    # Failed kickstart → immediate email, don't wait for "recovery".
    if not kickstart_succeeded:
        email = build_critical_email(
            title="Daemon stalled",
            detail=(
                f"Heartbeat last seen {age_seconds:.0f}s ago.\n"
                f"Auto-restart attempted via launchctl: failed."
            ),
        )
        send_alert(kind="daemon_stall", to=ALERT_RECIPIENT,
                   subject=email.subject, html_body=email.html_body)
        return

    # Kickstart succeeded — wait briefly to see if heartbeat resumes.
    sleep(60)
    if is_heartbeat_fresh(max_age_seconds=120.0):
        log.event(
            "daemon_blip_recovered",
            stall_duration_seconds=age_seconds,
            recovery_method="kickstart",
        )
        return

    # Heartbeat still stale 60s after kickstart → email.
    email = build_critical_email(
        title="Daemon stalled",
        detail=(
            f"Heartbeat last seen {age_seconds:.0f}s ago.\n"
            f"Auto-restart attempted via launchctl: success — but heartbeat "
            f"did not resume within 60s."
        ),
    )
    send_alert(kind="daemon_stall", to=ALERT_RECIPIENT,
               subject=email.subject, html_body=email.html_body)


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="supervisor")
    log.event("supervisor_boot")

    boot_ts = _time_module.monotonic()
    GRACE_SECONDS = 60

    cadence = load_cadence(CONFIG_PATH)
    stall_max_age = 5 * 60  # spec: > 5 min stale triggers kickstart

    engine = get_engine(STATE_DB)

    watchdog_role = WatchdogRole(
        engine=engine,
        heartbeat_path=HEARTBEAT_PATH,
        max_age_seconds=stall_max_age,
        plist_label=DAEMON_PLIST_LABEL,
    )
    account_sentinel_role = AccountSentinelRole(
        engine=engine, alpaca=_alpaca(),
        pause_flag_path=PAUSE_PATH,
        max_dd_pct=20.0, account="paper",
    )

    last_account_check = 0.0
    last_wheel_stall_check = 0.0
    last_wheel_stall_alert = 0.0

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("supervisor_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    while not stop["flag"]:
        try:
            # 1. Watchdog: every 60s, but skip first GRACE_SECONDS for boot-race avoidance
            now_mono = _time_module.monotonic()
            if now_mono - boot_ts < GRACE_SECONDS:
                # Still in boot grace window — skip watchdog stall check
                pass
            else:
                result = watchdog_role.safe_run(ctx={})
                if result.outputs.get("stalled"):
                    age_seconds = result.outputs.get("age_seconds", 0)
                    log.event("stall_detected", age_seconds=age_seconds)
                    kicked = result.outputs.get("kickstart_attempted", False)
                    log.event("kickstart_attempted", success=kicked)
                    _handle_stall(
                        log=log,
                        age_seconds=age_seconds,
                        kickstart_succeeded=bool(kicked),
                        send_alert=lambda **kw: _send_alert(log, **kw),
                        is_heartbeat_fresh=_is_heartbeat_fresh,
                        sleep=time.sleep,
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
                    result = account_sentinel_role.safe_run(ctx={})
                    equity = result.outputs.get("equity", "0")
                    hwm = result.outputs.get("hwm", 0.0)
                    drawdown_pct = result.outputs.get("drawdown_pct", 0.0)
                    paused = result.outputs.get("paused", False)
                    log.event(
                        "account_check",
                        equity=str(equity),
                        hwm=hwm,
                        drawdown_pct=drawdown_pct,
                        paused=paused,
                    )
                    if paused:
                        email = build_critical_email(
                            title="Drawdown breach — trading paused",
                            detail=(
                                f"Drawdown {drawdown_pct:.2f}% from HWM ${hwm:,.2f}.\n"
                                f"Current equity ${equity}.\n"
                                f"pause.flag written. Daemon will not place new orders."
                            ),
                        )
                        _send_alert(
                            log,
                            kind="drawdown_breach",
                            to=ALERT_RECIPIENT,
                            subject=email.subject,
                            html_body=email.html_body,
                        )
                except Exception as e:
                    log.error("account_check_failed", error=e)
                last_account_check = now

            # 3. Wheel-scan stall watchdog: if a wheel_scan started but
            # never finished and the gap exceeds 15 min, emit a structured
            # event + alert (rate-limited to once per hour).
            if now - last_wheel_stall_check >= 60:
                last_wheel_stall_check = now
                try:
                    from trading_bot.options.wheel_runner import is_wheel_scan_stalled
                    stalled, info = is_wheel_scan_stalled(max_age_seconds=900)
                    if stalled:
                        log.event("wheel_scan_stalled", **info)
                        if now - last_wheel_stall_alert >= _ALERT_COOLDOWN_SECONDS:
                            email = build_critical_email(
                                title=f"wheel_scan stalled {info.get('age_seconds',0)}s",
                                detail=(
                                    f"wheel_scan started at {info.get('started_at')} "
                                    f"and has not emitted finished_at in "
                                    f"{info.get('age_seconds',0)}s "
                                    f"(threshold 900s). The Alpaca options-chain "
                                    f"endpoint is the most likely culprit. The "
                                    f"per-call 30s timeout should bound any one "
                                    f"call, so a stall here suggests a different "
                                    f"hang — investigate the daemon stderr log."
                                ),
                            )
                            _send_alert(
                                log, kind="wheel_scan_stalled",
                                to=ALERT_RECIPIENT, subject=email.subject,
                                html_body=email.html_body,
                            )
                            last_wheel_stall_alert = now
                except Exception as e:
                    log.error("wheel_stall_check_failed", error=e)

        except Exception as e:
            log.error("supervisor_loop_error", error=e)

        time.sleep(cadence.watchdog_seconds)

    log.event("supervisor_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
