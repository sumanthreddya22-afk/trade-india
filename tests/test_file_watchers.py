"""Phase 3 — file-watcher tests.

Drives the watcher with a tiny poll interval against a temp dir and
asserts that emits land in SQLite. Cheaper than mocking — exercises
the same path the daemon uses.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from trading_bot.event_bus import bus as bus_mod
from trading_bot.streams.file_watchers import (
    FileWatcherRunner,
    _DirNewfileWatch,
    _FileMtimeWatch,
)


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


@pytest.fixture()
def db(tmp_path: Path) -> str:
    p = str(tmp_path / "state.db")
    _create_events_table(p)
    return p


@pytest.fixture()
def bus(db: str):
    bus_mod.reset_bus_for_tests()
    bus_mod.set_process_tag("daemon")
    bus_mod.get_bus(db)
    yield
    bus_mod.reset_bus_for_tests()


def _wait_rows(db: str, where: str = "1=1", n: int = 1, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(db) as conn:
            got = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}").fetchone()[0]
        if got >= n:
            return got
        time.sleep(0.05)
    return got


class TestFileMtimeWatch:
    def test_emits_on_first_change(self, tmp_path: Path, db: str, bus) -> None:
        target = tmp_path / "last_scan.json"
        target.write_text(json.dumps({"command": "intel", "decisions": [{"action": "BUY"}]}))

        runner = FileWatcherRunner(poll_interval=0.05)
        runner.watch_file(_FileMtimeWatch(
            path=target, event_type="scan.completed", source="t",
            extract=lambda p: {"got": True},
        ))
        runner.start()
        try:
            # Modify the file. Sleep so mtime cleanly differs even on
            # filesystems with 1s resolution.
            time.sleep(1.1)
            target.write_text(json.dumps({"command": "intel", "decisions": [
                {"action": "BUY"}, {"action": "SELL"},
            ]}))
            assert _wait_rows(db, "type='scan.completed'", n=1) == 1
        finally:
            runner.stop()

    def test_does_not_emit_on_boot_for_existing_files(self, tmp_path: Path, db: str, bus) -> None:
        target = tmp_path / "scout.json"
        target.write_text("[]")
        runner = FileWatcherRunner(poll_interval=0.05)
        runner.watch_file(_FileMtimeWatch(
            path=target, event_type="scout.completed", source="t",
        ))
        runner.start()
        try:
            time.sleep(0.3)  # let several poll cycles elapse
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type='scout.completed'"
                ).fetchone()[0]
            assert rows == 0
        finally:
            runner.stop()


class TestDirNewfileWatch:
    def test_emits_per_new_file(self, tmp_path: Path, db: str, bus) -> None:
        done_dir = tmp_path / "done"
        done_dir.mkdir()
        runner = FileWatcherRunner(poll_interval=0.05)
        runner.watch_dir(_DirNewfileWatch(
            root=done_dir, glob="*.json",
            event_type="mailbox.brief.completed", source="t",
            extract=lambda p: {"brief_id": p.stem},
        ))
        runner.start()
        try:
            (done_dir / "abc.json").write_text("{}")
            (done_dir / "def.json").write_text("{}")
            assert _wait_rows(db, "type='mailbox.brief.completed'", n=2) == 2
        finally:
            runner.stop()

    def test_priming_skips_preexisting_files(self, tmp_path: Path, db: str, bus) -> None:
        done_dir = tmp_path / "done"
        done_dir.mkdir()
        (done_dir / "before-boot.json").write_text("{}")
        runner = FileWatcherRunner(poll_interval=0.05)
        runner.watch_dir(_DirNewfileWatch(
            root=done_dir, glob="*.json",
            event_type="mailbox.brief.completed", source="t",
        ))
        runner.start()
        try:
            time.sleep(0.3)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type='mailbox.brief.completed'"
                ).fetchone()[0]
            assert rows == 0
            # New file after start does emit.
            (done_dir / "new-one.json").write_text("{}")
            assert _wait_rows(db, "type='mailbox.brief.completed'", n=1) == 1
        finally:
            runner.stop()


class TestStartupGuards:
    def test_disabled_via_env(self, monkeypatch) -> None:
        from trading_bot.streams.file_watchers import maybe_start
        monkeypatch.setenv("TRADING_BOT_FILE_WATCHERS_DISABLED", "1")
        assert maybe_start() is None

    def test_starts_when_paths_missing(self, tmp_path: Path, db: str, bus) -> None:
        # All target paths missing — runner should still start cleanly,
        # then emit once any of them appears.
        from trading_bot.streams.file_watchers import maybe_start
        # Point all paths at non-existent locations under tmp_path; runner
        # primes empty `seen` sets and waits.
        runner = maybe_start(
            last_scan=str(tmp_path / "missing.json"),
            opportunities_md=str(tmp_path / "missing.md"),
            scout_json=str(tmp_path / "missing-scout.json"),
            llm_done_dir=str(tmp_path / "no-such-done"),
            runs_dir=str(tmp_path / "no-such-runs"),
        )
        assert runner is not None
        try:
            time.sleep(0.2)
            # Now create scout.json — should emit.
            (tmp_path / "missing-scout.json").write_text("[]")
            time.sleep(2.0)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE type='scout.completed'"
                ).fetchone()[0]
            assert rows >= 1
        finally:
            runner.stop()
