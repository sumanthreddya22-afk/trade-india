"""Daemon logging — rotating file handler so overnight logs don't bloat
the disk and a single read can show last-N-MB without paging.

Defaults (overridable via DaemonConfig):
  * Path: ``data/daemon.log`` (rotated to ``data/daemon.log.1`` … ``.5``)
  * Max bytes per file: 10 MB
  * Backup count: 5 (so the cap is ~60 MB total)
  * Format: ISO-8601 ts | level | logger | msg
  * Console mirror at INFO so ``bot daemon`` in foreground stays readable.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


DEFAULT_LOG_PATH = Path("data") / "daemon.log"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
DEFAULT_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(
    log_path: Path = DEFAULT_LOG_PATH,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    level: int = logging.INFO,
    also_console: bool = True,
) -> Path:
    """Configure root logger with a rotating file handler + optional
    console mirror. Returns the resolved log path.

    Idempotent: removes any existing handlers on root first, so a
    second call (e.g. test) replaces the config cleanly.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Remove existing handlers (avoid duplicate output during reload).
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        str(log_path), maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    if also_console:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)

    root.setLevel(level)

    # Tame noisy third-party libs so the daemon log stays signal-rich.
    for noisy in ("apscheduler", "urllib3", "asyncio", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_path


__all__ = [
    "DEFAULT_BACKUP_COUNT", "DEFAULT_LOG_PATH", "DEFAULT_MAX_BYTES",
    "setup_logging",
]
