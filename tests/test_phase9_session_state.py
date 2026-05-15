"""Daemon session-start equity helper — anchors intraday DD checks."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from trading_bot.daemon.jobs import ensure_account_snapshot_table
from trading_bot.daemon.session_state import (
    _current_session_anchor_utc,
    session_start_equity,
)


from zoneinfo import ZoneInfo
NY = ZoneInfo("America/New_York")


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    ensure_account_snapshot_table(c)
    yield c
    c.close()


def _insert_snapshot(conn: sqlite3.Connection, ts: dt.datetime, equity: float) -> None:
    conn.execute(
        "INSERT INTO account_snapshot "
        "(snapshot_ts, equity, cash, buying_power) VALUES (?, ?, ?, ?)",
        (ts.astimezone(dt.timezone.utc).isoformat(), equity, 0.0, 0.0),
    )
    conn.commit()


def test_falls_back_to_current_when_no_snapshot(conn) -> None:
    eq = session_start_equity(conn, fallback_equity=12_345.0)
    assert eq == 12_345.0


def test_returns_first_snapshot_in_session(conn) -> None:
    # Pin "now" at 14:00 ET; session anchor = today 09:30 ET.
    now = dt.datetime(2026, 5, 15, 14, 0, tzinfo=NY)
    snap_at_open = dt.datetime(2026, 5, 15, 9, 31, tzinfo=NY)
    snap_later = dt.datetime(2026, 5, 15, 13, 0, tzinfo=NY)
    _insert_snapshot(conn, snap_at_open, equity=100_000.0)
    _insert_snapshot(conn, snap_later, equity=98_500.0)
    eq = session_start_equity(conn, fallback_equity=999.0, now=now)
    assert eq == 100_000.0


def test_ignores_prior_session_snapshots(conn) -> None:
    """Snapshots from yesterday must NOT anchor today's session."""
    now = dt.datetime(2026, 5, 15, 10, 0, tzinfo=NY)
    yesterday_snap = dt.datetime(2026, 5, 14, 10, 0, tzinfo=NY)
    today_snap = dt.datetime(2026, 5, 15, 9, 31, tzinfo=NY)
    _insert_snapshot(conn, yesterday_snap, equity=90_000.0)
    _insert_snapshot(conn, today_snap, equity=100_000.0)
    eq = session_start_equity(conn, fallback_equity=999.0, now=now)
    assert eq == 100_000.0


def test_anchor_walks_back_to_friday_on_weekend(conn) -> None:
    """Sat / Sun → previous Friday 09:30 anchor (crypto strategies must
    still compute DD on weekends)."""
    # Saturday 2026-05-16 10:00 ET
    saturday = dt.datetime(2026, 5, 16, 10, 0, tzinfo=NY)
    anchor = _current_session_anchor_utc(saturday.astimezone(dt.timezone.utc))
    # Anchor must be Friday 2026-05-15 09:30 ET
    anchor_ny = anchor.astimezone(NY)
    assert anchor_ny.weekday() == 4    # Friday
    assert anchor_ny.date() == dt.date(2026, 5, 15)
    assert (anchor_ny.hour, anchor_ny.minute) == (9, 30)


def test_anchor_walks_back_before_today_open(conn) -> None:
    """At 08:00 ET (pre-open), anchor should be yesterday's 09:30 — we
    have not yet entered today's session."""
    pre_open = dt.datetime(2026, 5, 15, 8, 0, tzinfo=NY)
    anchor = _current_session_anchor_utc(pre_open.astimezone(dt.timezone.utc))
    anchor_ny = anchor.astimezone(NY)
    assert anchor_ny.date() == dt.date(2026, 5, 14)
