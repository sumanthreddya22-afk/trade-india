"""Disk-backed cache for Massive grouped-aggregates data.

`bot rank` and other consumers read grouped OHLC from this cache only —
they never call Massive directly. The cache is filled by `bot massive-
refresh`, which is the single place in the system that calls Massive's
`/v2/aggs/grouped` endpoint. This decouples consumer-side trading
windows (8:00 ET premarket-rank, hourly intel-scan) from Massive's
~5 calls/min rate budget.

Schema: one row per (trade_date, ticker). Reads return a DataFrame
indexed by ticker with the columns the universe builder expects
(o, h, l, c, v, vw).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Column, Date, DateTime, Float, String, create_engine, delete, select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Session


GROUPED_DB_PATH = Path("data/massive_grouped.db")


class _Base(DeclarativeBase):
    pass


class _GroupedRow(_Base):
    __tablename__ = "grouped_bars"
    trade_date = Column(Date, primary_key=True)
    ticker = Column(String, primary_key=True)
    o = Column(Float, nullable=False)
    h = Column(Float, nullable=False)
    l = Column(Float, nullable=False)
    c = Column(Float, nullable=False)
    v = Column(Float, nullable=False)
    vw = Column(Float, nullable=False)
    cached_at = Column(DateTime, nullable=False)


class MassiveGroupedCache:
    """SQLite-backed cache of Polygon grouped-aggregates data.

    Idempotent writes (re-store on a given date overwrites prior rows
    for that date). Reads are by date or "latest within window".
    """

    def __init__(self, db_path: Path | str = GROUPED_DB_PATH) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        _Base.metadata.create_all(self._engine)

    def store(self, trade_date: date, df: pd.DataFrame) -> int:
        """Upsert all rows from a grouped DataFrame for `trade_date`.

        Empty DataFrame is a no-op (e.g. Massive returned no results
        for a holiday). Returns number of rows written.
        """
        if df.empty:
            return 0
        now = datetime.utcnow()
        with Session(self._engine) as s:
            s.execute(delete(_GroupedRow).where(_GroupedRow.trade_date == trade_date))
            payload = [
                {
                    "trade_date": trade_date,
                    "ticker": str(ticker),
                    "o": float(row["o"]),
                    "h": float(row["h"]),
                    "l": float(row["l"]),
                    "c": float(row["c"]),
                    "v": float(row["v"]),
                    "vw": float(row.get("vw", 0.0) or 0.0),
                    "cached_at": now,
                }
                for ticker, row in df.iterrows()
            ]
            if payload:
                s.execute(sqlite_insert(_GroupedRow), payload)
            s.commit()
            return len(payload)

    def has(self, trade_date: date) -> bool:
        with Session(self._engine) as s:
            row = s.execute(
                select(_GroupedRow.trade_date)
                .where(_GroupedRow.trade_date == trade_date)
                .limit(1)
            ).first()
            return row is not None

    def latest(self, *, max_age_days: int = 5) -> tuple[date, pd.DataFrame] | None:
        """Return (date, DataFrame) for the most recent cached trading
        day within `max_age_days` of today, or None if nothing fresh."""
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=max_age_days)
        with Session(self._engine) as s:
            recent = s.execute(
                select(_GroupedRow.trade_date)
                .where(_GroupedRow.trade_date >= cutoff)
                .order_by(_GroupedRow.trade_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if recent is None:
                return None
            rows = s.execute(
                select(_GroupedRow).where(_GroupedRow.trade_date == recent)
            ).scalars().all()
        if not rows:
            return None
        df = pd.DataFrame(
            [{"o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v, "vw": r.vw} for r in rows],
            index=[r.ticker for r in rows],
        )
        return recent, df

    def evict_older_than(self, *, days: int = 30) -> int:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        with Session(self._engine) as s:
            result = s.execute(delete(_GroupedRow).where(_GroupedRow.trade_date < cutoff))
            s.commit()
            return result.rowcount or 0
