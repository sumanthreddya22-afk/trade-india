"""Producer side of the event bus.

Design constraints:

* ``emit()`` MUST NOT block a producer thread or an SDK callback. The
  Alpaca trade stream callback runs on the SDK's thread; if we ``await``
  SQLite there a single WAL checkpoint can stall a fill for hundreds of
  milliseconds. So ``emit()`` is a ``put_nowait()`` onto a bounded
  in-memory queue — drop with a counter on full, never wait.
* A single dedicated writer thread drains the queue in batches and
  inserts rows into the shared ``events`` table. Batched inserts are an
  order of magnitude cheaper than per-row ones and they let many
  producers feed one writer.
* Per-process singleton. Each launchd process (daemon / lab /
  supervisor / mailbox / dashboard) imports the bus and gets its own
  ``EventBus`` instance. They all write to the same SQLite file.
* No new infra deps. SQLite is already shared in WAL mode.

Process tag: ``set_process_tag("daemon")`` should be called once at
process startup so each row records which process emitted it. Default
is the value of ``TRADING_BOT_PROCESS`` env var, then ``"unknown"``.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# How many rows the writer thread tries to drain per batch insert. Higher
# = fewer round-trips to SQLite under load; lower = lower per-event
# latency. 50 was picked empirically as a sweet spot for a single-writer
# WAL workload — small enough that p99 lag stays sub-second, large
# enough that bursty producers don't multiply round-trips.
_BATCH_SIZE = 50

# Bounded producer queue. If a producer outpaces the writer thread for
# longer than this many events, we start dropping with a counter. 1000
# is roughly an hour of normal traffic — generous headroom but bounded.
_QUEUE_MAXSIZE = 1000

# Periodic checkpoint cadence. WAL files grow without bound otherwise;
# the daemon process is the only one that runs the checkpoint to avoid
# multiple writers racing for the checkpoint lock.
_CHECKPOINT_INTERVAL_S = 30 * 60  # 30 min

# Throttle dropped-event log lines so a slow consumer doesn't drown the
# log. We still increment the metric on every drop.
_DROP_LOG_INTERVAL_S = 60


_GLOBAL_BUS_LOCK = threading.Lock()
_GLOBAL_BUS: "EventBus | None" = None
_PROCESS_TAG: str = os.environ.get("TRADING_BOT_PROCESS", "unknown")


def set_process_tag(tag: str) -> None:
    """Stamp this process's emissions with a tag (daemon / lab / supervisor /
    mailbox / dashboard). Call once at startup before the first ``emit()``."""
    global _PROCESS_TAG
    _PROCESS_TAG = tag


def get_process_tag() -> str:
    return _PROCESS_TAG


class EventBus:
    """Producer-side singleton. Owns the bounded queue + writer thread."""

    def __init__(
        self,
        db_path: str | Path = "data/state.db",
        *,
        run_checkpoint: bool = False,
    ) -> None:
        self._db_path = str(db_path)
        self._queue: "queue.Queue[tuple[str, str, str, str, datetime] | None]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_checkpoint = run_checkpoint

        # Counters — read by /api/stream/health.
        self._lock = threading.Lock()
        self.events_emitted_total = 0
        self.events_dropped_total = 0
        self.events_written_total = 0
        self._last_drop_log_ts: float = 0.0
        self._last_checkpoint_ts: float = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._run_writer,
            name="event-bus-writer",
            daemon=True,
        )
        t.start()
        self._thread = t

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # Sentinel unblocks writer if it's parked on queue.get().
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------
    def emit(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        source: str = "",
    ) -> bool:
        """Enqueue an event. Returns True if accepted, False if dropped.

        NEVER blocks. NEVER raises. Safe from any thread, including SDK
        callbacks. JSON-serializes the payload eagerly so producers don't
        share mutable references with the writer thread.
        """
        try:
            payload_json = json.dumps(payload or {}, default=str, separators=(",", ":"))
        except Exception:  # pragma: no cover — defensive
            payload_json = "{}"
        row = (
            str(type),
            payload_json,
            str(source or ""),
            _PROCESS_TAG,
            datetime.now(timezone.utc),
        )
        with self._lock:
            self.events_emitted_total += 1
        try:
            self._queue.put_nowait(row)
            return True
        except queue.Full:
            with self._lock:
                self.events_dropped_total += 1
            self._maybe_log_drop()
            return False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "events_emitted_total": self.events_emitted_total,
                "events_dropped_total": self.events_dropped_total,
                "events_written_total": self.events_written_total,
                "queue_depth": self._queue.qsize(),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _maybe_log_drop(self) -> None:
        now = time.monotonic()
        if (now - self._last_drop_log_ts) < _DROP_LOG_INTERVAL_S:
            return
        self._last_drop_log_ts = now
        logger.warning(
            "event_bus: dropped event (queue full); total dropped=%d emitted=%d",
            self.events_dropped_total,
            self.events_emitted_total,
        )

    def _open_conn(self) -> sqlite3.Connection:
        # Check_same_thread=False because the writer is single-threaded
        # but we need to handle a possible reconnect path. Timeout
        # tolerates concurrent writers from other processes.
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            timeout=30.0,
            check_same_thread=False,
        )
        # Don't override journal_mode if other connections set WAL — but
        # set the safe defaults for our writer.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _run_writer(self) -> None:
        try:
            conn = self._open_conn()
        except Exception:  # pragma: no cover — defensive; SQLite open shouldn't fail
            logger.exception("event_bus: failed to open SQLite for writer; bus will drop all events")
            return
        try:
            while not self._stop.is_set():
                batch = self._drain_batch()
                if batch:
                    try:
                        self._write_batch(conn, batch)
                    except Exception:
                        # On a transient WAL contention, retry once after a
                        # short sleep; if still failing, drop the batch and
                        # keep going so the bus never wedges.
                        logger.exception("event_bus: write failed; retrying once")
                        time.sleep(0.1)
                        try:
                            self._write_batch(conn, batch)
                        except Exception:
                            logger.exception("event_bus: write failed after retry; dropping batch of %d", len(batch))
                if self._run_checkpoint:
                    self._maybe_checkpoint(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _drain_batch(self) -> list[tuple]:
        batch: list[tuple] = []
        try:
            # Block until at least one item arrives or we're asked to stop.
            first = self._queue.get(timeout=1.0)
        except queue.Empty:
            return batch
        if first is None:
            return batch  # sentinel
        batch.append(first)
        # Greedily drain whatever else is already enqueued.
        while len(batch) < _BATCH_SIZE:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                break
            batch.append(item)
        return batch

    def _write_batch(self, conn: sqlite3.Connection, batch: list[tuple]) -> None:
        conn.executemany(
            "INSERT INTO events (type, payload, source, process, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            batch,
        )
        with self._lock:
            self.events_written_total += len(batch)

    def _maybe_checkpoint(self, conn: sqlite3.Connection) -> None:
        now = time.monotonic()
        if (now - self._last_checkpoint_ts) < _CHECKPOINT_INTERVAL_S:
            return
        self._last_checkpoint_ts = now
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.exception("event_bus: WAL checkpoint failed (non-fatal)")


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
def get_bus(
    db_path: str | Path | None = None,
    *,
    run_checkpoint: bool | None = None,
) -> EventBus:
    """Return the per-process singleton bus, creating it on first call.

    The first caller's args win; subsequent calls ignore them. The daemon
    process should pass ``run_checkpoint=True`` so it owns the periodic
    WAL truncate; other processes leave it False.
    """
    global _GLOBAL_BUS
    with _GLOBAL_BUS_LOCK:
        if _GLOBAL_BUS is None:
            path = db_path or os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
            checkpoint = run_checkpoint if run_checkpoint is not None else (_PROCESS_TAG == "daemon")
            bus = EventBus(path, run_checkpoint=checkpoint)
            bus.start()
            _GLOBAL_BUS = bus
        return _GLOBAL_BUS


def emit(type: str, payload: dict[str, Any] | None = None, source: str = "") -> bool:
    """Emit on the per-process singleton bus.

    Convenience wrapper — equivalent to ``get_bus().emit(...)``. Use this
    everywhere except in tests, where you'll instantiate ``EventBus``
    directly against a temp DB.
    """
    return get_bus().emit(type, payload, source)


def reset_bus_for_tests() -> None:
    """Tear down the global bus so tests can spin a fresh one. Pytest only."""
    global _GLOBAL_BUS
    with _GLOBAL_BUS_LOCK:
        if _GLOBAL_BUS is not None:
            _GLOBAL_BUS.stop()
            _GLOBAL_BUS = None
