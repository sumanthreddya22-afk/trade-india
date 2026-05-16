"""WS6a — broker_switch_event ledger writes + recon kill-switch suppression."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from trading_bot.ledger.broker_switch_event import (
    most_recent_switch_within, write_event,
)
from trading_bot.ledger.schema import create_ledger
from trading_bot.risk.kill_switches import detect_recon_mismatch


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_ledger(conn)
    return conn


def test_write_event_increments_ledger_seq() -> None:
    conn = _conn()
    s1 = write_event(
        conn, from_broker="alpaca", to_broker="webull",
        operator="bharath@local", reason="2026-06-12 cutover",
    )
    s2 = write_event(
        conn, from_broker="webull", to_broker="alpaca",
        operator="bharath@local", reason="rollback",
    )
    assert s2 > s1
    rows = conn.execute(
        "SELECT from_broker, to_broker, prev_hash, this_hash "
        "FROM broker_switch_event ORDER BY ledger_seq"
    ).fetchall()
    assert len(rows) == 2
    # Hash chain: row2.prev_hash == row1.this_hash.
    assert rows[1][2] == rows[0][3]
    assert rows[0][3] != rows[1][3]


def test_write_event_required_fields() -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        write_event(conn, from_broker="", to_broker="webull", operator="op")
    with pytest.raises(ValueError):
        write_event(conn, from_broker="alpaca", to_broker="webull", operator="")


def test_append_only_triggers_block_update_and_delete() -> None:
    conn = _conn()
    write_event(
        conn, from_broker="alpaca", to_broker="webull",
        operator="op",
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE broker_switch_event SET operator='evil'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM broker_switch_event")


def test_most_recent_switch_within_window() -> None:
    conn = _conn()
    now = dt.datetime(2026, 6, 13, 1, 0, 0, tzinfo=dt.timezone.utc)
    # Switch 1h ago — within 24h window.
    write_event(
        conn, from_broker="alpaca", to_broker="webull",
        operator="op", now=now - dt.timedelta(hours=1),
    )
    hit = most_recent_switch_within(
        conn, window=dt.timedelta(hours=24), now=now,
    )
    assert hit is not None
    assert hit["from_broker"] == "alpaca"
    # Same query 26h later — outside window.
    miss = most_recent_switch_within(
        conn, window=dt.timedelta(hours=24),
        now=now + dt.timedelta(hours=26),
    )
    assert miss is None


def test_recon_kill_switch_suppressed_after_recent_switch() -> None:
    # Without a recent switch, match=0 fires the kill.
    k = detect_recon_mismatch(latest_match=0, latest_window="2026-06-13")
    assert k is not None
    assert k.detector == "recon_mismatch"

    # With a recent switch, the kill is suppressed.
    suppressed = detect_recon_mismatch(
        latest_match=0, latest_window="2026-06-13",
        recent_broker_switch={"event_ts": "2026-06-12T20:30:00Z"},
    )
    assert suppressed is None

    # match=1 still returns None regardless.
    assert detect_recon_mismatch(
        latest_match=1, latest_window="2026-06-13",
    ) is None
