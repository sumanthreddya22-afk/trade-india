"""Consumer side of the event bus — used by the dashboard SSE endpoint.

The ``tail`` async generator polls the events table by ``id > cursor``
every 250ms and yields rows. Per-client ``asyncio.Queue`` instances
managed by ``Broadcaster`` fan out to every connected SSE client.

Why a dedicated broadcaster instead of one tail per client: we don't
want N clients all polling SQLite with their own cursor. One tail
loop, N queues. New events go into all queues at once; slow clients
just lag behind their own queue, never the database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger(__name__)

# Cadence of the tail-loop's polling. 250ms is the worst-case end-to-end
# latency between an event landing in SQLite and the SSE client seeing
# it (in practice usually 0–250ms; producer-write to client-read is
# typically <500ms total).
_TAIL_POLL_S = 0.25

# Per-client SSE queue size. Slow tabs (devtools open, throttled) lag.
# 200 is enough for a few seconds of bursty traffic; beyond that we
# drop oldest non-critical events with a counter.
_CLIENT_QUEUE_MAXSIZE = 200

# Events that must NEVER be dropped under backpressure — they carry
# load-bearing trading state. If a client's queue is full and one of
# these arrives, we drop oldest non-critical instead.
_CRITICAL_PREFIXES = (
    "order.",
    "trade.closed",
    "debate.unblock.completed",
    "role.failed",
    "role.stalled",
)


@dataclass
class Event:
    """A row from the events table, in flight to an SSE client."""
    id: int
    type: str
    payload: dict[str, Any]
    source: str
    process: str
    created_at: datetime

    def to_sse_line(self) -> str:
        """Format for the wire. Each event is `id` (durable only),
        `event` (name), `data` (JSON) — plus a blank line.

        Ephemeral events (id == 0) are sent WITHOUT an ``id:`` line so
        they never affect the browser's ``Last-Event-ID`` cursor. This
        is how Phase 8 price ticks ride the same SSE channel without
        polluting the resume semantics.
        """
        body = {
            "id": self.id,
            "type": self.type,
            "payload": self.payload,
            "source": self.source,
            "process": self.process,
            "created_at": self.created_at.isoformat(),
        }
        data_line = f"data: {json.dumps(body, default=str, separators=(',', ':'))}"
        if self.id and self.id > 0:
            return f"id: {self.id}\nevent: {self.type}\n{data_line}\n\n"
        return f"event: {self.type}\n{data_line}\n\n"


def _is_critical(type_: str) -> bool:
    return any(type_ == p or type_.startswith(p) for p in _CRITICAL_PREFIXES)


def _row_to_event(row: tuple) -> Event:
    rid, type_, payload_str, source, process, created_at_str = row
    try:
        payload = json.loads(payload_str) if payload_str else {}
    except Exception:
        payload = {}
    if isinstance(created_at_str, str):
        try:
            created_at = datetime.fromisoformat(created_at_str)
        except Exception:
            created_at = datetime.now(timezone.utc)
    elif isinstance(created_at_str, datetime):
        created_at = created_at_str
    else:
        created_at = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return Event(id=rid, type=type_, payload=payload, source=source or "",
                 process=process or "unknown", created_at=created_at)


@contextmanager
def _open_ro(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open SQLite in read-only mode — won't create the file if missing."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_max_event_id(db_path: str | Path = "data/state.db") -> int:
    """Latest event id, or 0 if the table is empty / missing.

    Used by the dashboard at startup to set the initial cursor — we want
    new clients to start from "now" by default, with replay only when
    they reconnect with a Last-Event-ID header.
    """
    try:
        with _open_ro(str(db_path)) as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def fetch_since(db_path: str | Path, cursor: int, limit: int = 200) -> list[Event]:
    """Block-fetch events with id > cursor, oldest first."""
    try:
        with _open_ro(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, type, payload, source, process, created_at "
                "FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
    except Exception:
        # Read failures are transient (table missing on first boot,
        # WAL contention). Return empty so the caller's poll loop
        # retries on its next tick.
        return []
    return [_row_to_event(r) for r in rows]


# ---------------------------------------------------------------------------
# Broadcaster — single tail loop, fan out to per-client queues.
# ---------------------------------------------------------------------------
class _ClientQueue:
    """Per-client async queue with severity-aware drop policy."""

    def __init__(self) -> None:
        self.q: "asyncio.Queue[Event]" = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAXSIZE)
        self.dropped = 0

    async def put(self, ev: Event) -> None:
        # Always try fast-path first.
        try:
            self.q.put_nowait(ev)
            return
        except asyncio.QueueFull:
            pass
        if _is_critical(ev.type):
            # Drop oldest non-critical to make room. Walk the queue and
            # rebuild without the first non-critical entry.
            self._make_room_for_critical(ev)
            try:
                self.q.put_nowait(ev)
            except asyncio.QueueFull:
                self.dropped += 1
        else:
            # Non-critical: drop this incoming event.
            self.dropped += 1

    def _make_room_for_critical(self, incoming: Event) -> None:
        # Pull all items into a list, drop the first non-critical, push
        # the rest back. O(n) per drop but n <= queue size and this
        # only fires under sustained backpressure.
        buf: list[Event] = []
        while not self.q.empty():
            try:
                buf.append(self.q.get_nowait())
            except asyncio.QueueEmpty:
                break
        dropped_one = False
        for item in buf:
            if (not dropped_one) and (not _is_critical(item.type)):
                dropped_one = True
                self.dropped += 1
                continue
            try:
                self.q.put_nowait(item)
            except asyncio.QueueFull:
                self.dropped += 1
        if not dropped_one:
            # Every item is critical — shouldn't happen under normal
            # load. Drop the *oldest* critical to preserve
            # liveness; ordering still preserved for newer events.
            try:
                _ = self.q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass


class Broadcaster:
    """Single tail loop, N client queues. Lifecycle bound to the dashboard
    process; started in app startup, stopped on shutdown."""

    def __init__(
        self,
        db_path: str | Path = "data/state.db",
        *,
        poll_interval: float = _TAIL_POLL_S,
    ) -> None:
        self._db_path = str(db_path)
        self._poll_interval = poll_interval
        self._clients: set[_ClientQueue] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = False
        # Cursor starts at the current max so newly-connecting clients
        # don't get a flood of historical events. Reconnects can request
        # earlier ids via Last-Event-ID.
        self._cursor = get_max_event_id(self._db_path)
        self._max_seen_lag_ms = 0.0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._tail_loop(), name="event-bus-broadcaster")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def subscribe(self) -> _ClientQueue:
        cq = _ClientQueue()
        async with self._lock:
            self._clients.add(cq)
        return cq

    async def unsubscribe(self, cq: _ClientQueue) -> None:
        async with self._lock:
            self._clients.discard(cq)

    async def broadcast(self, ev: Event) -> None:
        """Inject an event directly into every connected client's queue
        without persisting to SQLite.

        Phase 8 path: market-data ticks are high-rate and ephemeral, so
        they bypass the durable bus entirely. Browser still sees them as
        named SSE events but they don't affect Last-Event-ID resume.
        """
        async with self._lock:
            clients = list(self._clients)
        for cq in clients:
            await cq.put(ev)

    def broadcast_threadsafe(self, ev: Event, loop: asyncio.AbstractEventLoop) -> None:
        """Schedule a ``broadcast`` from a non-event-loop thread.

        Alpaca's ``StockDataStream`` runs its handler on its own asyncio
        loop in a worker thread. Calling ``asyncio.Queue.put`` from
        there is unsafe; this helper bridges via ``call_soon_threadsafe``.
        """
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(ev), loop)
        except RuntimeError:
            # Loop closed during shutdown — drop silently.
            pass

    async def replay_for(self, cq: _ClientQueue, since_id: int) -> int:
        """Push events with id > since_id directly into a single client's
        queue. Returns the highest id sent. Used on reconnect to honor
        Last-Event-ID without polluting other clients' streams."""
        events = fetch_since(self._db_path, since_id, limit=500)
        last = since_id
        for ev in events:
            await cq.put(ev)
            last = ev.id
        return last

    def stats(self) -> dict[str, Any]:
        return {
            "clients_connected": len(self._clients),
            "cursor": self._cursor,
            "lag_ms_p99": int(self._max_seen_lag_ms),
            "dropped_per_client": [c.dropped for c in self._clients],
        }

    async def _tail_loop(self) -> None:
        while not self._stop:
            try:
                t0 = time.monotonic()
                events = await asyncio.to_thread(
                    fetch_since, self._db_path, self._cursor, 500,
                )
                if events:
                    self._cursor = events[-1].id
                    # Snapshot client set so we don't hold the lock
                    # while awaiting on slow clients.
                    async with self._lock:
                        clients = list(self._clients)
                    for ev in events:
                        for cq in clients:
                            await cq.put(ev)
                lag_ms = (time.monotonic() - t0) * 1000.0
                # Track a lightweight running max — proper p99 needs a
                # sliding window; this is good enough for a health gauge.
                if lag_ms > self._max_seen_lag_ms:
                    self._max_seen_lag_ms = lag_ms
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("broadcaster tail loop error (will retry)")
            await asyncio.sleep(self._poll_interval)


# ---------------------------------------------------------------------------
# Async tail (without broadcaster) — used by tests and ad-hoc tools.
# ---------------------------------------------------------------------------
async def tail(
    db_path: str | Path = "data/state.db",
    start_id: int = 0,
    poll_interval: float = _TAIL_POLL_S,
    type_prefix: str | None = None,
) -> AsyncIterator[Event]:
    """Yield events with id > start_id forever. Optional prefix filter."""
    cursor = start_id
    while True:
        events = await asyncio.to_thread(fetch_since, db_path, cursor, 500)
        if events:
            for ev in events:
                if type_prefix and not ev.type.startswith(type_prefix):
                    continue
                yield ev
            cursor = events[-1].id
        await asyncio.sleep(poll_interval)
