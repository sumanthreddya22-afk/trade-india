"""News sentiment cache + entry filter (Plan 6c).

Pulls per-ticker article sentiment from Massive's `/v2/reference/news`
endpoint, aggregates into a daily score per (symbol, date), and exposes
a simple gate:

    score >= sentiment_floor → allow entry
    score <  sentiment_floor → skip entry

The strategy code stays unaware of news sources; it just sees a numeric
score in [-1, +1] (or None if no data). The orchestrator + backtester
both call `score_for(symbol, lookback_days=3)` before passing the
`sig.action == BUY` test.

Cache: SQLite at `data/news_sentiment.db` keyed by (symbol, date). Each
row stores the aggregate score, n_articles, and the dominant label.

Default sentiment_floor is **None** (filter disabled) until a backtest
sweep finds a value that improves PF. Once found, set in `strategy/
config.yaml::strategy.sentiment_floor`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    Column, Date, DateTime, Float, Integer, String, create_engine, select,
)
from sqlalchemy.orm import DeclarativeBase, Session

from trading_bot.massive_client import MassiveAuthError, MassiveClient


SENTIMENT_DB_PATH = Path("data/news_sentiment.db")


class _Base(DeclarativeBase):
    pass


class _SentRow(_Base):
    __tablename__ = "news_sentiment"
    symbol = Column(String, primary_key=True)
    snapshot_date = Column(Date, primary_key=True)
    score = Column(Float, nullable=False)         # -1..+1 average
    n_articles = Column(Integer, nullable=False)
    dominant_label = Column(String, nullable=False)
    cached_at = Column(DateTime, nullable=False)


@dataclass(frozen=True)
class SentimentReading:
    symbol: str
    snapshot_date: date
    score: float
    n_articles: int
    dominant_label: str


class SentimentCache:
    def __init__(self, db_path: Path | str = SENTIMENT_DB_PATH) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        _Base.metadata.create_all(self._engine)

    def write(self, r: SentimentReading) -> None:
        with Session(self._engine) as s:
            existing = s.get(_SentRow, {"symbol": r.symbol, "snapshot_date": r.snapshot_date})
            if existing is None:
                s.add(_SentRow(
                    symbol=r.symbol, snapshot_date=r.snapshot_date,
                    score=r.score, n_articles=r.n_articles,
                    dominant_label=r.dominant_label,
                    cached_at=datetime.utcnow(),
                ))
            else:
                existing.score = r.score
                existing.n_articles = r.n_articles
                existing.dominant_label = r.dominant_label
                existing.cached_at = datetime.utcnow()
            s.commit()

    def latest(self, symbol: str, *, max_age_days: int = 7) -> SentimentReading | None:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=max_age_days)
        with Session(self._engine) as s:
            row = s.execute(
                select(_SentRow)
                .where(_SentRow.symbol == symbol)
                .where(_SentRow.snapshot_date >= cutoff)
                .order_by(_SentRow.snapshot_date.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return None
        return SentimentReading(
            symbol=row.symbol, snapshot_date=row.snapshot_date,
            score=row.score, n_articles=row.n_articles,
            dominant_label=row.dominant_label,
        )


# Cap the per-run symbol count so an inflated active universe can't blow
# through the Massive rate budget. 50 × 13s/call = ~11 min worst case.
MAX_SYMBOLS_PER_WARM = 50


def warm_for_symbols(
    symbols: list[str],
    *,
    lookback_days: int = 3,
    cache: SentimentCache | None = None,
    massive: MassiveClient | None = None,
) -> dict[str, SentimentReading | None]:
    """Pull fresh sentiment for each symbol and cache it.

    Skips symbols that already have a row in the cache from today
    (idempotent: re-running within the same trading day is a no-op
    on the Massive side). Caps input at MAX_SYMBOLS_PER_WARM.

    Returns {symbol -> reading or None on missing data}.
    """
    cache = cache or SentimentCache()
    try:
        massive = massive or MassiveClient()
    except MassiveAuthError:
        return {sym: None for sym in symbols}

    out: dict[str, SentimentReading | None] = {}
    today = datetime.now(timezone.utc).date()

    capped = symbols[:MAX_SYMBOLS_PER_WARM]
    for sym in capped:
        existing = cache.latest(sym, max_age_days=1)
        if existing is not None and existing.snapshot_date == today:
            out[sym] = existing
            continue
        try:
            score, n, label = massive.aggregate_sentiment(sym, lookback_days=lookback_days)
        except Exception:
            out[sym] = None
            continue
        if n == 0:
            out[sym] = None
            continue
        reading = SentimentReading(
            symbol=sym, snapshot_date=today,
            score=score, n_articles=n, dominant_label=label,
        )
        cache.write(reading)
        out[sym] = reading

    return out


def score_for(
    symbol: str,
    *,
    cache: SentimentCache | None = None,
    max_age_days: int = 3,
) -> float | None:
    """Read the most recent cached score. Returns None if no data fresh
    enough — caller decides whether to gate or pass through."""
    c = cache or SentimentCache()
    r = c.latest(symbol, max_age_days=max_age_days)
    return r.score if r is not None else None


def passes_filter(score: float | None, *, floor: float | None) -> bool:
    """Boolean gate. None floor → always pass (filter disabled). None
    score → always pass (no data shouldn't block entries; filter should
    only veto explicitly-negative names)."""
    if floor is None:
        return True
    if score is None:
        return True
    return score >= floor
