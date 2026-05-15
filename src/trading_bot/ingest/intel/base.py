"""Intel feed protocol — every external source plugs in here."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol


class IntelUnavailable(RuntimeError):
    """Raised when a feed cannot return a value (network down, rate
    limit, parse error). The daemon translates this into a per-decision
    halt for the strategies that depend on the feed; **never** silently
    fall back to a stale value, since intel decay is the silent failure
    mode that erodes systematic-trading edges over time."""


@dataclass(frozen=True)
class IntelRecord:
    """A single intel value.

    ``value`` is unit-typed by the feed (e.g. percent for FRED yields,
    seconds-since-epoch for timestamps). The feed documents the unit;
    consumers carry the documented unit forward.

    ``source_ts`` is the publication / observation timestamp from the
    upstream source (e.g. FRED's last_updated). ``fetched_ts`` is
    when the daemon retrieved it. The pair lets the operator see stale
    data even when the daemon is healthy.

    ``source_hash`` is a sha256 prefix of (feed_id, value, source_ts).
    It anchors the snapshot row's hash chain without re-canonicalising
    payloads.
    """

    feed_id: str
    series_id: str
    value: float
    unit: str
    source_ts: str               # ISO-8601, source's own clock
    fetched_ts: str              # ISO-8601, daemon wall clock
    source_url: str | None = None

    @property
    def source_hash(self) -> str:
        body = f"{self.feed_id}|{self.series_id}|{self.value}|{self.source_ts}"
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_hash"] = self.source_hash
        return d


class IntelFeed(Protocol):
    """Pluggable feed. ``fetch`` returns a mapping of series_id -> record.

    Implementations are network-bound and time-out-bound. Network errors
    raise ``IntelUnavailable``. Successful returns are deterministic
    given the upstream state.
    """

    feed_id: str

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]: ...

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]: ...


class BaseIntelFeed:
    """Convenience base — implements ``query_features`` as a no-op so
    subclasses only need to override what they actually publish.

    Subclasses MUST set ``feed_id`` and implement ``fetch``. They may
    override ``query_features`` to expose per-symbol values (e.g. an
    insider-cluster score for SEC Form 4) used by v3 strategies via
    ``policy/strategy_signal_features_v1.json``.
    """

    feed_id: str = "base"

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        raise NotImplementedError

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        return {}


__all__ = ["BaseIntelFeed", "IntelFeed", "IntelRecord", "IntelUnavailable"]
