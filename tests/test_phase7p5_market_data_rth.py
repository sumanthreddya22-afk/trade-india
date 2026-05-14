"""market_data_ingest RTH gate."""
from __future__ import annotations

import datetime as dt
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
        universe=("SPY",),
    )


def test_ingest_skips_outside_rth(ctx, monkeypatch):
    """When the RTH helper says market is closed, ingest is a no-op."""
    called = []
    def fake_bars(**_):
        called.append(True)
        return {}
    ctx.bars_fetcher = fake_bars
    monkeypatch.setattr(
        "trading_bot.daemon.jobs.is_equity_rth", lambda: False
    )
    jobs.job_market_data_ingest(ctx)
    assert called == []   # never reached the fetcher
    import sqlite3
    conn = sqlite3.connect(str(ctx.ledger_db))
    try:
        cur = conn.execute(
            "SELECT last_detail FROM daemon_heartbeat WHERE job_name=?",
            ("market_data_ingest",),
        )
        detail = cur.fetchone()[0]
    finally:
        conn.close()
    assert "RTH gate" in detail


def test_ingest_runs_during_rth(ctx, monkeypatch):
    def fake_bars(symbols=None, **_):
        now = dt.datetime.now(dt.timezone.utc)
        return {s: {"ts": now, "open": 1, "high": 2, "low": 0.5,
                     "close": 1.5, "volume": 100} for s in (symbols or ())}
    ctx.bars_fetcher = fake_bars
    monkeypatch.setattr(
        "trading_bot.daemon.jobs.is_equity_rth", lambda: True
    )
    jobs.job_market_data_ingest(ctx)
    import sqlite3
    conn = sqlite3.connect(str(ctx.ledger_db))
    try:
        cur = conn.execute(
            "SELECT last_detail FROM daemon_heartbeat WHERE job_name=?",
            ("market_data_ingest",),
        )
        detail = cur.fetchone()[0]
    finally:
        conn.close()
    assert "1 symbols watermarked" in detail
