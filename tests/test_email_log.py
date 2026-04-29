import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def state_db(tmp_path):
    """Fresh state.db with the emails_sent table created."""
    db_path = tmp_path / "state.db"
    from sqlalchemy import create_engine, text
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE emails_sent ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sent_at TIMESTAMP NOT NULL, "
            "kind TEXT NOT NULL, "
            "subject TEXT NOT NULL, "
            "recipient TEXT NOT NULL, "
            "outcome TEXT NOT NULL)"
        ))
    return db_path


def test_send_logged_records_success(state_db):
    from trading_bot.email_log import send_logged, EmailLogStore

    sender = MagicMock()
    send_logged(
        sender=sender,
        subject="Test subject",
        html_body="<p>x</p>",
        kind="digest",
        recipient="x@y",
        store=EmailLogStore(state_db),
    )

    sender.send.assert_called_once_with(subject="Test subject", html_body="<p>x</p>")
    rows = EmailLogStore(state_db).since(dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc))
    assert len(rows) == 1
    assert rows[0]["kind"] == "digest"
    assert rows[0]["subject"] == "Test subject"
    assert rows[0]["recipient"] == "x@y"
    assert rows[0]["outcome"] == "ok"


def test_send_logged_records_failure(state_db):
    from trading_bot.email_log import send_logged, EmailLogStore

    sender = MagicMock()
    sender.send.side_effect = RuntimeError("smtp down")

    with pytest.raises(RuntimeError, match="smtp down"):
        send_logged(
            sender=sender, subject="s", html_body="b",
            kind="alert", recipient="x@y",
            store=EmailLogStore(state_db),
        )

    rows = EmailLogStore(state_db).since(dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failed"


def test_email_log_store_count_by_kind(state_db):
    from trading_bot.email_log import EmailLogStore

    store = EmailLogStore(state_db)
    now = dt.datetime.now(dt.timezone.utc)
    store.record(sent_at=now, kind="digest", subject="d", recipient="x", outcome="ok")
    store.record(sent_at=now, kind="alert", subject="a1", recipient="x", outcome="ok")
    store.record(sent_at=now, kind="alert", subject="a2", recipient="x", outcome="ok")
    store.record(sent_at=now, kind="alert", subject="a3", recipient="x", outcome="failed")

    counts = store.count_by_kind_since(now - dt.timedelta(hours=1))
    assert counts == {"digest": 1, "alert": 3}


def test_email_log_store_failures_only(state_db):
    from trading_bot.email_log import EmailLogStore

    store = EmailLogStore(state_db)
    now = dt.datetime.now(dt.timezone.utc)
    store.record(sent_at=now, kind="alert", subject="ok", recipient="x", outcome="ok")
    store.record(sent_at=now, kind="alert", subject="bad", recipient="x", outcome="failed")
    fails = store.failures_since(now - dt.timedelta(hours=1))
    assert len(fails) == 1
    assert fails[0]["subject"] == "bad"


def test_send_logged_emits_json_log_event(state_db, capsys):
    import json
    from trading_bot.email_log import send_logged, EmailLogStore

    sender = MagicMock()
    send_logged(
        sender=sender,
        subject="JSON event test",
        html_body="<p>body</p>",
        kind="status",
        recipient="test@example.com",
        store=EmailLogStore(state_db),
    )

    captured = capsys.readouterr()
    event = json.loads(captured.err.strip())
    assert event["event"] == "email_sent"
    assert event["kind"] == "status"
    assert event["subject"] == "JSON event test"
    assert event["recipient"] == "test@example.com"
    assert event["outcome"] == "ok"
    assert event["role"] == "email_log"
    assert event["level"] == "info"
