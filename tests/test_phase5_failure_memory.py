"""Phase 5 — failure memory (90-day reject cache)."""
from __future__ import annotations

import datetime as dt

from trading_bot.research import is_blocked, record_rejection


def test_unrecorded_hypothesis_is_not_blocked(ledger_conn) -> None:
    blocked, reason = is_blocked(ledger_conn, hypothesis_hash="abc")
    assert not blocked
    assert reason is None


def test_recent_rejection_blocks(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc)
    record_rejection(ledger_conn, hypothesis_hash="h1",
                     reason="DSR < 0.50", now=now)
    blocked, reason = is_blocked(
        ledger_conn, hypothesis_hash="h1", now=now,
    )
    assert blocked
    assert "DSR" in reason


def test_old_rejection_no_longer_blocks(ledger_conn) -> None:
    rejected_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    record_rejection(ledger_conn, hypothesis_hash="h2",
                     reason="overfit", now=rejected_at)
    # 100 days later
    later = rejected_at + dt.timedelta(days=100)
    blocked, _ = is_blocked(
        ledger_conn, hypothesis_hash="h2", now=later,
    )
    assert not blocked


def test_different_hash_not_blocked(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc)
    record_rejection(ledger_conn, hypothesis_hash="h1",
                     reason="x", now=now)
    blocked, _ = is_blocked(
        ledger_conn, hypothesis_hash="h2_different", now=now,
    )
    assert not blocked


def test_table_absent_returns_unblocked(ledger_conn) -> None:
    """Tolerate first-time lookups against a fresh DB (no failure_memory
    table yet)."""
    blocked, reason = is_blocked(ledger_conn, hypothesis_hash="x")
    assert not blocked
    assert reason is None
