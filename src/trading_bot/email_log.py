"""Wraps EmailSender.send() to journal every email send to state.db.

The single source of truth for "did we send this email?". Used by the
digest's System Health section and by ad-hoc debugging.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


def _emit_log_event(*, sent_at: dt.datetime, kind: str, subject: str,
                    recipient: str, outcome: str) -> None:
    print(json.dumps({
        "ts": sent_at.isoformat(),
        "role": "email_log",
        "event": "email_sent",
        "level": "info" if outcome == "ok" else "warn",
        "kind": kind,
        "subject": subject,
        "recipient": recipient,
        "outcome": outcome,
    }), file=sys.stderr, flush=True)


class EmailLogStore:
    """Append-only log of every email send attempt."""

    def __init__(self, db_path: Path | str = "data/state.db") -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)

    def record(self, *, sent_at: dt.datetime, kind: str, subject: str,
               recipient: str, outcome: str) -> None:
        with self._engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO emails_sent (sent_at, kind, subject, recipient, outcome) "
                    "VALUES (:sent_at, :kind, :subject, :recipient, :outcome)"
                ),
                {"sent_at": sent_at, "kind": kind, "subject": subject,
                 "recipient": recipient, "outcome": outcome},
            )

    def since(self, since_ts: dt.datetime) -> list[dict[str, Any]]:
        with self._engine.begin() as c:
            rows = c.execute(
                text("SELECT sent_at, kind, subject, recipient, outcome "
                     "FROM emails_sent WHERE sent_at >= :since ORDER BY sent_at"),
                {"since": since_ts},
            ).mappings().all()
        return [dict(r) for r in rows]

    def count_by_kind_since(self, since_ts: dt.datetime) -> dict[str, int]:
        with self._engine.begin() as c:
            rows = c.execute(
                text("SELECT kind, COUNT(*) AS n FROM emails_sent "
                     "WHERE sent_at >= :since GROUP BY kind"),
                {"since": since_ts},
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    def failures_since(self, since_ts: dt.datetime) -> list[dict[str, Any]]:
        with self._engine.begin() as c:
            rows = c.execute(
                text("SELECT sent_at, kind, subject, recipient, outcome "
                     "FROM emails_sent WHERE sent_at >= :since AND outcome = 'failed' "
                     "ORDER BY sent_at"),
                {"since": since_ts},
            ).mappings().all()
        return [dict(r) for r in rows]


def send_logged(
    *,
    sender: Any,  # EmailSender — duck-typed so tests can mock
    subject: str,
    html_body: str,
    kind: str,
    recipient: str,
    store: EmailLogStore | None = None,
) -> None:
    """Send via EmailSender.send() and record the attempt to state.db.

    Always re-raises send failures (caller decides what to do); always
    records the attempt before re-raising.
    """
    store = store or EmailLogStore()
    now = dt.datetime.now(dt.timezone.utc)
    try:
        sender.send(subject=subject, html_body=html_body)
        store.record(sent_at=now, kind=kind, subject=subject,
                     recipient=recipient, outcome="ok")
        _emit_log_event(sent_at=now, kind=kind, subject=subject,
                        recipient=recipient, outcome="ok")
    except Exception:
        store.record(sent_at=now, kind=kind, subject=subject,
                     recipient=recipient, outcome="failed")
        _emit_log_event(sent_at=now, kind=kind, subject=subject,
                        recipient=recipient, outcome="failed")
        raise
