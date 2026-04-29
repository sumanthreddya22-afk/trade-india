"""Tests for src/trading_bot/reconciler.py — diffs trade_journal vs
Alpaca positions, writes closed_trades for any positions that are gone."""
import datetime as dt
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.trade_journal import TradeJournal, TradeRecord


def _journal_record(symbol="AAPL", entry_order_id="o-1") -> TradeRecord:
    return TradeRecord(
        timestamp=dt.datetime(2026, 4, 27, 13, 7, tzinfo=dt.timezone.utc),
        symbol=symbol, side="buy", qty=Decimal("3"), price=Decimal("220.27"),
        asset_class="stock", strategy="momentum", regime="trending_up",
        entry_order_id=entry_order_id, stop_loss_order_id="stop-1",
        notes="entry",
    )


def _alpaca_filled_order(*, symbol="AAPL", side="sell", filled_qty="3",
                         filled_avg_price="195.00",
                         filled_at="2026-04-27T15:30:00+00:00"):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.filled_at = dt.datetime.fromisoformat(filled_at)
    o.status = "filled"
    return o


def test_reconciler_writes_closed_trade_when_position_disappears(tmp_path):
    from trading_bot.reconciler import reconcile, ReconcileReport

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AAPL", entry_order_id="o-aapl"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []  # AAPL is gone
    alpaca._client.get_orders.return_value = [
        _alpaca_filled_order(symbol="AAPL", side="sell"),
    ]

    closed_path = tmp_path / "closed.db"
    report: ReconcileReport = reconcile(
        client=alpaca, journal=journal,
        closed_trades_path=closed_path,
    )

    assert report.reconciled_count == 1
    assert report.unmatched_count == 0
    assert report.errors_count == 0

    from trading_bot.reconciliation import ClosedTradeStore
    rows = list(ClosedTradeStore(closed_path).all())
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].entry_price == Decimal("220.27")
    assert rows[0].exit_price == Decimal("195.00")
    assert rows[0].realized_pnl == Decimal("-75.81")  # 3 * (195 - 220.27)


def test_reconciler_skips_already_reconciled(tmp_path):
    """If closed_trades already has the entry_order_id, skip it."""
    from trading_bot.reconciler import reconcile

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(entry_order_id="o-dup"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = [_alpaca_filled_order()]

    closed_path = tmp_path / "closed.db"
    r1 = reconcile(client=alpaca, journal=journal, closed_trades_path=closed_path)
    r2 = reconcile(client=alpaca, journal=journal, closed_trades_path=closed_path)

    assert r1.reconciled_count == 1
    assert r2.reconciled_count == 0  # idempotent


def test_reconciler_skips_open_positions(tmp_path):
    """If a journal entry's symbol is still in Alpaca positions, leave it alone."""
    from trading_bot.reconciler import reconcile

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AAPL"))

    alpaca = MagicMock()
    pos = MagicMock(); pos.symbol = "AAPL"
    alpaca.get_positions.return_value = [pos]

    report = reconcile(
        client=alpaca, journal=journal,
        closed_trades_path=tmp_path / "closed.db",
    )
    assert report.reconciled_count == 0
    assert report.unmatched_count == 0


def test_reconciler_marks_unmatched_when_no_closing_fill(tmp_path):
    """Journal has an entry, position is gone, but Alpaca order history doesn't
    show the closing fill (Alpaca retention limit). Record as unmatched."""
    from trading_bot.reconciler import reconcile

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AMD", entry_order_id="o-amd"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = []  # no orders found

    report = reconcile(
        client=alpaca, journal=journal,
        closed_trades_path=tmp_path / "closed.db",
    )
    assert report.reconciled_count == 0
    assert report.unmatched_count == 1
