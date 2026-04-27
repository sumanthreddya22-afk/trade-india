"""Per-role-run JSON logging. One file per .event() / .error() call,
under runs/<YYYY-MM-DD>/<role>/<HH-MM-SS>.json. Multiple events at the
same wall-clock second get suffixed with a microsecond fragment.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import traceback
from pathlib import Path


_lock = threading.Lock()


def get_run_path(*, base: Path, date: dt.date, role: str, ts: dt.datetime) -> Path:
    fname = ts.strftime("%H-%M-%S") + ".json"
    return Path(base) / date.isoformat() / role / fname


class StructuredLogger:
    def __init__(self, *, base: str | Path = "runs", role: str):
        self.base = Path(base)
        self.role = role

    def _write(self, payload: dict) -> None:
        ts = dt.datetime.now(dt.timezone.utc)
        path = get_run_path(base=self.base, date=ts.date(), role=self.role, ts=ts)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve same-second collisions by appending microseconds.
        with _lock:
            target = path
            if target.exists():
                target = target.with_suffix(f".{ts.microsecond}.json")
            target.write_text(json.dumps(payload))

        # Also echo to stdout for launchd capture.
        print(json.dumps(payload), file=sys.stdout, flush=True)

    def event(self, name: str, **kwargs) -> None:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "role": self.role,
            "event": name,
            "level": "info",
            **kwargs,
        }
        self._write(payload)

    def error(self, name: str, *, error: Exception, **kwargs) -> None:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "role": self.role,
            "event": name,
            "level": "error",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc(),
            **kwargs,
        }
        self._write(payload)
