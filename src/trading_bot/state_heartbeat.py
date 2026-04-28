"""Heartbeat write/read. The daemon writes every 60s; the supervisor reads
mtime to detect stalls. Atomic via tmp+rename so a reader never observes
a half-written file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path


def write_heartbeat(path: str | Path, *, version: str, last_action: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": os.getpid(),
        "version": version,
        "last_action": last_action,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def read_heartbeat(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def is_stale(path: str | Path, *, max_age_seconds: int) -> bool:
    p = Path(path)
    if not p.exists():
        return True
    age = time.time() - p.stat().st_mtime
    return age > max_age_seconds
