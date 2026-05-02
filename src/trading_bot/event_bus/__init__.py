"""Cross-process event bus for the real-time dashboard.

Two halves:

* ``bus.EventBus`` — the producer side. Every launchd process gets one
  global instance; ``emit(type, payload)`` is non-blocking (bounded
  queue + dedicated writer thread that drains rows into the shared
  ``events`` SQLite table).
* ``subscriber`` — the dashboard-side consumer. Async ``tail()``
  generator polls ``id > cursor`` every 250ms and yields rows; the SSE
  endpoint fans them out to per-client queues.

Why SQLite is the IPC: all four trading-bot processes already share
``data/state.db`` in WAL mode, so adding one more append-only table is
free correctness across processes. Volume is low (hundreds/day);
high-rate price ticks go through a separate ephemeral in-process
channel inside the dashboard process — never SQLite.
"""
from trading_bot.event_bus.bus import EventBus, get_bus, emit, set_process_tag

__all__ = ["EventBus", "get_bus", "emit", "set_process_tag"]
