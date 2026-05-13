"""Shared fixtures for the Phase 1+ tests.

Provides an in-memory-like ledger fixture: a fresh SQLite file in a tmp
dir, with the v4 schema applied. Tests that need a writer connection
should depend on ``ledger_conn``; tests that want both ledger and mirror
should depend on ``ledger_pair``.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    """Path to a fresh ledger DB. The file does not exist until a writer
    creates it."""
    return tmp_path / "ledger.db"


@pytest.fixture()
def mirror_path(tmp_path: Path) -> Path:
    return tmp_path / "mirror.db"


@pytest.fixture()
def ledger_conn(ledger_path: Path):
    """Fresh ledger DB with the v4 schema applied. Caller gets a writer
    connection; closes on test teardown.
    """
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(ledger_path)
    create_ledger(conn)
    yield conn
    conn.close()


@pytest.fixture()
def ledger_pair(ledger_path: Path, mirror_path: Path):
    """Ledger + mirror, both initialised. Returns a tuple (ledger, mirror)
    of writer connections.
    """
    from trading_bot.ledger import connect_writer, create_ledger, init_mirror
    ledger = connect_writer(ledger_path)
    create_ledger(ledger)
    mirror = init_mirror(mirror_path)
    yield ledger, mirror
    ledger.close()
    mirror.close()
