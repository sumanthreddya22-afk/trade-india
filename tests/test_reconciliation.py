from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore


@pytest.fixture
def store(tmp_path: Path) -> ClosedTradeStore:
    return ClosedTradeStore(tmp_path / "closed.db")


def _trade(eid: str, pnl: float = 50.0, strategy: str = "momentum") -> ClosedTrade:
    return ClosedTrade(
        symbol="AAPL", side="buy",
        qty=Decimal("3"), entry_price=Decimal("220"), exit_price=Decimal("230"),
        realized_pnl=Decimal(str(pnl)), pnl_pct=2.27,
        strategy=strategy, regime="trending_up",
        entry_time=datetime(2026, 4, 25, tzinfo=timezone.utc),
        exit_time=datetime(2026, 4, 26, tzinfo=timezone.utc),
        hold_hours=24.0,
        entry_order_id=eid,
        notes="test",
    )


def test_closed_store_appends_and_reads(store: ClosedTradeStore):
    store.append(_trade("e-1"))
    rows = store.all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].realized_pnl == Decimal("50")


def test_closed_store_idempotent_on_same_order_id(store: ClosedTradeStore):
    store.append(_trade("e-1"))
    store.append(_trade("e-1"))  # same id
    assert len(store.all()) == 1


def test_closed_store_filter_by_strategy(store: ClosedTradeStore):
    store.append(_trade("e-1", strategy="momentum"))
    store.append(_trade("e-2", strategy="mean_reversion"))
    assert len(store.by_strategy("momentum")) == 1
    assert len(store.by_strategy("mean_reversion")) == 1
