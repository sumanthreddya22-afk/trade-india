"""Phase 1 — reconciliation_proof: bot vs broker position hash."""
from __future__ import annotations

from trading_bot.ledger import (
    compute_recon, hash_position_vector, verify_chain, write_recon_proof,
)


SPY = {"symbol": "SPY", "asset_class": "equity", "qty": 10}
QQQ = {"symbol": "QQQ", "asset_class": "equity", "qty": 5}
GLD = {"symbol": "GLD", "asset_class": "equity", "qty": 8}


def test_identical_vectors_match() -> None:
    h1 = hash_position_vector([SPY, QQQ])
    h2 = hash_position_vector([QQQ, SPY])  # order-insensitive
    assert h1 == h2


def test_qty_drift_breaks_hash() -> None:
    h1 = hash_position_vector([SPY])
    h2 = hash_position_vector([{**SPY, "qty": 11}])
    assert h1 != h2


def test_compute_recon_match() -> None:
    bot_hash, broker_hash, match, diff = compute_recon(
        bot_positions=[SPY, QQQ], broker_positions=[QQQ, SPY],
    )
    assert match
    assert diff is None
    assert bot_hash == broker_hash


def test_compute_recon_mismatch_qty() -> None:
    bot_hash, broker_hash, match, diff = compute_recon(
        bot_positions=[SPY, QQQ],
        broker_positions=[{**SPY, "qty": 11}, QQQ],
    )
    assert not match
    assert bot_hash != broker_hash
    assert diff is not None
    assert any(m["symbol"] == "SPY" for m in diff["qty_mismatches"])


def test_compute_recon_mismatch_only_in_broker() -> None:
    _, _, match, diff = compute_recon(
        bot_positions=[SPY],
        broker_positions=[SPY, GLD],
    )
    assert not match
    assert any(p["symbol"] == "GLD" for p in diff["only_in_broker"])


def test_write_recon_proof_match(ledger_conn) -> None:
    bot_hash, broker_hash, match, diff = compute_recon(
        bot_positions=[SPY], broker_positions=[SPY],
    )
    write_recon_proof(
        ledger_conn,
        recon_window="eod",
        bot_hash=bot_hash, broker_hash=broker_hash,
        match=match, diff_json=diff,
        action_taken="none",
    )
    assert verify_chain(ledger_conn, "reconciliation_proof") == 1


def test_write_recon_proof_mismatch_records_diff(ledger_conn) -> None:
    bot_hash, broker_hash, match, diff = compute_recon(
        bot_positions=[SPY], broker_positions=[SPY, GLD],
    )
    write_recon_proof(
        ledger_conn,
        recon_window="intraday",
        bot_hash=bot_hash, broker_hash=broker_hash,
        match=match, diff_json=diff,
        action_taken="halt_new",
    )
    cur = ledger_conn.cursor()
    cur.execute("SELECT match, diff_json, action_taken "
                "FROM reconciliation_proof WHERE ledger_seq=1")
    m, dj, action = cur.fetchone()
    assert m == 0
    assert "GLD" in dj
    assert action == "halt_new"
