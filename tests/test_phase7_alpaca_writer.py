"""ingest.alpaca_writer — pure unit tests with a fake bars fetcher."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.ingest.alpaca_writer import (
    LANE_EQUITY, SOURCE_ID_ALPACA, ingest_bars_once,
)


@pytest.fixture()
def ledger(tmp_path) -> Path:
    p = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def _fixed_bars(symbols=None, **_):
    ts = dt.datetime(2026, 5, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
    return {
        s: {"ts": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100}
        for s in (symbols or ())
    }


def test_empty_symbols_returns_zero(ledger):
    assert ingest_bars_once(ledger_db=ledger, symbols=(), bars_fetcher=_fixed_bars) == 0


def test_writes_watermark(ledger):
    n = ingest_bars_once(
        ledger_db=ledger, symbols=("SPY", "QQQ"), bars_fetcher=_fixed_bars,
    )
    assert n == 2
    # Read back the watermark row directly.
    import sqlite3
    conn = sqlite3.connect(str(ledger))
    try:
        row = conn.execute(
            "SELECT source_id, lane FROM data_watermark "
            "WHERE source_id=? AND lane=?",
            (SOURCE_ID_ALPACA, LANE_EQUITY),
        ).fetchone()
    finally:
        conn.close()
    assert row == (SOURCE_ID_ALPACA, LANE_EQUITY)


def test_empty_bars_payload(ledger):
    # bars_fetcher returns nothing — should be a no-op, not an error.
    n = ingest_bars_once(
        ledger_db=ledger, symbols=("SPY",), bars_fetcher=lambda **_: {},
    )
    assert n == 0
