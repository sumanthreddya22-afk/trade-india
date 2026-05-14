"""bot digest — last-24h summary."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from trading_bot.operator.digest import build_digest, format_digest_text


@pytest.fixture()
def ledger(tmp_path) -> Path:
    p = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_empty_digest(ledger):
    d = build_digest(hours=24, ledger_db=ledger)
    assert d["ledger_present"]
    # No account snapshots → n_snapshots == 0 (not an error).
    assert d["account"]["n_snapshots"] == 0
    assert d["kill_switches"] == []
    assert d["orders"] == []
    assert d["fills"] == []


def test_digest_picks_up_account_rows(ledger):
    from trading_bot.daemon.jobs import ensure_account_snapshot_table
    conn = sqlite3.connect(str(ledger))
    try:
        ensure_account_snapshot_table(conn)
        # Two rows: opening + later
        now = dt.datetime.now(dt.timezone.utc)
        for i, eq in enumerate((100000.0, 100250.0)):
            conn.execute(
                "INSERT INTO account_snapshot (snapshot_ts, equity, cash, buying_power) "
                "VALUES (?,?,?,?)",
                ((now + dt.timedelta(minutes=i)).isoformat(), eq, 50000, 200000),
            )
        conn.commit()
    finally:
        conn.close()
    d = build_digest(hours=24, ledger_db=ledger)
    assert d["account"]["n_snapshots"] == 2
    assert d["account"]["intraday_pnl"] == 250.0


def test_format_digest_text_works(ledger):
    d = build_digest(hours=24, ledger_db=ledger)
    text = format_digest_text(d)
    assert "trading-bot v4 digest" in text
    assert "ACCOUNT" in text
    assert "HEARTBEATS" in text
