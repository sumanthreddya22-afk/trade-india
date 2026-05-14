"""Strategy dispatch: enabled-strategies query + cadence gating."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from trading_bot.daemon import strategy_dispatch


@pytest.fixture()
def ledger(tmp_path):
    p = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def _register(ledger: Path, sid: str, ver: int, status: str) -> None:
    from trading_bot.ledger import connect_writer
    from trading_bot.registry import register_version
    conn = connect_writer(ledger)
    try:
        # Non-research_only statuses require a validation_artifact_id.
        artifact_id = None if status == "research_only" else f"{sid}.artifact"
        register_version(
            conn, strategy_id=sid, strategy_ver=ver,
            code_hash=f"{sid}.code", config_hash=f"{sid}.cfg",
            thesis_id=f"{sid}.thesis", hypothesis_id=f"{sid}.h1",
            validation_artifact_id=artifact_id, lane="etf_momentum",
            status=status, expiry_date=None, owner="tester",
        )
        conn.commit()
    finally:
        conn.close()


def test_enabled_strategies_filters_by_status(ledger):
    _register(ledger, "A_ONLY", 1, "research_only")
    _register(ledger, "B_SHADOW", 1, "shadow")
    _register(ledger, "C_LIVE", 1, "tiny_paper")
    conn = sqlite3.connect(str(ledger))
    try:
        out = strategy_dispatch._enabled_strategies(conn)
    finally:
        conn.close()
    sids = [s for s, _ in out]
    assert "A_ONLY" not in sids
    assert "B_SHADOW" not in sids
    assert "C_LIVE" in sids


def test_dispatch_skips_when_no_module_mapped(ledger, tmp_path):
    _register(ledger, "UNKNOWN_X", 1, "tiny_paper")

    class _Ctx:
        ledger_db = ledger
        positions_fetcher = lambda self: []
        account_fetcher = lambda self: {"equity": 1, "cash": 1, "buying_power": 1}
        broker_submit = lambda self, **kw: {"ok": False, "broker_order_id": None}

    summary = strategy_dispatch.dispatch_all_strategies(_Ctx())
    assert summary["n_enabled"] == 1
    assert summary["details"][0]["status"] == "no_module"
