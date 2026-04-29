"""Heartbeat write/read. The daemon writes every 60s; the supervisor reads
mtime to detect stalls. Atomic via tmp+rename so a reader never observes
a half-written file. Tmp filename is per-writer-unique (pid + monotonic ns)
so concurrent writers from different scheduler jobs in the same daemon
process don't race to consume the same tmp.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid
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
    # Unique tmp name per writer — concurrent calls from different scheduler
    # jobs in the same daemon can't collide. The os.replace gives last-writer-
    # wins semantics on the final file (which is what we want for a heartbeat).
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except FileNotFoundError:
        # tmp got swept by another writer between write_text and replace
        # (very rare; tolerable — heartbeat will refresh on the next tick)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def read_heartbeat(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def is_stale(path: str | Path, *, max_age_seconds: int) -> bool:
    p = Path(path)
    if not p.exists():
        return True
    age = time.time() - p.stat().st_mtime
    return age > max_age_seconds
