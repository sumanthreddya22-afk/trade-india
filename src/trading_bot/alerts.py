"""Action Alert framework with 20-min throttling.

Behavior:
- `queue_alert(event)` writes to alerts_pending. If last_alert_sent_at is
  None or > 20 min ago, drains immediately. Else the alert sits in queue.
- `drain_alerts()` claims all pending rows, sends a single email
  (single-event subject if N==1, batch subject if N>1), updates
  last_alert_sent_at.
- `dedup_key` is UNIQUE in alerts_pending; same key won't queue twice.

Severity bypass: severity="bad" drains immediately even if < 20 min have
passed since the last send — critical alerts (daemon_critical, stop_hit)
must not be throttled.

Called by every alert source via `queue_alert` (verify_stops, vip_scan,
portfolio_watch, fill notifications, daemon stall after recovery
window). The alert_drain cron job runs every 1 min to handle queued
alerts post-throttle.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from sqlalchemy import create_engine, text

from trading_bot.email_shell import (
    render_shell, section, footer,
    _BAD, _WARN, _INFO, _GOOD_LIGHT, _TEXT_PRIMARY, _TEXT_SECONDARY,
)


_THROTTLE_MIN = 20
_KEY_LAST_SENT = "last_alert_sent_at"


@dataclass(frozen=True)
class AlertEvent:
    kind: Literal["fill", "stop_hit", "auto_protect_summary",
                  "vip_tweet", "daemon_critical", "portfolio_anomaly",
                  "wheel_csp_opened", "wheel_cc_opened", "wheel_take_profit",
                  "wheel_dte_close", "wheel_roll", "wheel_assignment",
                  "wheel_called_away", "wheel_allocation_cap",
                  "wheel_chain_fetch_failure"]
    severity: Literal["info", "warn", "bad"]
    title: str
    detail_html: str
    fired_at: dt.datetime
    dedup_key: str


class AlertStore:
    def __init__(self, db_path: Path | str = "data/state.db") -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)

    def queue(self, event: AlertEvent) -> bool:
        """Insert into alerts_pending. Returns True if newly queued, False if dedup'd."""
        with self._engine.begin() as c:
            res = c.execute(
                text("INSERT OR IGNORE INTO alerts_pending "
                     "(queued_at, kind, severity, title, detail_html, dedup_key) "
                     "VALUES (:queued_at, :kind, :severity, :title, :detail, :dedup)"),
                {"queued_at": event.fired_at, "kind": event.kind,
                 "severity": event.severity, "title": event.title,
                 "detail": event.detail_html, "dedup": event.dedup_key},
            )
            return (res.rowcount or 0) > 0

    def claim_pending(self) -> list[AlertEvent]:
        """Atomically read + delete all pending rows. Returns the events."""
        with self._engine.begin() as c:
            rows = c.execute(text(
                "SELECT queued_at, kind, severity, title, detail_html, dedup_key "
                "FROM alerts_pending ORDER BY queued_at"
            )).mappings().all()
            if not rows:
                return []
            c.execute(text("DELETE FROM alerts_pending"))
            return [
                AlertEvent(
                    kind=r["kind"], severity=r["severity"],
                    title=r["title"], detail_html=r["detail_html"],
                    fired_at=dt.datetime.fromisoformat(str(r["queued_at"])),
                    dedup_key=r["dedup_key"],
                ) for r in rows
            ]

    def get_last_sent(self) -> dt.datetime | None:
        with self._engine.begin() as c:
            row = c.execute(text("SELECT value FROM bot_meta WHERE key = :k"),
                            {"k": _KEY_LAST_SENT}).first()
        return dt.datetime.fromisoformat(row[0]) if row else None

    def set_last_sent(self, ts: dt.datetime) -> None:
        with self._engine.begin() as c:
            c.execute(text(
                "INSERT INTO bot_meta (key, value) VALUES (:k, :v) "
                "ON CONFLICT(key) DO UPDATE SET value = :v"
            ), {"k": _KEY_LAST_SENT, "v": ts.isoformat()})

    def record_send(self, *, sent_at: dt.datetime, subject: str,
                    event_count: int, max_severity: str) -> None:
        with self._engine.begin() as c:
            c.execute(text(
                "INSERT INTO alerts_sent (sent_at, subject, event_count, max_severity) "
                "VALUES (:sent_at, :subject, :n, :sev)"
            ), {"sent_at": sent_at, "subject": subject,
                "n": event_count, "sev": max_severity})


_SEV_ORDER = {"info": 0, "warn": 1, "bad": 2}


def _max_severity(events: list[AlertEvent]) -> str:
    return max((e.severity for e in events), key=lambda s: _SEV_ORDER.get(s, 0))


def _build_alert_email_html(events: list[AlertEvent], *, now: dt.datetime) -> tuple[str, str]:
    """Returns (subject, html_body) for a 1-event single email or N-event batch."""
    sev = _max_severity(events)
    sev_label = sev.upper()
    if len(events) == 1:
        e = events[0]
        subject = f"[{sev_label}] {e.title}"
    else:
        kinds = sorted({e.kind for e in events})
        subject = (
            f"[{sev_label}] {len(events)} alerts · "
            f"{', '.join(kinds)}"
        )

    sections = []
    for e in events:
        kind_label = e.kind.replace("_", " ").upper()
        sections.append(section(
            title=f"{kind_label} — {e.title}",
            glyph={"bad": "⚠", "warn": "▲", "info": "●"}.get(e.severity, "●"),
            body=e.detail_html,
            severity={"bad": "bad", "warn": "warn", "info": "info"}.get(e.severity, "info"),
        ))
    sections.append(footer(version="phase4-v1", git_sha="HEAD"))

    status = {"info": "ok", "warn": "warn", "bad": "bad"}.get(sev, "ok")
    html = render_shell(
        title=f"Action Alert · {len(events)} event{'s' if len(events) != 1 else ''}",
        status=status,
        timestamp_et=now.strftime("%a, %b %d · %H:%M ET"),
        body_sections=sections,
    )
    return subject, html


def queue_alert(
    event: AlertEvent,
    *,
    store: AlertStore | None = None,
    sender_send: Callable[..., None] | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Insert alert into queue. If last send was > 20 min ago (or never),
    or severity is "bad" (critical), drain immediately.
    Else leave it queued for the alert_drain cron."""
    store = store or AlertStore()
    now = now or dt.datetime.now(dt.timezone.utc)

    is_new = store.queue(event)
    if not is_new:
        return  # dedup'd

    last_sent = store.get_last_sent()
    is_critical = event.severity == "bad"
    if (is_critical
            or last_sent is None
            or now - last_sent >= dt.timedelta(minutes=_THROTTLE_MIN)):
        drain_alerts(store=store, sender_send=sender_send, now=now)


def drain_alerts(
    *,
    store: AlertStore | None = None,
    sender_send: Callable[..., None] | None = None,
    now: dt.datetime | None = None,
) -> int:
    """Send all queued alerts as a single (or batched) email. Returns
    count of events sent. No-op if queue is empty."""
    store = store or AlertStore()
    now = now or dt.datetime.now(dt.timezone.utc)

    events = store.claim_pending()
    if not events:
        return 0

    subject, html = _build_alert_email_html(events, now=now)
    if sender_send is not None:
        sender_send(subject=subject, html_body=html)
    else:
        # Production path: route through send_logged.
        from trading_bot.config import Settings, load_config
        from trading_bot.email_log import send_logged
        from trading_bot.email_sender import EmailSender
        from pathlib import Path as _Path
        _config_path = _Path("strategy/config.yaml")
        s = Settings()
        cfg = load_config(_config_path)
        sender = EmailSender(user=s.gmail_user, app_password=s.gmail_app_password,
                              to=cfg.email.to)
        send_logged(sender=sender, subject=subject, html_body=html,
                    kind="alert", recipient=cfg.email.to)

    store.record_send(sent_at=now, subject=subject, event_count=len(events),
                      max_severity=_max_severity(events))
    store.set_last_sent(now)
    return len(events)
