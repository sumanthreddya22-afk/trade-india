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
