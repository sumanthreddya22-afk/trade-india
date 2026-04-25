"""Reconcile journal entries with Alpaca order history.

Tracks closed trades in a separate `closed_trades` table so the evolution
loop has a clean dataset of realized outcomes (entry, exit, P&L, hold time,
strategy, regime) to learn from.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session

from trading_bot.config import Settings
from trading_bot.exceptions import AlpacaClientError
from trading_bot.trade_journal import TradeJournal


class _Base(DeclarativeBase):
    pass


class _ClosedTradeRow(_Base):
    __tablename__ = "closed_trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(32), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    qty = Column(Numeric(20, 8), nullable=False)
    entry_price = Column(Numeric(20, 8), nullable=False)
    exit_price = Column(Numeric(20, 8), nullable=False)
    realized_pnl = Column(Numeric(20, 8), nullable=False)
    pnl_pct = Column(Float, nullable=False)
    strategy = Column(String(32), nullable=False)
    regime = Column(String(32), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True), nullable=False)
    hold_hours = Column(Float, nullable=False)
    entry_order_id = Column(String(64), nullable=False, unique=True)
    notes = Column(Text, nullable=False, default="")


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    pnl_pct: float
    strategy: str
    regime: str
    entry_time: datetime
    exit_time: datetime
    hold_hours: float
    entry_order_id: str
    notes: str = ""


class ClosedTradeStore:
    """SQLite append-only store for closed trades."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)
        _Base.metadata.create_all(self._engine)

    def append(self, trade: ClosedTrade) -> None:
        with Session(self._engine) as s:
            existing = s.execute(
                select(_ClosedTradeRow).where(
                    _ClosedTradeRow.entry_order_id == trade.entry_order_id
                )
            ).first()
            if existing:
                return  # idempotent — already recorded
            s.add(_ClosedTradeRow(
                symbol=trade.symbol, side=trade.side, qty=trade.qty,
                entry_price=trade.entry_price, exit_price=trade.exit_price,
                realized_pnl=trade.realized_pnl, pnl_pct=trade.pnl_pct,
                strategy=trade.strategy, regime=trade.regime,
                entry_time=trade.entry_time, exit_time=trade.exit_time,
                hold_hours=trade.hold_hours, entry_order_id=trade.entry_order_id,
                notes=trade.notes,
            ))
            s.commit()

    def all(self) -> list[ClosedTrade]:
        with Session(self._engine) as s:
            rows = s.execute(
                select(_ClosedTradeRow).order_by(_ClosedTradeRow.exit_time)
            ).scalars().all()
            return [self._to(r) for r in rows]

    def by_strategy(self, strategy: str) -> list[ClosedTrade]:
        with Session(self._engine) as s:
            rows = s.execute(
                select(_ClosedTradeRow).where(_ClosedTradeRow.strategy == strategy)
            ).scalars().all()
            return [self._to(r) for r in rows]

    @staticmethod
    def _to(r: _ClosedTradeRow) -> ClosedTrade:
        return ClosedTrade(
            symbol=r.symbol, side=r.side,
            qty=Decimal(str(r.qty)),
            entry_price=Decimal(str(r.entry_price)),
            exit_price=Decimal(str(r.exit_price)),
            realized_pnl=Decimal(str(r.realized_pnl)),
            pnl_pct=float(r.pnl_pct),
            strategy=r.strategy, regime=r.regime,
            entry_time=r.entry_time, exit_time=r.exit_time,
            hold_hours=float(r.hold_hours),
            entry_order_id=r.entry_order_id, notes=r.notes,
        )


@dataclass(frozen=True)
class ReconcileSummary:
    new_closed: int
    open_drift: list[str]  # symbols where journal disagrees with Alpaca
    notes: str = ""


class Reconciler:
    """Walks recent journal entries, finds matching Alpaca order outcomes,
    populates the closed_trades table with realized P&L.
    """

    def __init__(
        self,
        settings: Settings,
        journal: TradeJournal,
        closed_store: ClosedTradeStore,
    ) -> None:
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        self._journal = journal
        self._closed = closed_store

    def reconcile(self, lookback_days: int = 30) -> ReconcileSummary:
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=datetime.now(timezone.utc) - timedelta(days=lookback_days),
                limit=500,
                nested=True,
            )
            closed_orders = self._client.get_orders(req)
        except Exception as e:
            raise AlpacaClientError(f"reconcile fetch_orders failed: {e}") from e

        # Index closed orders by parent order id (entry order id)
        by_id = {str(o.id): o for o in closed_orders}

        new_count = 0
        for rec in self._journal.all():
            entry_id = rec.entry_order_id
            entry_order = by_id.get(entry_id)
            if entry_order is None:
                continue
            if str(entry_order.status).lower() not in {"orderstatus.filled", "filled"}:
                # Entry never filled (canceled, rejected) — not a closed trade.
                continue
            entry_fill_price = entry_order.filled_avg_price
            entry_fill_time = entry_order.filled_at
            if entry_fill_price is None or entry_fill_time is None:
                continue

            # Find the bracket child (stop or take-profit) that filled
            exit_price = None
            exit_time = None
            for leg in (entry_order.legs or []):
                if str(leg.status).lower() in {"orderstatus.filled", "filled"}:
                    if leg.filled_avg_price is not None and leg.filled_at is not None:
                        exit_price = leg.filled_avg_price
                        exit_time = leg.filled_at
                        break
            if exit_price is None or exit_time is None:
                continue  # still open — skip

            entry_dec = Decimal(str(entry_fill_price))
            exit_dec = Decimal(str(exit_price))
            qty_dec = Decimal(str(rec.qty))
            if rec.side == "buy":
                pnl = (exit_dec - entry_dec) * qty_dec
            else:
                pnl = (entry_dec - exit_dec) * qty_dec
            pnl_pct = float((pnl / (entry_dec * qty_dec)) * Decimal("100")) if entry_dec > 0 else 0.0
            hold = (exit_time - entry_fill_time).total_seconds() / 3600.0

            self._closed.append(ClosedTrade(
                symbol=rec.symbol,
                side=rec.side,
                qty=qty_dec,
                entry_price=entry_dec,
                exit_price=exit_dec,
                realized_pnl=pnl,
                pnl_pct=pnl_pct,
                strategy=rec.strategy,
                regime=rec.regime,
                entry_time=entry_fill_time,
                exit_time=exit_time,
                hold_hours=hold,
                entry_order_id=entry_id,
                notes=rec.notes,
            ))
            new_count += 1

        return ReconcileSummary(new_closed=new_count, open_drift=[])
