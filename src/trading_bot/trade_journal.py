# src/trading_bot/trade_journal.py
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session


class _Base(DeclarativeBase):
    pass


class _TradeRow(_Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    qty = Column(Numeric(20, 8), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    asset_class = Column(String(16), nullable=False)
    strategy = Column(String(32), nullable=False)
    regime = Column(String(32), nullable=False)
    entry_order_id = Column(String(64), nullable=False)
    stop_loss_order_id = Column(String(64), nullable=False)
    notes = Column(Text, nullable=False, default="")


@dataclass(frozen=True)
class TradeRecord:
    timestamp: datetime
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    asset_class: str
    strategy: str
    regime: str
    entry_order_id: str
    stop_loss_order_id: str
    notes: str


class TradeJournal:
    """Append-only SQLite trade journal."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)
        _Base.metadata.create_all(self._engine)

    def append(self, rec: TradeRecord) -> None:
        with Session(self._engine) as s:
            s.add(
                _TradeRow(
                    timestamp=rec.timestamp,
                    symbol=rec.symbol,
                    side=rec.side,
                    qty=rec.qty,
                    price=rec.price,
                    asset_class=rec.asset_class,
                    strategy=rec.strategy,
                    regime=rec.regime,
                    entry_order_id=rec.entry_order_id,
                    stop_loss_order_id=rec.stop_loss_order_id,
                    notes=rec.notes,
                )
            )
            s.commit()

    def all(self) -> list[TradeRecord]:
        with Session(self._engine) as s:
            rows = s.execute(select(_TradeRow).order_by(_TradeRow.timestamp)).scalars().all()
            return [self._to_record(r) for r in rows]

    def between(self, start: datetime, end: datetime) -> list[TradeRecord]:
        with Session(self._engine) as s:
            rows = (
                s.execute(
                    select(_TradeRow)
                    .where(_TradeRow.timestamp >= start, _TradeRow.timestamp <= end)
                    .order_by(_TradeRow.timestamp)
                )
                .scalars()
                .all()
            )
            return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(r: _TradeRow) -> TradeRecord:
        return TradeRecord(
            timestamp=r.timestamp,
            symbol=r.symbol,
            side=r.side,
            qty=Decimal(str(r.qty)),
            price=Decimal(str(r.price)),
            asset_class=r.asset_class,
            strategy=r.strategy,
            regime=r.regime,
            entry_order_id=r.entry_order_id,
            stop_loss_order_id=r.stop_loss_order_id,
            notes=r.notes,
        )
