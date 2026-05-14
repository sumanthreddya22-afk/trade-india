"""Halt / resume via operator controls — writes kill_switch_event rows."""
from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.operator import controls
from trading_bot.risk import active_kills


def test_halt_then_resume_round_trip(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    # init_ledger creates the schema; we lean on connect_writer to make
    # the file and ensure_kill_switch_table runs via halt().
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(ledger)
    create_ledger(conn)
    conn.close()

    # Halt
    out = controls.halt(reason="unit-test", operator="tester", ledger_db=ledger)
    assert out["ok"]
    assert controls.OPERATOR_HALT in out["active"]

    # Resume
    out = controls.resume(reason="done", operator="tester", ledger_db=ledger)
    assert out["ok"]
    assert controls.OPERATOR_HALT not in out["active"]


def test_status_snapshot_reflects_halt(tmp_path):
    ledger = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(ledger)
    create_ledger(conn)
    conn.close()

    snap = controls.status_snapshot(ledger_db=ledger)
    assert snap["halted"] is False

    controls.halt(reason="x", operator="tester", ledger_db=ledger)
    snap = controls.status_snapshot(ledger_db=ledger)
    assert snap["halted"] is True
    assert controls.OPERATOR_HALT in snap["active_kills"]
