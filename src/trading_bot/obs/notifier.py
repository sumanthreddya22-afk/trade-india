"""Email alerts via Gmail SMTP.

Used for:
  * daily digest (~23:55 ET, after reconciliation)
  * kill-switch fires (immediate)
  * daemon startup failures (immediate)
  * manual halt by operator (immediate, audit trail)

Configuration: reads ``GMAIL_USER`` / ``GMAIL_APP_PASSWORD`` from env
(via .env). If either is missing, ``send_alert`` becomes a no-op
returning ``{"ok": False, "reason": "creds_missing"}`` — never raises,
so the daemon never crashes due to an alert problem.

Throttling: simple in-process dedup. Two messages with the same
(subject, severity) within 5 minutes deduplicate to one send. Operator
sees the latest message body.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import smtplib
import threading
from email.mime.text import MIMEText
from typing import Any, Optional

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

_dedup_lock = threading.Lock()
_recent_sends: dict[str, dt.datetime] = {}
_DEDUP_WINDOW = dt.timedelta(minutes=5)


def _gmail_creds() -> tuple[str, str] | None:
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not user or not pw:
        return None
    return user, pw


def _to_address() -> str:
    """Default destination: same address as GMAIL_USER (self-alert).
    Operator can override via TRADING_BOT_ALERT_TO."""
    return os.environ.get("TRADING_BOT_ALERT_TO", "").strip() \
        or os.environ.get("GMAIL_USER", "").strip()


def _should_send(dedup_key: str, now: dt.datetime) -> bool:
    with _dedup_lock:
        last = _recent_sends.get(dedup_key)
        if last and (now - last) < _DEDUP_WINDOW:
            return False
        _recent_sends[dedup_key] = now
        # Prune old entries periodically.
        if len(_recent_sends) > 200:
            cutoff = now - _DEDUP_WINDOW * 2
            for k, v in list(_recent_sends.items()):
                if v < cutoff:
                    _recent_sends.pop(k, None)
    return True


def send_alert(
    *, subject: str, body: str, severity: str = "INFO",
    dedup_key: Optional[str] = None,
) -> dict[str, Any]:
    """Send one email. Best-effort: never raises. Returns a result dict
    the caller can log."""
    creds = _gmail_creds()
    if creds is None:
        return {"ok": False, "reason": "creds_missing"}
    to_addr = _to_address()
    if not to_addr:
        return {"ok": False, "reason": "to_addr_missing"}
    now = dt.datetime.now(dt.timezone.utc)
    key = dedup_key or f"{severity}:{subject}"
    if not _should_send(key, now):
        return {"ok": True, "deduplicated": True, "dedup_key": key}

    user, pw = creds
    msg = MIMEText(body or "(no body)", "plain", "utf-8")
    msg["Subject"] = f"[trading-bot:{severity}] {subject}"
    msg["From"] = user
    msg["To"] = to_addr
    msg["Date"] = now.strftime("%a, %d %b %Y %H:%M:%S +0000")

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        log.info("alert sent: %s severity=%s to=%s", subject, severity, to_addr)
        return {"ok": True, "to": to_addr, "subject": subject, "severity": severity}
    except Exception as e:  # noqa: BLE001
        log.warning("alert send failed: %s: %s", subject, e)
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def send_kill_switch_alert(*, detector: str, reason: str, actor: str) -> dict:
    return send_alert(
        subject=f"KILL SWITCH FIRED — {detector}",
        body=(
            f"A kill switch fired on trading-bot v4.\n\n"
            f"  Detector: {detector}\n"
            f"  Actor:    {actor}\n"
            f"  Reason:   {reason}\n"
            f"  Time:     {dt.datetime.now(dt.timezone.utc).isoformat()}\n\n"
            f"All new entries are halted. Existing positions can only exit.\n"
            f"Inspect via the dashboard: http://127.0.0.1:8765/\n"
        ),
        severity="ALERT",
        dedup_key=f"kill_switch:{detector}",
    )


def send_daily_digest(*, digest_text: str) -> dict:
    return send_alert(
        subject=f"Daily digest — {dt.date.today().isoformat()}",
        body=digest_text,
        severity="INFO",
        dedup_key=f"digest:{dt.date.today().isoformat()}",
    )


def send_daemon_startup_failure(*, error: str) -> dict:
    return send_alert(
        subject="DAEMON STARTUP FAILED",
        body=(
            f"trading-bot v4 daemon failed to start.\n\n"
            f"Error: {error}\n\n"
            f"Time: {dt.datetime.now(dt.timezone.utc).isoformat()}\n\n"
            f"Check logs: tail -F data/daemon.log\n"
        ),
        severity="ALERT",
    )


def send_manual_halt_alert(*, operator: str, reason: str) -> dict:
    return send_alert(
        subject=f"manual halt by {operator}",
        body=f"Operator: {operator}\nReason: {reason}\nTime: {dt.datetime.now(dt.timezone.utc)}",
        severity="WARN",
        dedup_key=f"manual_halt:{operator}",
    )


def send_drift_alert(
    *,
    lane: str,
    n_trades: int,
    modelled_mean_bps: float,
    realised_mean_bps: float,
    ratio: float,
    recommendation: str,
) -> dict:
    """Fired by ``job_drift_monitor`` when realised slippage on a lane
    exceeds the modelled-pessimistic mean by more than the tolerance
    multiplier. The dedup_key is ``drift:<lane>:<UTC-date>`` so the
    operator sees one alert per lane per nightly run, even if the
    same breach persists for several nights."""
    return send_alert(
        subject=f"DRIFT BREACH — lane={lane} ratio={ratio:.2f}x",
        body=(
            f"Slippage drift exceeded tolerance on {lane}.\n\n"
            f"  Trades in window: {n_trades}\n"
            f"  Modelled mean:    {modelled_mean_bps:.2f} bps\n"
            f"  Realised mean:    {realised_mean_bps:.2f} bps\n"
            f"  Ratio:            {ratio:.2f}x\n"
            f"  Recommendation:   {recommendation or '(none)'}\n"
            f"  Time:             {dt.datetime.now(dt.timezone.utc).isoformat()}\n\n"
            f"Per Plan v4 §9 the lane should be demoted to observe-only "
            f"until slippage normalises. Inspect via the dashboard or "
            f"the drift_event table.\n"
        ),
        severity="ALERT",
        dedup_key=f"drift:{lane}:{dt.date.today().isoformat()}",
    )


__all__ = [
    "send_alert", "send_daemon_startup_failure", "send_daily_digest",
    "send_drift_alert", "send_kill_switch_alert", "send_manual_halt_alert",
]
