"""SQLite connection factory + single-writer guard.

Plan v4 §5 immutability defense #1: "a single-writer process model — only
the kernel daemon holds the WAL writer; all other tools open read-only."

For Phase 1 the ``acquire_writer_lock`` helper enforces a PID lock file at
``<db_dir>/.writer.lock``. The kernel daemon (lands Phase 5+) will hold
the lock; everything else opens read-only.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LEDGER_PATH = Path("data/ledger/ledger.db")
DEFAULT_MIRROR_PATH = Path("data/ledger/mirror.db")
WRITER_LOCK_FILENAME = ".writer.lock"


class WriterLockHeld(Exception):
    """Raised when ``acquire_writer_lock`` finds a foreign live PID
    already holding the lock."""


def _is_pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is currently a live process on the host.

    Posix-only: ``os.kill(pid, 0)`` signals nothing but raises if the pid
    does not exist or we lack permission.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


@contextmanager
def acquire_writer_lock(db_path: Path):
    """Acquire the writer PID lock for the directory of ``db_path``.

    Yields the resolved lock file path. Raises ``WriterLockHeld`` if a
    foreign live PID already holds the lock. Cleans up on exit.

    Stale lock files (PID no longer alive) are reclaimed automatically.
    """
    db_dir = Path(db_path).resolve().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    lock = db_dir / WRITER_LOCK_FILENAME

    if lock.exists():
        try:
            existing = int(lock.read_text().strip().split()[0])
        except Exception:
            existing = -1
        if existing != os.getpid() and _is_pid_alive(existing):
            raise WriterLockHeld(
                f"{lock} held by pid={existing} (live); refusing to write"
            )
        # Stale — overwrite below.

    lock.write_text(f"{os.getpid()}\n")
    try:
        yield lock
    finally:
        try:
            if lock.exists():
                content = lock.read_text().strip().split()[0]
                if content == str(os.getpid()):
                    lock.unlink()
        except Exception:
            pass


def connect_writer(db_path: Path = DEFAULT_LEDGER_PATH) -> sqlite3.Connection:
    """Open a writer connection with WAL + foreign keys + busy_timeout.

    Caller is responsible for holding ``acquire_writer_lock``. Outside
    tests this is the kernel daemon's job.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def connect_reader(db_path: Path = DEFAULT_LEDGER_PATH) -> sqlite3.Connection:
    """Open a read-only connection. Safe to run concurrently with the writer
    under WAL. Used by the dashboard, the CLI, and tests that only assert.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"ledger not initialised at {db_path}; run tools/init_ledger.py"
        )
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=ON;")
    return conn


__all__ = [
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_MIRROR_PATH",
    "WRITER_LOCK_FILENAME",
    "WriterLockHeld",
    "acquire_writer_lock",
    "connect_reader",
    "connect_writer",
]
