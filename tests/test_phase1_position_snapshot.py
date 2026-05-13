"""Phase 1 — position_snapshot writer."""
from __future__ import annotations

from trading_bot.ledger import (
    verify_chain, write_snapshot, write_snapshot_batch,
)


def test_single_snapshot(ledger_conn) -> None:
    seq = write_snapshot(
        ledger_conn,
        source="bot", symbol="SPY", asset_class="equity",
        qty=10.0, classification="bot",
        avg_cost=400.0, market_price=405.0, market_value=4050.0,
        strategy_id="ETF_MOMENTUM_v1",
    )
    assert seq == 1
    assert verify_chain(ledger_conn, "position_snapshot") == 1


def test_batch_snapshot_shares_timestamp(ledger_conn) -> None:
    positions = [
        {"symbol": "SPY", "asset_class": "equity", "qty": 10.0,
         "classification": "bot"},
        {"symbol": "QQQ", "asset_class": "equity", "qty": 5.0,
         "classification": "bot"},
        {"symbol": "GLD", "asset_class": "equity", "qty": 8.0,
         "classification": "external"},
    ]
    seqs = write_snapshot_batch(ledger_conn, positions, source="bot")
    assert len(seqs) == 3
    # All three rows must carry the identical snapshot_ts.
    cur = ledger_conn.cursor()
    cur.execute("SELECT DISTINCT snapshot_ts FROM position_snapshot")
    distinct_ts = cur.fetchall()
    assert len(distinct_ts) == 1
    assert verify_chain(ledger_conn, "position_snapshot") == 3
