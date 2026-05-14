"""Daemon-side drift_monitor wiring — exercises the strategy_decision
intent_price lookup path without hitting Alpaca."""
from __future__ import annotations

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


def test_drift_monitor_runs_without_fills(ctx):
    """With zero fills, the job should report n_trades=0 for both lanes."""
    jobs.job_drift_monitor(ctx)
    import sqlite3
    conn = sqlite3.connect(str(ctx.ledger_db))
    try:
        cur = conn.execute(
            "SELECT last_detail FROM daemon_heartbeat WHERE job_name=?",
            ("drift_monitor",),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "equity:n=0" in row[0]
    assert "crypto:n=0" in row[0]


def test_drift_monitor_skipped_when_cost_model_missing(tmp_path):
    """Daemon must report a clean 'skipped' rather than crashing if
    cost_model.lock is missing from the policy dir."""
    ledger = tmp_path / "ledger.db"
    mirror = tmp_path / "mirror.db"
    from trading_bot.ledger import connect_writer, create_ledger, init_mirror
    conn = connect_writer(ledger)
    create_ledger(conn)
    conn.close()
    init_mirror(mirror)
    # Point at a tmp policy dir with no cost_model.lock.
    empty_policy = tmp_path / "policy_empty"
    empty_policy.mkdir()
    ctx = DaemonContext(
        ledger_db=ledger, mirror_db=mirror, policy_dir=empty_policy,
    )
    jobs.job_drift_monitor(ctx)
    import sqlite3
    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT last_status, last_detail FROM daemon_heartbeat WHERE job_name=?",
            ("drift_monitor",),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row[0] == "ok"
    assert "cost_model.lock missing" in row[1]
