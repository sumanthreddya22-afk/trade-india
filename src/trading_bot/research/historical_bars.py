"""Historical-bars store for backtests.

A separate SQLite DB at ``data/historical_bars.db`` because:
  * the ledger is hash-chained and append-only; backfilling years of
    daily bars into it would bloat the chain meaninglessly,
  * the bars are *immutable historical data*, not a kernel state — they
    have a different audit story (provenance = source + as-of date),
  * we want the backtest to be runnable on a fresh checkout without
    seeding the live ledger.

Schema:

    bar_daily (
      symbol         TEXT,
      bar_date       TEXT,         -- YYYY-MM-DD (UTC; market-close aligned)
      open           REAL,
      high           REAL,
      low            REAL,
      close          REAL,         -- adjusted close (split + dividend)
      volume         REAL,
      vwap           REAL,
      source         TEXT,         -- e.g. "alpaca:1Day:adj"
      fetched_at     TEXT,         -- ISO-8601 UTC
      PRIMARY KEY (symbol, bar_date)
    )

The loader is idempotent: re-running for the same window replaces rows
via INSERT OR REPLACE. Provenance is captured per-row via
``source`` + ``fetched_at`` so a future audit can ask "what version of
the bars did Tier-1 artifact X consume?" by hashing the rows.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)


DEFAULT_HISTORICAL_PATH = Path("data") / "historical_bars.db"

DDL = """
CREATE TABLE IF NOT EXISTS bar_daily (
    symbol      TEXT NOT NULL,
    bar_date    TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    vwap        REAL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, bar_date)
);
CREATE INDEX IF NOT EXISTS idx_bar_daily_symbol ON bar_daily(symbol);
CREATE INDEX IF NOT EXISTS idx_bar_daily_date ON bar_daily(bar_date);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in DDL.strip().split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()


def open_store(path: Path = DEFAULT_HISTORICAL_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)
    return conn


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    bar_date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    source: str = "alpaca:1Day:adj"


def upsert_bars(conn: sqlite3.Connection, bars: Iterable[DailyBar]) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = [
        (b.symbol, b.bar_date.isoformat(), b.open, b.high, b.low, b.close,
         b.volume, b.vwap, b.source, now)
        for b in bars
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO bar_daily "
        "(symbol, bar_date, open, high, low, close, volume, vwap, source, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_bars(
    conn: sqlite3.Connection,
    *,
    symbols: Sequence[str],
    start: dt.date,
    end: dt.date,
) -> dict[str, list[DailyBar]]:
    """Return ``{symbol: [DailyBar, …]}`` sorted ascending by date.

    Symbols with no rows in the window are present with an empty list.
    Callers downstream of this must handle short series (e.g. an ETF
    that didn't list until 2014).
    """
    out: dict[str, list[DailyBar]] = {s: [] for s in symbols}
    placeholders = ",".join("?" for _ in symbols)
    cur = conn.execute(
        f"SELECT symbol, bar_date, open, high, low, close, volume, vwap, source "
        f"FROM bar_daily WHERE symbol IN ({placeholders}) "
        f"AND bar_date >= ? AND bar_date <= ? "
        f"ORDER BY symbol, bar_date ASC",
        (*symbols, start.isoformat(), end.isoformat()),
    )
    for row in cur.fetchall():
        sym, ds, op, hi, lo, cl, vol, vw, src = row
        out[sym].append(DailyBar(
            symbol=sym, bar_date=dt.date.fromisoformat(ds),
            open=op, high=hi, low=lo, close=cl, volume=vol,
            vwap=vw, source=src,
        ))
    return out


def fetch_bars_from_alpaca(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
    adapter=None,
) -> list[DailyBar]:
    """Pull daily bars (adjusted) for ``symbols`` between ``start`` and
    ``end`` inclusive, from Alpaca."""
    if adapter is None:
        from trading_bot.ingest.alpaca_adapter import AlpacaAdapter
        adapter = AlpacaAdapter()
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc),
        end=dt.datetime.combine(end, dt.time.max, tzinfo=dt.timezone.utc),
        # adjustment="all" → split + dividend adjusted close. Required
        # for momentum signal correctness across ex-dividend dates.
        adjustment="all",
    )
    bs = adapter.data.get_stock_bars(req)
    out: list[DailyBar] = []
    for sym in symbols:
        series = bs[sym] if sym in bs.data else []
        for b in series:
            ts = getattr(b, "timestamp", None)
            if ts is None:
                continue
            bar_date = ts.date() if hasattr(ts, "date") else dt.date.today()
            out.append(DailyBar(
                symbol=sym, bar_date=bar_date,
                open=float(b.open or 0), high=float(b.high or 0),
                low=float(b.low or 0), close=float(b.close or 0),
                volume=float(b.volume or 0),
                vwap=float(getattr(b, "vwap", 0) or 0) or None,
                source="alpaca:1Day:adj",
            ))
    return out


__all__ = [
    "DDL", "DEFAULT_HISTORICAL_PATH", "DailyBar",
    "ensure_schema", "fetch_bars_from_alpaca", "load_bars",
    "open_store", "upsert_bars",
]
