"""Phase 1 — single-writer lock semantics."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from trading_bot.ledger import WriterLockHeld, acquire_writer_lock
from trading_bot.ledger.connection import (
    WRITER_LOCK_FILENAME, _is_pid_alive,
)


def test_acquire_releases_on_exit(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    lock = tmp_path / WRITER_LOCK_FILENAME
    with acquire_writer_lock(db):
        assert lock.exists()
    assert not lock.exists()


def test_second_writer_refused_when_first_live(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    lock = tmp_path / WRITER_LOCK_FILENAME
    lock.write_text(f"{os.getpid()}\n")  # impersonate a live foreign writer

    # We hold the lock with our own PID, so the next acquire from a
    # *fake* foreign PID should be refused. Simulate by writing a
    # different live PID — use the parent's PID; we're a child of it.
    lock.write_text(f"{os.getppid()}\n")
    with pytest.raises(WriterLockHeld):
        with acquire_writer_lock(db):
            pass


def test_stale_lock_reclaimed(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    lock = tmp_path / WRITER_LOCK_FILENAME
    db.parent.mkdir(parents=True, exist_ok=True)
    # PID 1 is init; will exist. Use a high improbable PID instead.
    lock.write_text("999999\n")
    assert not _is_pid_alive(999999)
    with acquire_writer_lock(db):
        # acquired successfully despite stale file
        assert lock.read_text().startswith(str(os.getpid()))
