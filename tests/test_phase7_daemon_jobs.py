"""Daemon job tests — heartbeat writing, error capture, skip semantics."""
from __future__ import annotations

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


def _read_heartbeat(ledger: Path, job: str) -> dict | None:
    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT job_name, last_status, last_detail FROM daemon_heartbeat WHERE job_name=?",
            (job,),
        )
        row = cur.fetchone()
        return None if row is None else {"job_name": row[0], "status": row[1], "detail": row[2]}
    finally:
        conn.close()


def test_position_snapshot_skipped_without_fetcher(ctx):
    jobs.job_position_snapshot(ctx)
    hb = _read_heartbeat(ctx.ledger_db, "position_snapshot")
    assert hb is not None
    assert hb["status"] == "ok"   # skip is wrapped as a successful "ok" with detail noting skip
    assert "skipped" in hb["detail"]


def test_position_snapshot_writes_with_fetcher(ctx):
    def fake_fetch():
        return [
            {"symbol": "SPY", "qty": 10, "avg_entry_price": 400.0,
             "market_price": 410.0, "market_value": 4100.0,
             "asset_class": "us_equity", "classification": "bot"}
        ]
    ctx.positions_fetcher = fake_fetch
    jobs.job_position_snapshot(ctx)
    hb = _read_heartbeat(ctx.ledger_db, "position_snapshot")
    assert hb["status"] == "ok"
    assert "1 positions" in hb["detail"]


def test_market_data_ingest_skipped_when_universe_empty(ctx):
    ctx.bars_fetcher = lambda **_: {}
    jobs.job_market_data_ingest(ctx)
    hb = _read_heartbeat(ctx.ledger_db, "market_data_ingest")
    assert "skipped" in hb["detail"]


def test_market_data_ingest_writes_watermark(ctx, monkeypatch):
    import datetime as dt
    # Force RTH so the gate doesn't short-circuit (Phase 7.5 behaviour).
    monkeypatch.setattr("trading_bot.daemon.jobs.is_equity_rth", lambda: True)
    ctx.universe = ("SPY", "QQQ")
    def fake_bars(**kw):
        now = dt.datetime.now(dt.timezone.utc)
        return {
            "SPY": {"ts": now, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
            "QQQ": {"ts": now, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
        }
    ctx.bars_fetcher = fake_bars
    jobs.job_market_data_ingest(ctx)
    hb = _read_heartbeat(ctx.ledger_db, "market_data_ingest")
    assert hb["status"] == "ok"
    assert "2 symbols" in hb["detail"]


def test_job_error_recorded_to_heartbeat(ctx, monkeypatch):
    monkeypatch.setattr("trading_bot.daemon.jobs.is_equity_rth", lambda: True)
    def boom(**_kw):
        raise RuntimeError("kaboom")
    ctx.bars_fetcher = boom
    ctx.universe = ("SPY",)
    jobs.job_market_data_ingest(ctx)
    hb = _read_heartbeat(ctx.ledger_db, "market_data_ingest")
    assert hb["status"] == "error"
    assert "kaboom" in hb["detail"]
