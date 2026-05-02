"""Phase 4 — retention sweep + workers=1 startup guard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.daemon import _run_event_bus_retention


def _create_events_table(p: str) -> None:
    with sqlite3.connect(p) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', "
            "source TEXT NOT NULL DEFAULT '', "
            "process TEXT NOT NULL DEFAULT 'unknown', "
            "created_at DATETIME NOT NULL)"
        )
        conn.commit()


def _insert(db: str, type_: str, age_days: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events (type, payload, source, process, created_at) "
            "VALUES (?, '{}', '', 'test', ?)",
            (type_, ts),
        )
        conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    p = str(tmp_path / "state.db")
    _create_events_table(p)
    return p


class TestRetention:
    def test_keeps_recent_drops_old(self, db: str) -> None:
        _insert(db, "fresh.1", age_days=0.1)
        _insert(db, "fresh.2", age_days=2.0)
        _insert(db, "stale.1", age_days=8.0)
        _insert(db, "stale.2", age_days=14.0)

        log = MagicMock()
        _run_event_bus_retention(Path(db), log=log, max_age_days=7)

        with sqlite3.connect(db) as conn:
            types = sorted(t for (t,) in conn.execute("SELECT type FROM events"))
        assert types == ["fresh.1", "fresh.2"]
        log.event.assert_called_once()
        kwargs = log.event.call_args.kwargs
        assert kwargs.get("deleted") == 2

    def test_no_op_on_empty_table(self, db: str) -> None:
        log = MagicMock()
        _run_event_bus_retention(Path(db), log=log, max_age_days=7)
        log.event.assert_called_once()
        kwargs = log.event.call_args.kwargs
        assert kwargs.get("deleted") == 0


class TestWorkersGuard:
    def test_run_rejects_workers_gt_1(self) -> None:
        from trading_bot.dashboard.app import run
        with pytest.raises(RuntimeError, match="workers=1"):
            run(workers=2)
