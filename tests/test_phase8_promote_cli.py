"""Operator promote: gating + new-version write."""
from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.operator import controls


@pytest.fixture()
def ledger(tmp_path):
    p = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_promote_blocked_without_artifact(ledger):
    """If no Tier-1 artifact exists, promotion to shadow is denied."""
    from trading_bot.ledger import connect_writer
    from trading_bot.registry import register_version
    conn = connect_writer(ledger)
    try:
        register_version(
            conn, strategy_id="X", strategy_ver=1,
            code_hash="x", config_hash="x",
            thesis_id="x", hypothesis_id="x",
            validation_artifact_id=None, lane="etf_momentum",
            status="research_only", expiry_date=None, owner="tester",
        )
        conn.commit()
    finally:
        conn.close()

    out = controls.strategy_promote(
        strategy_id="X", target_status="shadow", operator="tester",
        ledger_db=ledger,
    )
    assert not out["ok"]
    assert "no passing" in out["reason"].lower() or "research_candidate" in out["reason"].lower()


def test_promote_live_requires_packet(ledger):
    """live target requires a packet; we just verify the gate signals that."""
    from trading_bot.ledger import connect_writer
    from trading_bot.registry import register_version
    conn = connect_writer(ledger)
    try:
        register_version(
            conn, strategy_id="Y", strategy_ver=1,
            code_hash="y", config_hash="y",
            thesis_id="y", hypothesis_id="y",
            validation_artifact_id=None, lane="etf_momentum",
            status="research_only", expiry_date=None, owner="tester",
        )
        conn.commit()
    finally:
        conn.close()

    out = controls.strategy_promote(
        strategy_id="Y", target_status="live", operator="tester",
        ledger_db=ledger,
    )
    assert not out["ok"]
