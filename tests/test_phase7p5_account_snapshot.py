"""Account-snapshot job + status_snapshot derivation."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from trading_bot.daemon import jobs
from trading_bot.daemon.jobs import DaemonContext


@pytest.fixture()
def ctx(tmp_path) -> DaemonContext:
    ledger = tmp_path / "ledger.db"
    mirror = tmp_path / "mirror.db"
    from trading_bot.ledger import connect_writer, create_ledger, init_mirror
    conn = connect_writer(ledger)
    create_ledger(conn)
    conn.close()
    init_mirror(mirror)
    return DaemonContext(
        ledger_db=ledger, mirror_db=mirror,
        policy_dir=Path(__file__).resolve().parent.parent / "policy",
    )


def test_skipped_without_fetcher(ctx):
    jobs.job_account_snapshot(ctx)
    # Without a fetcher, the table is never created (we short-circuit
    # before touching the writer). Heartbeat should record the skip.
    conn = sqlite3.connect(str(ctx.ledger_db))
    try:
        cur = conn.execute(
            "SELECT last_detail FROM daemon_heartbeat WHERE job_name=?",
            ("account_snapshot",),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "skipped" in row[0]


def test_writes_account_row_with_fetcher(ctx):
    def fake_account():
        return {
            "equity": 100000.0, "cash": 50000.0, "buying_power": 200000.0,
            "daytrade_count": 0, "pattern_day_trader": False, "status": "ACTIVE",
        }
    ctx.account_fetcher = fake_account
    jobs.job_account_snapshot(ctx)
    conn = sqlite3.connect(str(ctx.ledger_db))
    try:
        cur = conn.execute(
            "SELECT equity, cash, buying_power, broker_status FROM account_snapshot"
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row == (100000.0, 50000.0, 200000.0, "ACTIVE")


def test_status_snapshot_includes_account_after_tick(ctx):
    def fake_account():
        return {"equity": 50000.0, "cash": 10000.0, "buying_power": 60000.0}
    ctx.account_fetcher = fake_account
    jobs.job_account_snapshot(ctx)
    from trading_bot.operator import controls
    snap = controls.status_snapshot(ledger_db=ctx.ledger_db)
    assert snap["account"]["equity"] == 50000.0
