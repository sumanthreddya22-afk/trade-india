"""v4 daemon — the long-lived process that ticks the kernel.

Plan v4 §6 + §10: a single supervised process that:
  * runs boot checks on startup (refuses to start on hash mismatch),
  * schedules the recurring jobs (snapshot, reconciliation, orphan loop,
    drift monitor, mutation cycle, market data ingest, watermark sweep),
  * exits cleanly on SIGTERM so launchd / systemd can restart it.

The daemon is a *scheduler*, not a decision-maker. Every job it calls
already exists in the kernel / execution / research layers; this module
only wires them to clocks and a shared sqlite connection.
"""
from __future__ import annotations

from trading_bot.daemon.scheduler import (
    DaemonConfig,
    build_scheduler,
    run_daemon,
)

__all__ = ["DaemonConfig", "build_scheduler", "run_daemon"]
