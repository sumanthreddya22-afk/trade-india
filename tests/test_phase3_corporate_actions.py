"""Phase 3 — corporate actions: record, cross-check, pure-math helpers."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from trading_bot.ingest import (
    CorporateAction, apply_dividend_to_cash, apply_split_to_price,
    apply_split_to_qty, cross_check, record_action,
)


def _action(symbol="SPY", source="alpaca", factor=2.0,
            ex_date=dt.date(2026, 5, 13)) -> CorporateAction:
    return CorporateAction(
        symbol=symbol, action_type="split", ex_date=ex_date,
        factor=factor, source_id=source,
        raw_payload={"source": source, "x": "irrelevant"},
    )


def test_record_action_appends_and_hash_chains(ledger_conn) -> None:
    seq = record_action(ledger_conn, _action())
    assert seq == 1
    cur = ledger_conn.cursor()
    cur.execute("SELECT prev_hash, this_hash FROM corporate_action WHERE ledger_seq=1")
    prev, this = cur.fetchone()
    assert prev == "0" * 64
    assert len(this) == 64


def test_record_action_unique_per_source(ledger_conn) -> None:
    record_action(ledger_conn, _action(source="alpaca"))
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE"):
        record_action(ledger_conn, _action(source="alpaca"))


def test_record_action_different_sources_coexist(ledger_conn) -> None:
    record_action(ledger_conn, _action(source="alpaca"))
    record_action(ledger_conn, _action(source="yfinance"))
    cur = ledger_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM corporate_action")
    assert cur.fetchone()[0] == 2


def test_cross_check_match(ledger_conn) -> None:
    record_action(ledger_conn, _action(source="alpaca", factor=2.0))
    record_action(ledger_conn, _action(source="yfinance", factor=2.0))
    res = cross_check(ledger_conn, symbol="SPY", action_type="split",
                      ex_date=dt.date(2026, 5, 13))
    assert res.match
    assert set(res.sources) == {"alpaca", "yfinance"}


def test_cross_check_mismatch(ledger_conn) -> None:
    record_action(ledger_conn, _action(source="alpaca", factor=2.0))
    record_action(ledger_conn, _action(source="yfinance", factor=3.0))
    res = cross_check(ledger_conn, symbol="SPY", action_type="split",
                      ex_date=dt.date(2026, 5, 13))
    assert not res.match
    assert "mismatch" in res.note


def test_cross_check_single_source_is_not_a_match(ledger_conn) -> None:
    record_action(ledger_conn, _action(source="alpaca", factor=2.0))
    res = cross_check(ledger_conn, symbol="SPY", action_type="split",
                      ex_date=dt.date(2026, 5, 13))
    assert not res.match
    assert "only one source" in res.note


def test_cross_check_no_rows(ledger_conn) -> None:
    # Force the table to exist by recording an unrelated row first.
    record_action(ledger_conn, _action(symbol="OTHER", source="alpaca"))
    res = cross_check(ledger_conn, symbol="GHOST", action_type="split",
                      ex_date=dt.date(2026, 5, 13))
    assert not res.match
    assert "no rows" in res.note


def test_apply_split_math() -> None:
    assert apply_split_to_qty(10, 2.0) == 20
    assert apply_split_to_price(100, 2.0) == 50
    assert apply_split_to_price(100, 0) == 100      # safe div-by-zero


def test_apply_dividend_math() -> None:
    assert apply_dividend_to_cash(100, 1.25) == 125.0
