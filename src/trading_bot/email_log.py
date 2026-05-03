"""Wraps EmailSender.send() to journal every email send to state.db.

The single source of truth for "did we send this email?". Used by the
digest's System Health section and by ad-hoc debugging.

Includes a per-kind cooldown layer (Phase: 2026-05-03 — added after
the operator was buried under repeated digests, status updates, and
place/skip entry-debate pairs for MSFT every 15 min). When a (kind,
dedup_key) was sent within ``cooldown_seconds``, the next send is
suppressed and recorded with outcome='suppressed' so the audit log
shows what we dropped.

Defaults err on the side of fewer emails. Callers that want the old
fire-on-every-call behavior can pass ``cooldown_seconds=0``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-kind default cooldowns (in seconds)
# ---------------------------------------------------------------------------
# Each kind specifies how long to wait before sending another email with
# the same dedup_key. When dedup_key isn't supplied the subject is used
# as the dedup_key (so identical subjects get suppressed within the
# cooldown — exactly what the operator wants for "Daily Digest · May 03"
# firing 9 times today).

_DEFAULT_COOLDOWNS_SEC: dict[str, int] = {
    # Once-per-day kinds. 18h gives slack for daylight-savings shifts +
    # midnight-spanning runs without letting a same-day repeat through.
    "digest":         18 * 3600,
    "status":         18 * 3600,
    "nightly_review": 18 * 3600,
    # Per-symbol decision kinds. 60 min lets the same (symbol, verdict)
    # surface twice an hour at most. The first place→skip pair from a
    # single scan still fires (different verdicts → different dedup keys);
    # the *next* tick that produces the same pair is suppressed.
    "entry_debate":     60 * 60,
    "unblock_debate":  120 * 60,  # borderline-cap rejections rarely change in 2h
    # Generic alerts — already have their own dedup_key in alerts.py but
    # add a safety net so a buggy producer can't flood the inbox.
    "alert":            4 * 3600,
    # Per-fill kinds — never suppress, the operator wants every fill.
    "fill":             0,
    "order":            0,
    "critical":         0,         # never gate critical alerts
    "promotion":        4 * 3600,
    "midday":          18 * 3600,
}


def cooldown_for(kind: str) -> int:
    """Return the suppression cooldown in seconds for a given email kind.

    Unknown kinds default to 30 min — defensive: a new kind is more
    likely a misfire than a desired ping until proven otherwise.
    """
    return _DEFAULT_COOLDOWNS_SEC.get(kind, 30 * 60)


def _normalize_dedup_key(kind: str, dedup_key: Optional[str], subject: str) -> str:
    """Compute the (kind, dedup_key) lookup key for the suppression check.

    When the caller doesn't supply a dedup_key we hash the subject so
    "Daily Digest · May 03 · +0.06% · $14,963" suppresses repeats of
    the same subject within the cooldown window.
    """
    if dedup_key:
        return f"{kind}|{dedup_key}"
    h = hashlib.sha1()
    h.update(subject.encode("utf-8"))
    return f"{kind}|sub:{h.hexdigest()[:16]}"


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

    def last_send_at(
        self, *, kind: str, dedup_subject: str, since_ts: dt.datetime,
    ) -> Optional[dt.datetime]:
        """Return the most recent SUCCESSFUL send timestamp for an
        identical ``(kind, subject)`` within ``since_ts`` — used by the
        cooldown check.

        Only ``outcome='ok'`` counts. A 'suppressed' row marks a
        "would have sent but didn't"; it must NOT extend the cooldown
        (otherwise the cooldown becomes effectively infinite once the
        first suppression hits). A 'failed' row also doesn't suppress
        the next attempt — we want to retry after a transport error.
        """
        with self._engine.begin() as c:
            row = c.execute(
                text(
                    "SELECT sent_at FROM emails_sent "
                    "WHERE kind = :kind AND subject = :subject "
                    "  AND sent_at >= :since "
                    "  AND outcome = 'ok' "
                    "ORDER BY sent_at DESC LIMIT 1"
                ),
                {"kind": kind, "subject": dedup_subject, "since": since_ts},
            ).first()
        if row is None:
            return None
        ts = row[0]
        if isinstance(ts, str):
            try:
                ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts


def send_logged(
    *,
    sender: Any,  # EmailSender — duck-typed so tests can mock
    subject: str,
    html_body: str,
    kind: str,
    recipient: str,
    store: EmailLogStore | None = None,
    dedup_key: Optional[str] = None,
    cooldown_seconds: Optional[int] = None,
    now: Optional[dt.datetime] = None,
) -> str:
    """Send via EmailSender.send() and record the attempt to state.db.

    Returns the outcome string: ``"ok"`` on send, ``"suppressed"`` when
    a same-key send happened within the cooldown window, or re-raises on
    transport errors (after recording outcome=failed).

    ``dedup_key`` overrides the default (which hashes the subject so
    identical subjects within ``cooldown_seconds`` are suppressed).
    Pass ``cooldown_seconds=0`` to disable suppression for one call.
    """
    store = store or EmailLogStore()
    now = now or dt.datetime.now(dt.timezone.utc)
    cooldown = (
        cooldown_seconds
        if cooldown_seconds is not None
        else cooldown_for(kind)
    )

    # Suppression: skip the send when the same (kind, dedup_subject)
    # was sent within the cooldown window. We still record the attempt
    # so the operator can audit "what we would have sent" — outcome
    # 'suppressed' shows up in the email-firehose card.
    if cooldown > 0:
        # The subject we use as the lookup key. When dedup_key is set,
        # synthesize a stable subject-equivalent string from it so the
        # last_send_at lookup matches across calls. Otherwise the actual
        # subject is the lookup.
        lookup_subject = (
            f"__dedup__:{dedup_key}" if dedup_key else subject
        )
        cutoff = now - dt.timedelta(seconds=cooldown)
        last = store.last_send_at(
            kind=kind, dedup_subject=lookup_subject, since_ts=cutoff,
        )
        if last is not None:
            store.record(sent_at=now, kind=kind, subject=lookup_subject,
                         recipient=recipient, outcome="suppressed")
            _emit_log_event(sent_at=now, kind=kind, subject=subject,
                             recipient=recipient, outcome="suppressed")
            logger.info(
                "send_logged: suppressed %s (last sent %s, cooldown %ss)",
                kind, last.isoformat(), cooldown,
            )
            return "suppressed"
        # We're going to send; record the lookup_subject too when a
        # dedup_key was supplied so the next suppression check finds it.
        # When dedup_key is None we record the real subject below — that
        # IS the lookup key.
    try:
        sender.send(subject=subject, html_body=html_body)
        # When a dedup_key is supplied, record TWO rows: the actual
        # subject for the operator's email-firehose UI, and the dedup
        # lookup row so the next suppression check finds it. We mark the
        # lookup row as outcome='ok' so it counts as a successful prior
        # send for the cooldown window.
        store.record(sent_at=now, kind=kind, subject=subject,
                     recipient=recipient, outcome="ok")
        if dedup_key:
            store.record(sent_at=now, kind=kind,
                         subject=f"__dedup__:{dedup_key}",
                         recipient=recipient, outcome="ok")
        _emit_log_event(sent_at=now, kind=kind, subject=subject,
                        recipient=recipient, outcome="ok")
        return "ok"
    except Exception:
        store.record(sent_at=now, kind=kind, subject=subject,
                     recipient=recipient, outcome="failed")
        _emit_log_event(sent_at=now, kind=kind, subject=subject,
                        recipient=recipient, outcome="failed")
        raise
