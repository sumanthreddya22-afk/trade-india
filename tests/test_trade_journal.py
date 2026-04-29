# tests/test_trade_journal.py
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.trade_journal import TradeJournal, TradeRecord


@pytest.fixture
def journal(tmp_path: Path) -> TradeJournal:
    return TradeJournal(tmp_path / "test.db")


def test_journal_appends_and_reads_back(journal: TradeJournal):
    rec = TradeRecord(
        timestamp=datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc),
        symbol="AAPL",
        side="buy",
        qty=Decimal("10"),
        price=Decimal("195.00"),
        asset_class="stock",
        strategy="momentum",
        regime="trending_up",
        entry_order_id="e1",
        stop_loss_order_id="s1",
        notes="initial entry",
    )
    journal.append(rec)
    rows = journal.all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].qty == Decimal("10")


def test_journal_is_append_only(journal: TradeJournal):
    """Sanity: journal exposes no update/delete API."""
    assert not hasattr(journal, "update")
    assert not hasattr(journal, "delete")


def test_journal_filters_by_date_range(journal: TradeJournal):
    base = datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc)
    for i in range(3):
        journal.append(
            TradeRecord(
                timestamp=base.replace(day=25 + i),
                symbol=f"S{i}",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                asset_class="stock",
                strategy="momentum",
                regime="trending_up",
                entry_order_id=f"e{i}",
                stop_loss_order_id=f"s{i}",
                notes="",
            )
        )
    middle = base.replace(day=26)
    rows = journal.between(middle, middle.replace(hour=23, minute=59))
    assert len(rows) == 1
    assert rows[0].symbol == "S1"


# ---------------------------------------------------------------------------
# Task 2 (A5): Idempotent append + cleanup_duplicates
# ---------------------------------------------------------------------------

import datetime as dt
from decimal import Decimal as D


def _rec(symbol="AAPL", entry_order_id="abc-123") -> TradeRecord:
    return TradeRecord(
        timestamp=dt.datetime(2026, 4, 28, 13, 7, tzinfo=dt.timezone.utc),
        symbol=symbol, side="buy", qty=D("3"), price=D("220.27"),
        asset_class="stock", strategy="momentum", regime="trending_up",
        entry_order_id=entry_order_id, stop_loss_order_id="stop-1",
        notes="rsi=61.0 macd>-3.202 close>EMA20",
    )


def test_journal_append_dedupes_by_entry_order_id(tmp_path):
    j = TradeJournal(tmp_path / "j.db")
    j.append(_rec())
    j.append(_rec())  # duplicate
    j.append(_rec())  # triple

    rows = j.all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"


def test_journal_append_distinct_order_ids_kept(tmp_path):
    j = TradeJournal(tmp_path / "j.db")
    j.append(_rec(entry_order_id="o-1"))
    j.append(_rec(entry_order_id="o-2"))

    rows = j.all()
    assert len(rows) == 2


def test_journal_traded_today_returns_buy_symbols(tmp_path):
    """traded_today() returns only symbols with a BUY recorded on the reference date."""
    j = TradeJournal(tmp_path / "j.db")
    today = datetime(2026, 4, 27, 17, 7, tzinfo=timezone.utc)
    yesterday = datetime(2026, 4, 26, 17, 7, tzinfo=timezone.utc)

    def _rec(symbol, side, ts, order_id):
        return TradeRecord(
            timestamp=ts, symbol=symbol, side=side, qty=Decimal("3"),
            price=Decimal("220"), asset_class="stock", strategy="momentum",
            regime="trending_up", entry_order_id=order_id,
            stop_loss_order_id=f"s-{order_id}", notes="",
        )

    j.append(_rec("AMD", "buy", today, "ord-1"))       # buy today → included
    j.append(_rec("CLS", "sell", today, "ord-2"))      # sell today → excluded
    j.append(_rec("NVDA", "buy", yesterday, "ord-3"))  # buy yesterday → excluded

    result = j.traded_today(as_of=today)
    assert result == {"AMD"}


def test_journal_cleanup_removes_existing_duplicates(tmp_path):
    """If a journal db already contains duplicates from before this fix,
    calling TradeJournal(...).cleanup_duplicates() removes them."""
    db_path = tmp_path / "j.db"
    j = TradeJournal(db_path)
    # Force duplicate insertion via raw SQL to simulate pre-fix state.
    from sqlalchemy import text
    with j._engine.begin() as c:  # noqa: SLF001
        for ts_hour in (13, 20):
            c.execute(
                text(
                    "INSERT INTO trades (timestamp, symbol, side, qty, price, "
                    "asset_class, strategy, regime, entry_order_id, "
                    "stop_loss_order_id, notes) VALUES "
                    "(:ts, 'AAPL', 'buy', 3, 220.27, 'stock', 'momentum', "
                    "'trending_up', 'dup-order', 'stop-1', 'x')"
                ),
                {"ts": dt.datetime(2026, 4, 27, ts_hour, 7, tzinfo=dt.timezone.utc)},
            )
    assert len(j.all()) == 2  # before cleanup

    removed = j.cleanup_duplicates()
    assert removed == 1
    assert len(j.all()) == 1
