"""Intel feeds — external information sources consumed by the
research factory and snapshotted into ``feature_snapshot``.

Each module exposes one or more ``IntelFeed`` objects: pure-data
adapters that fetch a value (or set of values) and return an
``IntelRecord`` tagged with the source, freshness, and a source
hash. The kernel never reads from these directly; the daemon
snapshots them into ``feature_snapshot`` at decision time, and the
strategy reads from the snapshot.

This separation enforces:

  - **No hot-path network call.** A snapshot is one-shot per decision.
  - **Reproducibility.** A backtest replays the snapshot, not the live
    feed.
  - **Source attribution.** Every numeric in a postmortem points at a
    feed and a fetch timestamp.
"""
from __future__ import annotations

from trading_bot.ingest.intel.base import (
    IntelFeed,
    IntelRecord,
    IntelUnavailable,
)
from trading_bot.ingest.intel.fred import FredFeed

__all__ = [
    "FredFeed",
    "IntelFeed",
    "IntelRecord",
    "IntelUnavailable",
]
