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

    # The reconciler issues two get_orders calls: one for CLOSED orders with
    # an 'after' window, one for OPEN orders to detect pending entries.
    # Verify at least one CLOSED call carries the lookback_days 'after'.
    assert alpaca._client.get_orders.call_count >= 1
    closed_calls_with_after = []
    for call in alpaca._client.get_orders.call_args_list:
        req = call.kwargs.get("filter") or (call.args[0] if call.args else None)
        if req is not None and getattr(req, "after", None) is not None:
            closed_calls_with_after.append(req)
    assert closed_calls_with_after, "no get_orders call carried 'after'"
    after_val = closed_calls_with_after[0].after
    now = dt.datetime.now(dt.timezone.utc)
    expected_after = now - dt.timedelta(days=14)
    delta = abs((after_val - expected_after).total_seconds())
    assert delta < 10, f"after={after_val} too far from expected {expected_after}"


def _alpaca_buy_filled(*, symbol, qty, price, filled_at, order_id):
    o = MagicMock()
    o.symbol = symbol
    o.side = "buy"
    o.filled_qty = str(qty)
    o.filled_avg_price = str(price)
    o.filled_at = dt.datetime.fromisoformat(filled_at)
    o.status = "filled"
    o.id = order_id
    o.type = "market"
    return o


def _alpaca_sell_filled(*, symbol, qty, price, filled_at, order_id, type_="market"):
    o = MagicMock()
    o.symbol = symbol
    o.side = "sell"
    o.filled_qty = str(qty)
    o.filled_avg_price = str(price)
    o.filled_at = dt.datetime.fromisoformat(filled_at)
    o.status = "filled"
    o.id = order_id
    o.type = type_
    return o


def _alpaca_pending_order(*, symbol, side="buy", order_id, qty="3"):
    """A buy order that's been accepted by Alpaca but hasn't filled yet —
    the case that produced spurious $0 audit rows pre-fix."""
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.filled_qty = "0"
    o.filled_avg_price = None
    o.filled_at = None
    o.status = "accepted"
    o.id = order_id
    o.qty = qty
    o.type = "market"
    return o


def test_reconciler_captures_round_trip_without_journal_entry(tmp_path):
    """Bug B regression: bot placed a FIL/USD buy and sell on Alpaca but the
    journal has no record of the buy (legacy path / pre-journal-wiring).
    The reconciler must walk Alpaca history and write the round-trip with
    realized P&L anyway."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")  # empty journal

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = [
        _alpaca_buy_filled(symbol="FIL/USD", qty="673",
                           price="0.9151",
                           filled_at="2026-04-28T12:35:34+00:00",
                           order_id="buy-fil-1"),
        _alpaca_sell_filled(symbol="FIL/USD", qty="671.32",
                            price="0.8996",
                            filled_at="2026-04-29T09:47:55+00:00",
                            order_id="sell-fil-1"),
    ]

    closed_path = tmp_path / "closed.db"
    report = reconcile(client=alpaca, journal=journal,
                       closed_trades_path=closed_path)

    rows = list(ClosedTradeStore(closed_path).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "FIL/USD"
    assert row.entry_price == Decimal("0.9151")
    assert row.exit_price == Decimal("0.8996")
    assert row.qty == Decimal("671.32")
    # Realized PnL = 671.32 * (0.8996 - 0.9151) ≈ -10.4054... (matched qty)
    assert row.realized_pnl == (Decimal("0.8996") - Decimal("0.9151")) * Decimal("671.32")
    assert row.entry_order_id == "buy-fil-1"
    assert row.strategy == "external"  # no journal record → fallback
    assert report.reconciled_count == 1


def test_reconciler_defers_pending_entry_orders(tmp_path):
    """Bug A regression: a journal entry whose Alpaca order is still pending
    (not yet filled) must NOT get a $0 'reconciled_no_fill_found' audit row.
    Pre-fix, the reconciler wrote a $0 row that then blocked the real
    round-trip from being recorded once the order eventually filled.

    Once the order does fill (next reconciler run), the row should appear
    only when the position closes — not before.
    """
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="ARM", entry_order_id="o-arm-pending"))

    # First call — order is pending, no positions yet.
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []

    pending = _alpaca_pending_order(symbol="ARM", order_id="o-arm-pending")
    def _get_orders(filter=None):
        # Mimic Alpaca: pending order shows up in OPEN, not in CLOSED.
        from alpaca.trading.enums import QueryOrderStatus
        if getattr(filter, "status", None) == QueryOrderStatus.OPEN:
            return [pending]
        return []  # CLOSED
    alpaca._client.get_orders.side_effect = _get_orders

    closed_path = tmp_path / "closed.db"
    report = reconcile(client=alpaca, journal=journal,
                       closed_trades_path=closed_path)

    # No row written — the entry is deferred.
    assert list(ClosedTradeStore(closed_path).all()) == []
    assert report.reconciled_count == 0
    assert any(d.get("outcome") == "deferred_pending" for d in report.detail)


def test_reconciler_self_heals_stale_audit_row_when_order_now_filled(tmp_path):
    """If a previous run wrote a 'reconciled_no_fill_found' audit row for an
    entry that had not yet filled, the next run — once Alpaca shows the
    order as FILLED — must DELETE the stale audit row so the round-trip
    pass can write the real outcome (or leave the symbol open)."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    journal.append(_journal_record(symbol="ARM", entry_order_id="o-arm"))

    # Seed a stale audit row from a prior reconciler run.
    closed_path = tmp_path / "closed.db"
    store = ClosedTradeStore(closed_path)
    now = dt.datetime.now(dt.timezone.utc)
    store.append(ClosedTrade(
        symbol="ARM", side="buy", qty=Decimal("3"),
        entry_price=Decimal("201.69"), exit_price=Decimal("201.69"),
        realized_pnl=Decimal("0"), pnl_pct=0.0,
        strategy="momentum", regime="trending_up",
        entry_time=now, exit_time=now,
        hold_hours=0.0, entry_order_id="o-arm",
        notes="reconciled_no_fill_found: entry_order_id=o-arm",
    ))
    assert len(store.all()) == 1

    # Now Alpaca shows the order as FILLED and the position is open.
    alpaca = MagicMock()
    pos = MagicMock(); pos.symbol = "ARM"
    alpaca.get_positions.return_value = [pos]

    arm_filled = _alpaca_buy_filled(
        symbol="ARM", qty="3", price="201.21",
        filled_at="2026-04-30T13:34:11+00:00", order_id="o-arm",
    )
    alpaca._client.get_orders.return_value = [arm_filled]

    report = reconcile(client=alpaca, journal=journal,
                       closed_trades_path=closed_path)

    # Stale audit row deleted; ARM is open so no new row written.
    rows = list(ClosedTradeStore(closed_path).all())
    assert rows == []


def test_reconciler_pairs_multiple_round_trips_for_same_symbol(tmp_path):
    """Two separate buy→sell cycles for the same symbol should produce
    two distinct closed_trades rows (FIFO pairing)."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    # Two clean cycles (no dust): first buy is fully closed by first sell,
    # second buy by second sell.
    alpaca._client.get_orders.return_value = [
        _alpaca_buy_filled(symbol="FIL/USD", qty="100", price="0.95",
                           filled_at="2026-04-27T02:11:59+00:00",
                           order_id="buy-1"),
        _alpaca_sell_filled(symbol="FIL/USD", qty="100", price="0.91",
                            filled_at="2026-04-27T10:32:49+00:00",
                            order_id="sell-1"),
        _alpaca_buy_filled(symbol="FIL/USD", qty="200", price="0.92",
                           filled_at="2026-04-28T12:35:34+00:00",
                           order_id="buy-2"),
        _alpaca_sell_filled(symbol="FIL/USD", qty="200", price="0.90",
                            filled_at="2026-04-29T09:47:55+00:00",
                            order_id="sell-2"),
    ]

    closed_path = tmp_path / "closed.db"
    report = reconcile(client=alpaca, journal=journal,
                       closed_trades_path=closed_path)

    rows = sorted(ClosedTradeStore(closed_path).all(),
                  key=lambda r: r.entry_time)
    assert len(rows) == 2
    assert rows[0].entry_order_id == "buy-1"
    assert rows[1].entry_order_id == "buy-2"
    assert rows[0].entry_price == Decimal("0.95")
    assert rows[1].entry_price == Decimal("0.92")
    assert report.reconciled_count == 2


def test_reconciler_skips_lots_with_no_matching_sell(tmp_path):
    """A buy with no matching sell (still-open lot) must not produce a
    closed_trades row."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    alpaca = MagicMock()
    pos = MagicMock(); pos.symbol = "DOTUSD"
    alpaca.get_positions.return_value = [pos]
    alpaca._client.get_orders.return_value = [
        _alpaca_buy_filled(symbol="DOT/USD", qty="419", price="1.26608",
                           filled_at="2026-04-27T02:11:59+00:00",
                           order_id="buy-dot"),
    ]

    closed_path = tmp_path / "closed.db"
    report = reconcile(client=alpaca, journal=journal,
                       closed_trades_path=closed_path)
    assert list(ClosedTradeStore(closed_path).all()) == []
    assert report.reconciled_count == 0


def test_reconciler_round_trip_idempotent_across_runs(tmp_path):
    """Running the reconciler twice with the same Alpaca history must not
    produce duplicate round-trip rows."""
    from trading_bot.reconciler import reconcile
    from trading_bot.reconciliation import ClosedTradeStore

    journal = TradeJournal(tmp_path / "j.db")
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca._client.get_orders.return_value = [
        _alpaca_buy_filled(symbol="FIL/USD", qty="673", price="0.9151",
                           filled_at="2026-04-28T12:35:34+00:00",
                           order_id="buy-fil-1"),
        _alpaca_sell_filled(symbol="FIL/USD", qty="671.32", price="0.8996",
                            filled_at="2026-04-29T09:47:55+00:00",
                            order_id="sell-fil-1"),
    ]

    closed_path = tmp_path / "closed.db"
    r1 = reconcile(client=alpaca, journal=journal, closed_trades_path=closed_path)
    r2 = reconcile(client=alpaca, journal=journal, closed_trades_path=closed_path)
    assert r1.reconciled_count == 1
    assert r2.reconciled_count == 0
    assert len(ClosedTradeStore(closed_path).all()) == 1


def test_reconcile_marks_csp_assigned_when_alpaca_position_disappears(tmp_path):
    """When a short-put position disappears from Alpaca and the underlying now shows
    100 long shares, the reconciler advances the cycle to 'assigned' and emits the
    appropriate alert."""
    import datetime as dt
    from decimal import Decimal
    from unittest.mock import MagicMock
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from trading_bot.state_db import Base, OptionFill, WheelCycle
    from trading_bot.reconciler import reconcile_options
    engine = create_engine(f"sqlite:///{tmp_path/'rec.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(OptionFill(ts=dt.datetime.now(dt.timezone.utc), underlying="AAPL",
                         contract_symbol="AAPL250516P00190000", option_type="CSP",
                         side="SELL", strike=Decimal("190"),
                         expiration=dt.date(2025, 5, 16), qty=1,
                         premium=Decimal("2.10"), alpaca_order_id="o1",
                         cycle_id="c1"))
        s.add(WheelCycle(cycle_id="c1", symbol="AAPL", phase="csp_open",
                         opened_at=dt.datetime.now(dt.timezone.utc),
                         csp_contract="AAPL250516P00190000",
                         csp_strike=Decimal("190"), csp_expiration=dt.date(2025, 5, 16),
                         csp_credit=Decimal("2.10")))
        s.commit()
    option_alpaca = MagicMock()
    option_alpaca.get_option_positions.return_value = []  # CSP gone
    alpaca_eq = MagicMock()
    eq_pos = MagicMock(); eq_pos.symbol = "AAPL"; eq_pos.qty = "100"; eq_pos.avg_entry_price = "190"
    alpaca_eq.get_positions.return_value = [eq_pos]
    alert_q = MagicMock()
    reconcile_options(engine=engine, option_alpaca=option_alpaca,
                      alpaca_equity=alpaca_eq, alert_queue=alert_q)
    with Session(engine) as s:
        cyc = s.query(WheelCycle).one()
        assert cyc.phase == "assigned"
    assert any("wheel_assignment" in str(c) for c in alert_q.mock_calls)
