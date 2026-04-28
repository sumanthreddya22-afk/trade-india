"""Pause flag sentinel. If file exists, daemon must not place new orders."""
from __future__ import annotations

import datetime as dt
from pathlib import Path


def is_paused(path: str | Path) -> bool:
    return Path(path).exists()


def set_pause(path: str | Path, *, reason: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{reason}\n"
    p.write_text(payload)


def clear_pause(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)
