"""Options-source collector framework — write_event + SourceResult + helpers.

Mirrors ``pipelines.crypto.sources._base`` shape so the existing
aggregator/scoring patterns port over with minimal change, but writes
to ``intel_events_options`` instead of ``intel_events_crypto``.

Per-pipeline isolation: this module never imports anything from the
stocks or crypto pipelines. It writes only to options-owned tables.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.state_db import IntelEventOptions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source weights — Phase 3 baseline (tunable later)
# ---------------------------------------------------------------------------

OPTIONS_SOURCE_WEIGHTS: Dict[str, float] = {
    # Tier-1 — primary structural signals
    "earnings_calendar":     5.0,   # binary catalyst — IV crush risk
    "unusual_options_flow":  4.0,   # smart-money flow signal
    # Tier-2 — secondary context
    "cboe_skew":             3.0,   # index-level vol regime context
    "iv_capture":            3.0,   # per-name IV history (already wired)
}


OPTIONS_DECAY_HOURS: Dict[str, float] = {
    "earnings_calendar":      72.0,  # earnings dates are stable
    "unusual_options_flow":   12.0,  # large-block signals fade fast
    "cboe_skew":              24.0,  # daily-update cadence
    "iv_capture":             24.0,  # daily-snapshot cadence
}


DEFAULT_SOURCE_WEIGHT = 1.0
DEFAULT_DECAY_HOURS = 24.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SourceResult:
    """Per-collector return value. ``error`` is set on failure but the
    collector still returns rather than raising so collect_all keeps going.
    """
    source: str
    written: int = 0
    skipped: int = 0
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "source": self.source,
            "written": self.written,
            "skipped": self.skipped,
        }
        if self.error:
            d["error"] = self.error
        if self.extra:
            d["extra"] = self.extra
        return d


# ---------------------------------------------------------------------------
# write_event — the only path options intel goes into the DB
# ---------------------------------------------------------------------------


def write_event(
    engine: Any,
    *,
    underlying: str,
    source: str,
    headline: str = "",
    url: str = "",
    sentiment: Optional[float] = None,
    raw_score: Optional[float] = None,
    event_at: Optional[dt.datetime] = None,
    event_hash: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Insert one ``IntelEventOptions`` row. Idempotent via the
    (underlying, source, event_hash) unique constraint — duplicates
    return False without raising.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if event_hash is None:
        h = hashlib.sha1()
        h.update(source.encode())
        h.update((url or headline or "").encode())
        event_hash = h.hexdigest()

    row = IntelEventOptions(
        underlying=underlying.upper(),
        source=source,
        headline=(headline or "")[:1000],
        url=(url or "")[:1000],
        sentiment=sentiment,
        raw_score=raw_score,
        event_at=event_at,
        ingested_at=now,
        event_hash=event_hash,
    )
    try:
        with Session(engine) as session:
            session.add(row)
            session.commit()
            return True
    except IntegrityError:
        return False


def stable_event_hash(*parts: str) -> str:
    """Compose a deterministic hash from arbitrary parts."""
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
