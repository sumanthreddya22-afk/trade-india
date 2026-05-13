"""Phase 1 — strategy_decision writer + hash chain."""
from __future__ import annotations

from trading_bot.ledger import verify_chain, write_decision


def test_write_decision_accept(ledger_conn) -> None:
    seq = write_decision(
        ledger_conn,
        strategy_id="ETF_MOMENTUM_v1", strategy_ver=1,
        code_hash="a" * 64, config_hash="b" * 64, policy_hash="c" * 64,
        feature_snapshot_id="feat-2026-05-13T00:00:00Z",
        intent={"symbol": "SPY", "side": "buy", "qty": 10},
        risk_decision="accept",
        emitted_client_order_id="20260513_ETF_MOMENTUM_SPY_1",
    )
    assert seq == 1
    assert verify_chain(ledger_conn, "strategy_decision") == 1


def test_write_decision_halt_has_no_cid(ledger_conn) -> None:
    write_decision(
        ledger_conn,
        strategy_id="ETF_MOMENTUM_v1", strategy_ver=1,
        code_hash="a" * 64, config_hash="b" * 64, policy_hash="c" * 64,
        feature_snapshot_id="feat-x",
        intent={"symbol": "SPY", "side": "buy", "qty": 10},
        risk_decision="halt",
        risk_reason="account_cap_breached",
        emitted_client_order_id=None,
    )
    cur = ledger_conn.cursor()
    cur.execute("SELECT risk_decision, emitted_client_order_id "
                "FROM strategy_decision WHERE ledger_seq = 1")
    risk_decision, cid = cur.fetchone()
    assert risk_decision == "halt"
    assert cid is None
