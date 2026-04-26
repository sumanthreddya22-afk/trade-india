"""Historical bar cache for the backtest harness.

Pulling 60d-lookback bars × 25 symbols × 500 trading days = ~750k API calls
per backtest run is unworkable. Instead, this module caches daily bars in a
SQLite table keyed by `(symbol, date)`. Warm once per (symbol, date-range)
via Alpaca; subsequent runs hit the cache.

Schema:
    bars(symbol TEXT, date DATE, open, high, low, close, volume,
         cached_at DATETIME, PRIMARY KEY (symbol, date))
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Index,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session

from trading_bot.market_data import MarketDataClient

CACHE_TTL_HOURS = 24


class _Base(DeclarativeBase):
    pass


class _BarRow(_Base):
    __tablename__ = "bars"
    symbol = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    cached_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("ix_bars_symbol_date", "symbol", "date"),)


@dataclass(frozen=True)
class BacktestBar:
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarStore:
    """SQLite-backed historical daily-bar cache.

    The interface mirrors `MarketDataClient.get_daily_bars` so the simulator
    can drop the result straight into `compute_indicators`.
    """

    def __init__(self, db_path: Path | str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        _Base.metadata.create_all(self._engine)

    # ---- read ----

    def get(self, symbol: str, *, end_date: date, lookback_days: int) -> pd.DataFrame:
        """Return up to `lookback_days` bars ending on or before `end_date`."""
        start_date = end_date - timedelta(days=lookback_days * 2)  # weekend padding
        with Session(self._engine) as s:
            rows = s.execute(
                select(_BarRow)
                .where(_BarRow.symbol == symbol)
                .where(_BarRow.date >= start_date)
                .where(_BarRow.date <= end_date)
                .order_by(_BarRow.date)
            ).scalars().all()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(
            [
                {"open": r.open, "high": r.high, "low": r.low,
                 "close": r.close, "volume": r.volume}
                for r in rows
            ],
            index=pd.DatetimeIndex(
                [pd.Timestamp(r.date) for r in rows], name="timestamp"
            ),
        )
        return df.tail(lookback_days)

    def get_bar(self, symbol: str, on: date) -> BacktestBar | None:
        """Return the bar for an exact date, or None if missing (e.g. weekend)."""
        with Session(self._engine) as s:
            row = s.execute(
                select(_BarRow)
                .where(_BarRow.symbol == symbol)
                .where(_BarRow.date == on)
            ).scalar_one_or_none()
        if row is None:
            return None
        return BacktestBar(
            symbol=row.symbol, date=row.date,
            open=row.open, high=row.high, low=row.low,
            close=row.close, volume=row.volume,
        )

    def trading_dates(self, symbol: str, *, from_date: date, to_date: date) -> list[date]:
        """All cached dates for `symbol` within range, ascending. Used as a
        liquidity-checked trading-day calendar (since SPY trades whenever the
        US equity market is open)."""
        with Session(self._engine) as s:
            rows = s.execute(
                select(_BarRow.date)
                .where(_BarRow.symbol == symbol)
                .where(_BarRow.date >= from_date)
                .where(_BarRow.date <= to_date)
                .order_by(_BarRow.date)
            ).scalars().all()
        return list(rows)

    # ---- write ----

    def is_warm(self, symbol: str, *, from_date: date, to_date: date) -> bool:
        """True if there are cached rows covering both endpoints AND the cache
        is fresh (within CACHE_TTL_HOURS)."""
        with Session(self._engine) as s:
            row = s.execute(
                select(_BarRow.cached_at)
                .where(_BarRow.symbol == symbol)
                .where(_BarRow.date >= from_date)
                .where(_BarRow.date <= to_date)
                .order_by(_BarRow.cached_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return False
        age = datetime.now(timezone.utc) - row.replace(tzinfo=timezone.utc)
        return age < timedelta(hours=CACHE_TTL_HOURS)

    def warm(
        self,
        symbols: list[str],
        *,
        from_date: date,
        to_date: date,
        market: MarketDataClient,
        refresh: bool = False,
    ) -> dict[str, int]:
        """Fetch each symbol's full date range from Alpaca and upsert.
        Skips symbols whose cache is already warm unless refresh=True.
        Returns {symbol: rows_inserted}."""
        results: dict[str, int] = {}
        lookback = (to_date - from_date).days + 30  # padding
        for sym in symbols:
            if not refresh and self.is_warm(sym, from_date=from_date, to_date=to_date):
                results[sym] = 0
                continue
            try:
                df = market.get_daily_bars(sym, lookback_days=lookback)
            except Exception:
                results[sym] = -1  # signal: fetch failed
                continue
            if df.empty:
                results[sym] = 0
                continue

            cached_at = datetime.utcnow()
            with Session(self._engine) as s:
                count = 0
                for ts, row in df.iterrows():
                    d = ts.date() if hasattr(ts, "date") else ts
                    existing = s.get(_BarRow, {"symbol": sym, "date": d})
                    if existing is None:
                        s.add(_BarRow(
                            symbol=sym, date=d,
                            open=float(row["open"]), high=float(row["high"]),
                            low=float(row["low"]), close=float(row["close"]),
                            volume=float(row["volume"]),
                            cached_at=cached_at,
                        ))
                        count += 1
                    else:
                        existing.open = float(row["open"])
                        existing.high = float(row["high"])
                        existing.low = float(row["low"])
                        existing.close = float(row["close"])
                        existing.volume = float(row["volume"])
                        existing.cached_at = cached_at
                s.commit()
                results[sym] = count
        return results
