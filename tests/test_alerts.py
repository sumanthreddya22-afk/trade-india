import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def state_db(tmp_path):
    db = tmp_path / "state.db"
    from sqlalchemy import create_engine, text
    e = create_engine(f"sqlite:///{db}", future=True)
    with e.begin() as c:
        c.execute(text(
            "CREATE TABLE alerts_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "queued_at TIMESTAMP NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, "
            "title TEXT NOT NULL, detail_html TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE)"
        ))
        c.execute(text(
            "CREATE TABLE alerts_sent (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sent_at TIMESTAMP NOT NULL, subject TEXT NOT NULL, event_count INTEGER NOT NULL, "
            "max_severity TEXT NOT NULL)"
        ))
        c.execute(text("CREATE TABLE bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"))
    return db


def _mock_send(record_to: list):
    def _send(*, subject, html_body):
        record_to.append({"subject": subject, "body": html_body})
    return _send


def test_first_alert_sends_immediately(state_db):
    from trading_bot.alerts import AlertEvent, AlertStore, drain_alerts, queue_alert
    sent = []
    store = AlertStore(state_db)

    queue_alert(
        AlertEvent(kind="fill", severity="info",
                   title="Fill: BUY AAPL 10 @ $200.00",
                   detail_html="<p>filled</p>",
                   fired_at=dt.datetime(2026, 4, 28, 13, 0, tzinfo=dt.timezone.utc),
                   dedup_key="fill:AAPL:o-1"),
        store=store,
        sender_send=_mock_send(sent),
        now=dt.datetime(2026, 4, 28, 13, 0, tzinfo=dt.timezone.utc),
    )

    # Quiet window — first alert sent immediately
    assert len(sent) == 1
    assert "Fill: BUY AAPL" in sent[0]["subject"]


def test_burst_within_20min_batches(state_db):
    from trading_bot.alerts import AlertEvent, AlertStore, queue_alert
    sent = []
    store = AlertStore(state_db)
    base = dt.datetime(2026, 4, 28, 13, 0, tzinfo=dt.timezone.utc)

    # First — sends immediately
    queue_alert(AlertEvent(kind="fill", severity="info", title="Fill A",
                           detail_html="<p>a</p>", fired_at=base,
                           dedup_key="a"),
                store=store, sender_send=_mock_send(sent), now=base)

    # Second within 20 min — queued, not sent
    later = base + dt.timedelta(minutes=5)
    queue_alert(AlertEvent(kind="fill", severity="info", title="Fill B",
                           detail_html="<p>b</p>", fired_at=later,
                           dedup_key="b"),
                store=store, sender_send=_mock_send(sent), now=later)

    assert len(sent) == 1  # second is queued

    # Drain after 20 min
    much_later = base + dt.timedelta(minutes=25)
    from trading_bot.alerts import drain_alerts
    drain_alerts(store=store, sender_send=_mock_send(sent), now=much_later)

    assert len(sent) == 2
    assert "alert" in sent[1]["subject"].lower() or "Fill B" in sent[1]["subject"]


def test_dedup_key_prevents_double_queue(state_db):
    from trading_bot.alerts import AlertEvent, AlertStore, queue_alert
    sent = []
    store = AlertStore(state_db)
    base = dt.datetime(2026, 4, 28, 13, 0, tzinfo=dt.timezone.utc)

    e = AlertEvent(kind="fill", severity="info", title="x",
                   detail_html="<p>x</p>", fired_at=base, dedup_key="dup")

    queue_alert(e, store=store, sender_send=_mock_send(sent), now=base)
    queue_alert(e, store=store, sender_send=_mock_send(sent), now=base + dt.timedelta(minutes=2))

    assert len(sent) == 1  # second was deduped, never queued


def test_drain_with_empty_queue_no_email(state_db):
    from trading_bot.alerts import AlertStore, drain_alerts
    sent = []
    drain_alerts(store=AlertStore(state_db), sender_send=_mock_send(sent),
                 now=dt.datetime(2026, 4, 28, 13, 0, tzinfo=dt.timezone.utc))
    assert sent == []


def test_critical_severity_bypasses_throttle(state_db):
    from trading_bot.alerts import AlertEvent, AlertStore, queue_alert
    sent = []
    store = AlertStore(state_db)
    base = dt.datetime(2026, 4, 28, 13, 0, tzinfo=dt.timezone.utc)

    # Set last_sent to "just now" so a normal alert would be throttled.
    store.set_last_sent(base)
    queue_alert(
        AlertEvent(kind="daemon_critical", severity="bad",
                   title="DAEMON DOWN 8m",
                   detail_html="<p>down</p>", fired_at=base,
                   dedup_key="critical"),
        store=store, sender_send=_mock_send(sent),
        now=base + dt.timedelta(minutes=1),  # only 1 min later
    )
    # Severity=bad bypasses throttle → sent immediately
    assert len(sent) == 1
