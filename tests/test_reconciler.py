"""Tests for src/trading_bot/reconciler.py — diffs trade_journal vs
Alpaca positions, writes closed_trades for any positions that are gone."""
import datetime as dt
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.trade_journal import TradeJournal, TradeRecord


def _journal_record(symbol="AAPL", entry_order_id="o-1",
                    timestamp=None) -> TradeRecord:
    ts = timestamp or dt.datetime(2026, 4, 27, 13, 7, tzinfo=dt.timezone.utc)
    return TradeRecord(
        timestamp=ts,
        symbol=symbol, side="buy", qty=Decimal("3"), price=Decimal("220.27"),
        asset_class="stock", strategy="momentum", regime="trending_up",
        entry_order_id=entry_order_id, stop_loss_order_id="stop-1",
        notes="entry",
    )


def _alpaca_filled_order(*, symbol="AAPL", side="sell", filled_qty="3",
                         filled_avg_price="195.00",
                         filled_at="2026-04-27T15:30:00+00:00",
                         order_id=None):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.filled_at = dt.datetime.fromisoformat(filled_at)
    o.status = "filled"
    o.id = order_id or "fill-order-1"
    o.type = "market"
    return o


def _alpaca_expired_order(*, symbol="AMD", order_id="o-amd"):
    o = MagicMock()
    o.symbol = symbol
    o.side = "buy"
    o.filled_qty = "0"
    o.filled_avg_price = None
    o.filled_at = None
    o.status = "expired"
    o.id = order_id
    o.type = "limit"
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


def test_reconciler_writes_audit_fallback_when_no_order_found(tmp_path):
    """When Alpaca returns no orders at all (retention limit / paper account),
    the reconciler writes a 'reconciled_no_fill_found' audit row so the entry
    is never re-processed.  reconciled_count=1, unmatched_count=0."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AMD", entry_order_id="o-amd"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = []  # nothing in Alpaca history

    closed_path = tmp_path / "closed.db"
    report = reconcile(
        client=alpaca, journal=journal,
        closed_trades_path=closed_path,
    )

    assert report.reconciled_count == 1
    assert report.unmatched_count == 0
    assert report.errors_count == 0

    rows = list(ClosedTradeStore(closed_path).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "AMD"
    assert row.exit_price == row.entry_price        # sentinel: no exit known
    assert row.realized_pnl == Decimal("0")         # conservative: no P&L assumed
    assert "reconciled_no_fill_found" in row.notes
    assert "o-amd" in row.notes


def test_reconciler_writes_cancelled_unfilled_when_order_expired(tmp_path):
    """When the entry order exists in Alpaca but is EXPIRED with qty=0,
    the reconciler writes a 'cancelled_unfilled' audit row.
    These are limit orders that never triggered."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AMD", entry_order_id="o-amd"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = [
        _alpaca_expired_order(symbol="AMD", order_id="o-amd"),
    ]

    closed_path = tmp_path / "closed.db"
    report = reconcile(
        client=alpaca, journal=journal,
        closed_trades_path=closed_path,
    )

    assert report.reconciled_count == 1
    assert report.unmatched_count == 0
    assert report.errors_count == 0

    rows = list(ClosedTradeStore(closed_path).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "AMD"
    assert row.exit_price == row.entry_price
    assert row.realized_pnl == Decimal("0")
    assert "cancelled_unfilled" in row.notes


def test_reconciler_audit_fallback_is_idempotent(tmp_path):
    """Running reconcile twice when no fill is found must not create duplicate rows."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AMD", entry_order_id="o-amd2"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = []

    closed_path = tmp_path / "closed.db"
    r1 = reconcile(client=alpaca, journal=journal, closed_trades_path=closed_path)
    r2 = reconcile(client=alpaca, journal=journal, closed_trades_path=closed_path)

    assert r1.reconciled_count == 1
    assert r2.reconciled_count == 0  # idempotent — already in closed_trades
    assert len(ClosedTradeStore(closed_path).all()) == 1


def test_reconciler_uses_after_lookback(tmp_path):
    """Verify reconcile passes the 'after' parameter to Alpaca's get_orders
    so old fills within lookback_days are included."""
    from trading_bot.reconciler import reconcile

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="AAPL", entry_order_id="o-lb"))

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = [
        _alpaca_filled_order(symbol="AAPL", side="sell"),
    ]

    reconcile(client=alpaca, journal=journal,
              closed_trades_path=tmp_path / "closed.db",
              lookback_days=14)

    # get_orders should have been called exactly once with a GetOrdersRequest.
    assert alpaca._client.get_orders.call_count == 1
    call_kwargs = alpaca._client.get_orders.call_args
    # Accept both positional and keyword 'filter' argument.
    req = None
    if call_kwargs.kwargs.get("filter") is not None:
        req = call_kwargs.kwargs["filter"]
    elif call_kwargs.args:
        req = call_kwargs.args[0]
    # The request object should have an 'after' attribute that is within
    # the expected window (within a few seconds of now - 14 days).
    assert req is not None
    after_val = getattr(req, "after", None)
    assert after_val is not None
    now = dt.datetime.now(dt.timezone.utc)
    expected_after = now - dt.timedelta(days=14)
    delta = abs((after_val - expected_after).total_seconds())
    assert delta < 10, f"after={after_val} too far from expected {expected_after}"
