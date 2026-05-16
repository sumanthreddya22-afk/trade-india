"""WS5b — shared rolling-window broker-call counter.

The ``detect_broker_api_error_rate`` detector in ``risk/kill_switches.py``
is pure logic. To make it useful end-to-end, every broker call
(``submit_order``, ``fetch_positions``, etc.) records success or error
into a shared rolling window via this module. A nightly /
per-minute cron job (``job_broker_api_error_rate_check`` in
``daemon/jobs.py``) calls ``summarize_window`` and feeds the detector.

Thread-safe; uses a deque keyed by monotonic timestamps. The default
window is 5 minutes (matching ``risk_policy.lock.kill_switches.
broker_api_error_rate_window_minutes``).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


DEFAULT_WINDOW_SECONDS = 5 * 60


@dataclass(frozen=True)
class BrokerCallSummary:
    total: int
    errors: int

    @property
    def error_rate_pct(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.errors / self.total * 100.0


class _Tracker:
    """Module-level singleton; not exposed publicly."""

    def __init__(self) -> None:
        self._events: deque[tuple[float, bool]] = deque()
        self._lock = threading.Lock()

    def record(self, *, error: bool, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        with self._lock:
            self._events.append((ts, error))

    def summarize(self, *, window_seconds: float, now: float | None = None) -> BrokerCallSummary:
        cutoff = (now if now is not None else time.monotonic()) - window_seconds
        with self._lock:
            # Prune anything older than the window.
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()
            total = len(self._events)
            errors = sum(1 for _, err in self._events if err)
        return BrokerCallSummary(total=total, errors=errors)

    def reset(self) -> None:
        """For tests."""
        with self._lock:
            self._events.clear()


_TRACKER = _Tracker()


def record_success(*, now: float | None = None) -> None:
    _TRACKER.record(error=False, now=now)


def record_error(*, now: float | None = None) -> None:
    _TRACKER.record(error=True, now=now)


def summarize_window(
    *, window_seconds: float = DEFAULT_WINDOW_SECONDS,
    now: float | None = None,
) -> BrokerCallSummary:
    return _TRACKER.summarize(window_seconds=window_seconds, now=now)


def reset() -> None:
    _TRACKER.reset()


__all__ = [
    "BrokerCallSummary",
    "DEFAULT_WINDOW_SECONDS",
    "record_error",
    "record_success",
    "reset",
    "summarize_window",
]
