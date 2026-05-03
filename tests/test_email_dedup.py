"""Tests for the per-kind email cooldown / dedup layer in send_logged.

Covers:
  - Default cooldowns per kind (digest 18h, entry_debate 60min, etc.)
  - Suppression returns "suppressed" + writes audit row
  - Different dedup_keys within the same kind don't collide
  - cooldown_seconds=0 disables suppression for one call
  - Subject hash fallback when no dedup_key supplied
  - Failed sends still record the failure (no suppression on errors)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, List

import pytest
from sqlalchemy import create_engine, text

from trading_bot.email_log import (
    EmailLogStore,
    cooldown_for,
    send_logged,
)


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """Build a minimal emails_sent table — same DDL as migration 016."""
    db = tmp_path / "state.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text(
            """
            CREATE TABLE emails_sent (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sent_at DATETIME NOT NULL,
              kind TEXT NOT NULL,
              subject TEXT NOT NULL,
              recipient TEXT NOT NULL,
              outcome TEXT NOT NULL
            )
            """
        ))
    return db


class _RecordingSender:
    """Mock EmailSender; records every send call."""
    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.fail_next = False

    def send(self, *, subject: str, html_body: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated SMTP error")
        self.calls.append({"subject": subject, "html_body_len": len(html_body)})


def _store(db: Path) -> EmailLogStore:
    return EmailLogStore(db_path=str(db))


def _all_rows(db: Path) -> list[dict]:
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT sent_at, kind, subject, outcome FROM emails_sent ORDER BY id"
        )).mappings().all()]


# ---------------------------------------------------------------------------
# cooldown_for defaults
# ---------------------------------------------------------------------------


def test_cooldown_for_known_kinds():
    assert cooldown_for("digest") == 18 * 3600
    assert cooldown_for("status") == 18 * 3600
    assert cooldown_for("entry_debate") == 60 * 60
    assert cooldown_for("alert") == 4 * 3600
    assert cooldown_for("fill") == 0           # fills always fire


def test_cooldown_unknown_kind_defaults_to_30min():
    assert cooldown_for("unheard-of") == 30 * 60


# ---------------------------------------------------------------------------
# Suppression behaviour
# ---------------------------------------------------------------------------


def test_first_send_goes_through(state_db):
    sender = _RecordingSender()
    out = send_logged(
        sender=sender, subject="hi", html_body="<b>hi</b>",
        kind="digest", recipient="me@example.com",
        store=_store(state_db),
    )
    assert out == "ok"
    assert len(sender.calls) == 1
    rows = _all_rows(state_db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"


def test_repeat_within_cooldown_is_suppressed(state_db):
    sender = _RecordingSender()
    common = dict(
        sender=sender, subject="Daily Digest · May 03", html_body="<b>x</b>",
        kind="digest", recipient="me@example.com",
        store=_store(state_db),
    )
    out_a = send_logged(**common)
    out_b = send_logged(**common)
    assert out_a == "ok"
    assert out_b == "suppressed"
    # Sender only invoked once
    assert len(sender.calls) == 1
    # Audit table has both rows
    rows = _all_rows(state_db)
    assert [r["outcome"] for r in rows] == ["ok", "suppressed"]


def test_distinct_dedup_keys_dont_collide(state_db):
    sender = _RecordingSender()
    a = send_logged(
        sender=sender, subject="entry email", html_body="x",
        kind="entry_debate", recipient="me@example.com",
        store=_store(state_db),
        dedup_key="entry_stock_AAPL_placed_place_high",
    )
    b = send_logged(
        sender=sender, subject="entry email", html_body="x",
        kind="entry_debate", recipient="me@example.com",
        store=_store(state_db),
        dedup_key="entry_stock_AAPL_skipped_skip_medium",
    )
    assert a == "ok"
    assert b == "ok"
    assert len(sender.calls) == 2


def test_same_dedup_key_within_cooldown_suppressed(state_db):
    """Same (kind, dedup_key) within cooldown is suppressed even when
    the subject differs (timestamp/equity in subject doesn't matter)."""
    sender = _RecordingSender()
    a = send_logged(
        sender=sender, subject="Daily Digest · May 03 · +0.06% · $14,963",
        html_body="x", kind="digest", recipient="me@example.com",
        store=_store(state_db), dedup_key="daily_digest_2026-05-03",
    )
    b = send_logged(
        sender=sender, subject="Daily Digest · May 03 · +0.04% · $14,961",
        html_body="x", kind="digest", recipient="me@example.com",
        store=_store(state_db), dedup_key="daily_digest_2026-05-03",
    )
    assert a == "ok"
    assert b == "suppressed"
    assert len(sender.calls) == 1


def test_cooldown_zero_disables_suppression(state_db):
    sender = _RecordingSender()
    common = dict(
        sender=sender, subject="x", html_body="x",
        kind="digest", recipient="me@example.com",
        store=_store(state_db), cooldown_seconds=0,
    )
    a = send_logged(**common)
    b = send_logged(**common)
    assert a == "ok"
    assert b == "ok"
    assert len(sender.calls) == 2


def test_after_cooldown_send_resumes(state_db):
    """Once the cooldown window elapses, the next send goes through."""
    sender = _RecordingSender()
    base = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)
    a = send_logged(
        sender=sender, subject="x", html_body="x",
        kind="entry_debate", recipient="me@example.com",
        store=_store(state_db), dedup_key="aapl-place",
        now=base,
    )
    b = send_logged(
        sender=sender, subject="x", html_body="x",
        kind="entry_debate", recipient="me@example.com",
        store=_store(state_db), dedup_key="aapl-place",
        now=base + dt.timedelta(minutes=30),
    )
    c = send_logged(
        sender=sender, subject="x", html_body="x",
        kind="entry_debate", recipient="me@example.com",
        store=_store(state_db), dedup_key="aapl-place",
        now=base + dt.timedelta(minutes=70),  # past 60-min cooldown
    )
    assert (a, b, c) == ("ok", "suppressed", "ok")
    assert len(sender.calls) == 2


def test_send_failure_is_recorded_and_re_raised(state_db):
    sender = _RecordingSender()
    sender.fail_next = True
    with pytest.raises(RuntimeError):
        send_logged(
            sender=sender, subject="x", html_body="x",
            kind="digest", recipient="me@example.com",
            store=_store(state_db),
        )
    rows = _all_rows(state_db)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"


def test_suppression_check_counts_failed_as_NOT_blocking(state_db):
    """A prior failed send shouldn't suppress the next attempt — we WANT
    to retry after a transport error."""
    sender = _RecordingSender()
    common = dict(
        sender=sender, subject="x", html_body="x",
        kind="digest", recipient="me@example.com",
        store=_store(state_db),
    )
    sender.fail_next = True
    with pytest.raises(RuntimeError):
        send_logged(**common)
    # Next attempt: should NOT be suppressed since the prior was failed.
    out = send_logged(**common)
    assert out == "ok"
    assert len(sender.calls) == 1  # second call succeeded; first raised


def test_subject_hash_fallback_when_no_dedup_key(state_db):
    """Two identical subjects suppress within cooldown even without
    explicit dedup_key — subject is hashed as the lookup."""
    sender = _RecordingSender()
    common = dict(
        sender=sender, subject="repeat me", html_body="x",
        kind="digest", recipient="me@example.com",
        store=_store(state_db),
    )
    a = send_logged(**common)
    b = send_logged(**common)
    assert a == "ok"
    assert b == "suppressed"


def test_critical_kind_never_suppressed(state_db):
    """``critical`` kind has cooldown 0 — every send goes through."""
    sender = _RecordingSender()
    for _ in range(3):
        send_logged(
            sender=sender, subject="emergency", html_body="x",
            kind="critical", recipient="me@example.com",
            store=_store(state_db),
        )
    assert len(sender.calls) == 3
