"""Tests for the cross-process event bus (Phase 0).

Covers the pieces called out in the plan's Verification section:
* writer thread drains, never blocks emit, drops on full with counter
* process tag is stamped correctly
* subscriber tail returns rows in id order, respects start cursor
* per-client backpressure: drops oldest non-critical, never drops
  critical-whitelist events
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.event_bus import bus as bus_mod
from trading_bot.event_bus.bus import EventBus
from trading_bot.event_bus.subscriber import (
    Broadcaster,
    Event,
    fetch_since,
    get_max_event_id,
    _ClientQueue,
    _is_critical,
)


def _create_events_table(db_path: str) -> None:
    """Match the schema in migrations/versions/018_events_bus.py."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT '',
                process TEXT NOT NULL DEFAULT 'unknown',
                created_at DATETIME NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    p = str(tmp_path / "state.db")
    _create_events_table(p)
    return p


@pytest.fixture()
def fresh_bus(db: str):
    """Spin a fresh module-singleton bus pointed at the temp DB."""
    bus_mod.reset_bus_for_tests()
    bus_mod.set_process_tag("test")
    yield
    bus_mod.reset_bus_for_tests()


def _wait_for_rows(db: str, want: int, timeout: float = 2.0) -> int:
    """Poll until `events` has >= want rows or timeout. Returns observed count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if n >= want:
            return n
        time.sleep(0.02)
    return n


# ---------------------------------------------------------------------------
# bus.py
# ---------------------------------------------------------------------------
class TestEventBus:
    def test_writer_drains_to_sqlite_and_stamps_process(self, db: str) -> None:
        bus_mod.set_process_tag("daemon")
        bus = EventBus(db)
        bus.start()
        try:
            assert bus.emit("decision.created", {"symbol": "AAPL"}, source="orchestrator") is True
            assert bus.emit("order.filled", {"symbol": "MSFT", "qty": 50}) is True
            assert _wait_for_rows(db, 2) == 2
        finally:
            bus.stop()

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT type, payload, source, process FROM events ORDER BY id"
            ).fetchall()
        assert rows[0] == ("decision.created", '{"symbol":"AAPL"}', "orchestrator", "daemon")
        assert rows[1][0] == "order.filled"
        assert rows[1][3] == "daemon"
        assert json.loads(rows[1][1]) == {"symbol": "MSFT", "qty": 50}

    def test_emit_never_blocks_under_full_queue(self, db: str) -> None:
        # Tiny queue + writer that never starts means everything overflows.
        bus = EventBus(db)
        # Replace the queue with size 2 to force overflow quickly.
        import queue as _q
        bus._queue = _q.Queue(maxsize=2)
        # NOTE: don't start the writer — we want emit to fill the queue.

        # Time how long 200 emit() calls take. They should not block at all
        # (only the queue.put_nowait fast-path is exercised; nothing is
        # draining). Anything > 100ms means we're not non-blocking.
        t0 = time.monotonic()
        accepted = sum(1 for _ in range(200) if bus.emit("test.event", {"i": _}))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.2, f"emit() blocked for {elapsed:.3f}s — must be non-blocking"
        # First 2 went into the queue, rest dropped.
        assert accepted == 2
        stats = bus.stats()
        assert stats["events_emitted_total"] == 200
        assert stats["events_dropped_total"] == 198
        assert stats["events_written_total"] == 0

    def test_emit_serializes_unjsonable_payloads_safely(self, db: str) -> None:
        bus = EventBus(db)
        bus.start()
        try:
            # datetime serializes via default=str, set serializes... well, doesn't.
            # The bus should NOT raise — it falls back to "{}".
            class Weird:
                pass
            assert bus.emit("test.weird", {"obj": Weird()}) is True
            assert _wait_for_rows(db, 1) == 1
        finally:
            bus.stop()

    def test_get_bus_returns_singleton(self, db: str, fresh_bus) -> None:
        a = bus_mod.get_bus(db)
        b = bus_mod.get_bus(db)
        assert a is b

    def test_emit_module_helper_routes_through_singleton(self, db: str, fresh_bus) -> None:
        bus_mod.set_process_tag("lab")
        # First call seeds the singleton with our temp DB; subsequent
        # bus_mod.emit() goes to the same instance.
        bus_mod.get_bus(db)
        assert bus_mod.emit("intel.updated", {"n": 7}, source="ingestor") is True
        assert _wait_for_rows(db, 1) == 1
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT type, source, process FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row == ("intel.updated", "ingestor", "lab")


# ---------------------------------------------------------------------------
# subscriber.py
# ---------------------------------------------------------------------------
class TestFetchSince:
    def test_returns_events_in_id_order_after_cursor(self, db: str) -> None:
        bus = EventBus(db)
        bus.start()
        try:
            for i in range(5):
                bus.emit("scan.completed", {"i": i})
            _wait_for_rows(db, 5)
        finally:
            bus.stop()

        all_events = fetch_since(db, cursor=0, limit=100)
        assert [e.type for e in all_events] == ["scan.completed"] * 5
        assert [e.payload["i"] for e in all_events] == [0, 1, 2, 3, 4]
        # Cursor mid-stream returns only later rows.
        cut = all_events[2].id
        later = fetch_since(db, cursor=cut, limit=100)
        assert [e.payload["i"] for e in later] == [3, 4]

    def test_get_max_event_id(self, db: str) -> None:
        assert get_max_event_id(db) == 0
        bus = EventBus(db)
        bus.start()
        try:
            bus.emit("x", {})
            _wait_for_rows(db, 1)
        finally:
            bus.stop()
        assert get_max_event_id(db) >= 1


class TestCriticalGate:
    def test_classification(self) -> None:
        for t in ("order.placed", "order.filled", "trade.closed",
                  "debate.unblock.completed", "role.failed", "role.stalled"):
            assert _is_critical(t), t
        for t in ("decision.created", "scan.completed", "heartbeat.tick", "intel.updated"):
            assert not _is_critical(t), t


class TestClientQueueBackpressure:
    def test_drops_non_critical_when_full(self) -> None:
        async def _run() -> None:
            cq = _ClientQueue()
            for i in range(cq.q.maxsize):
                await cq.put(_synthetic("scan.completed", i))
            assert cq.q.full()
            await cq.put(_synthetic("scan.completed", 999))
            assert cq.dropped == 1
            await cq.put(_synthetic("order.filled", 1000))
            assert cq.dropped == 2
            contents = []
            while not cq.q.empty():
                contents.append(cq.q.get_nowait())
            assert any(e.type == "order.filled" for e in contents)

        asyncio.run(_run())

    def test_never_drops_critical_in_normal_path(self) -> None:
        async def _run() -> None:
            cq = _ClientQueue()
            for i in range(50):
                await cq.put(_synthetic("scan.completed", i))
            for i in range(5):
                await cq.put(_synthetic("order.filled", 1000 + i))
            contents = []
            while not cq.q.empty():
                contents.append(cq.q.get_nowait())
            critical_count = sum(1 for e in contents if _is_critical(e.type))
            assert critical_count == 5

        asyncio.run(_run())


def _synthetic(type_: str, i: int) -> Event:
    return Event(
        id=i, type=type_, payload={"i": i},
        source="test", process="test",
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Broadcaster — integration-level fan-out test.
# ---------------------------------------------------------------------------
class TestBroadcaster:
    def test_two_subscribers_receive_same_events(self, db: str) -> None:
        def insert_event(t: str, source: str = "test") -> None:
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO events (type, payload, source, process, created_at) "
                    "VALUES (?, '{}', ?, 'test', ?)",
                    (t, source, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()

        async def _run() -> None:
            b = Broadcaster(db, poll_interval=0.05)
            await b.start()
            try:
                cq1 = await b.subscribe()
                cq2 = await b.subscribe()
                insert_event("a.one")
                insert_event("a.two")
                received_1: list[str] = []
                received_2: list[str] = []

                async def collect(cq, into, n):
                    for _ in range(n):
                        ev = await asyncio.wait_for(cq.q.get(), timeout=2.0)
                        into.append(ev.type)

                await asyncio.gather(collect(cq1, received_1, 2),
                                     collect(cq2, received_2, 2))
                assert received_1 == ["a.one", "a.two"]
                assert received_2 == ["a.one", "a.two"]
                assert b.stats()["clients_connected"] == 2
            finally:
                await b.stop()

        asyncio.run(_run())

    def test_replay_for_client_only(self, db: str) -> None:
        with sqlite3.connect(db) as conn:
            for t in ("x.1", "x.2", "x.3"):
                conn.execute(
                    "INSERT INTO events (type, payload, source, process, created_at) "
                    "VALUES (?, '{}', '', 'test', ?)",
                    (t, datetime.now(timezone.utc).isoformat()),
                )
            conn.commit()

        async def _run() -> None:
            b = Broadcaster(db, poll_interval=0.05)
            await b.start()
            try:
                cq = await b.subscribe()
                await b.replay_for(cq, since_id=0)
                received = []
                for _ in range(3):
                    ev = await asyncio.wait_for(cq.q.get(), timeout=1.0)
                    received.append(ev.type)
                assert received == ["x.1", "x.2", "x.3"]
            finally:
                await b.stop()

        asyncio.run(_run())
