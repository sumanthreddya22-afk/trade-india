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
from trading_bot.ingest.intel.cryptopanic import CryptoPanicFeed
from trading_bot.ingest.intel.edgar import EdgarFeed
from trading_bot.ingest.intel.fred import FredFeed


def snapshot_payload(
    feeds: "list[IntelFeed]",
    decision_date,
) -> dict:
    """Run every feed and return a single intel_json payload suitable
    for ``feature_snapshot.intel_json``.

    Failure semantics: each feed runs independently. A feed that raises
    ``IntelUnavailable`` contributes an ``{"_error": <reason>}`` entry
    keyed by feed_id rather than aborting the whole snapshot — the
    downstream snapshot then advertises explicitly which feeds were
    unhealthy at decision time. **Never** silently drop a failing feed,
    since intel decay is the silent failure mode the kernel can't see.
    """
    out: dict = {}
    for feed in feeds:
        try:
            records = feed.fetch(decision_date)
        except IntelUnavailable as e:
            out[feed.feed_id] = {"_error": str(e)}
            continue
        out[feed.feed_id] = {
            sid: rec.to_payload() for sid, rec in records.items()
        }
    return out


__all__ = [
    "CryptoPanicFeed",
    "EdgarFeed",
    "FredFeed",
    "IntelFeed",
    "IntelRecord",
    "IntelUnavailable",
    "snapshot_payload",
]
