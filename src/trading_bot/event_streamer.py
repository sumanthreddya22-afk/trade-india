"""Phase G — Event-Driven Ingestion: fast-poll streaming + express triggers.

Fast-poll loop (configurable cadence, default 30s) for high-tier sources
that benefit from sub-tick latency:
  - SEC EDGAR (free polling endpoint; no actual websocket but the cadence
    is tight enough that it behaves like streaming for our purposes)
  - Polygon Benzinga (requires Partners tier; gated by `polygon_api_key`)
  - VIP Twitter / Truth Social (existing RSS poll, accelerated)

Each new event:
  1. Insert into ``intel_stream_events`` (deduped via UNIQUE on
     (source, event_hash)).
  2. For high-tier sources (sec_8k, vip_tweet with severity=high), call
     the per-symbol micro roll-up so the candidate row reflects the new
     event immediately.
  3. Dispatch express scout debate (if symbol is a new high-score
     candidate) OR express hold debate (if symbol is a held position).
  4. Mark ``processed_at`` to avoid double-handling.

Sequential: events processed in ingest order; one event's full handler
chain completes before the next starts. No parallelism inside or across
events.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import desc as _desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_bot.state_db import IntelStreamEvent, IntelEvent


log = logging.getLogger(__name__)


# Sources that get express-lane handling (immediate scout/hold trigger
# without waiting for the next ingestor tick).
EXPRESS_SOURCES = ("sec_8k", "vip_tweet")


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


def _make_event_hash(source: str, url: str, headline: str) -> str:
    h = hashlib.sha1()
    h.update(source.encode())
    h.update((url or headline or "").encode())
    return h.hexdigest()


def ingest_stream_event(
    engine,
    *,
    symbol: str,
    asset_class: str,
    source: str,
    headline: str = "",
    url: str = "",
    sentiment: float | None = None,
    event_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Insert a stream event. Idempotent via (source, event_hash) unique
    index. Returns True on insert, False on duplicate.

    Also writes a parallel row to ``intel_events`` so downstream rollup
    consumers see the event in the regular table — keeps Phase A-F
    logic untouched.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    event_hash = _make_event_hash(source, url, headline)
    inserted = False
    try:
        with Session(engine) as session:
            session.add(IntelStreamEvent(
                symbol=symbol.upper(), asset_class=asset_class,
                source=source,
                headline=(headline or "")[:1000],
                url=(url or "")[:1000],
                sentiment=sentiment,
                event_at=event_at,
                ingested_at=now,
                event_hash=event_hash,
            ))
            session.commit()
            inserted = True
    except IntegrityError:
        return False

    # Mirror to intel_events so Phase A-F consumers see the event.
    try:
        from trading_bot.intel.aggregator import write_event
        write_event(
            engine, symbol=symbol, asset_class=asset_class, source=source,
            headline=headline, url=url, sentiment=sentiment,
            event_at=event_at, event_hash=event_hash, now=now,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("event_streamer: mirror write_event failed: %s", e)
    return inserted


# ---------------------------------------------------------------------------
# Unprocessed event lookup
# ---------------------------------------------------------------------------


def unprocessed_events(
    engine,
    *,
    sources: Iterable[str] | None = None,
    limit: int = 50,
) -> list[IntelStreamEvent]:
    """Return rows where ``processed_at IS NULL``. Optionally filter by
    source list (e.g., EXPRESS_SOURCES for the express handler).
    """
    sources = list(sources) if sources is not None else None
    with Session(engine) as session:
        q = (
            session.query(IntelStreamEvent)
            .filter(IntelStreamEvent.processed_at.is_(None))
        )
        if sources:
            q = q.filter(IntelStreamEvent.source.in_(sources))
        rows = q.order_by(IntelStreamEvent.ingested_at).limit(limit).all()
        for r in rows:
            session.expunge(r)
    return rows


def mark_processed(
    engine, *, ids: list[int], now: dt.datetime | None = None,
) -> int:
    if not ids:
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        session.query(IntelStreamEvent).filter(
            IntelStreamEvent.id.in_(ids)
        ).update({"processed_at": now}, synchronize_session=False)
        session.commit()
    return len(ids)


# ---------------------------------------------------------------------------
# Express dispatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpressDispatchResult:
    n_processed: int
    n_dispatched_scout: int
    n_dispatched_hold: int
    held_symbols: tuple[str, ...]


def held_symbol_set(positions_provider) -> set[str]:
    """Resolve currently-held symbols. ``positions_provider`` is a callable
    returning a list of dicts with ``symbol`` keys (PositionMonitorRole's
    convention). Returns an empty set on any error.
    """
    if positions_provider is None:
        return set()
    try:
        positions = list(positions_provider())
    except Exception:
        return set()
    return {str(p.get("symbol", "")).upper() for p in positions if p.get("symbol")}


def dispatch_express(
    engine,
    *,
    sources: Iterable[str] = EXPRESS_SOURCES,
    held_symbols: set[str] | None = None,
    now: dt.datetime | None = None,
) -> ExpressDispatchResult:
    """Process unprocessed stream events: for each, decide whether to
    fire express scout (if not held) or express hold (if held). Marks
    rows processed regardless of dispatch outcome.

    NOTE: actual scout / hold dispatch is invoked via the existing
    ``scout_debate.run_scout_debate`` / ``hold_debate.run_hold_debate``
    paths — this function returns counts; the caller (a tiny role) does
    the actual dispatch sequentially. Keeps this module pure-ish for
    testability.
    """
    held = held_symbols or set()
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = unprocessed_events(engine, sources=sources)
    n_scout = 0
    n_hold = 0
    held_hits: set[str] = set()
    for r in rows:
        if r.symbol.upper() in held:
            n_hold += 1
            held_hits.add(r.symbol.upper())
        else:
            n_scout += 1
    mark_processed(engine, ids=[r.id for r in rows], now=now)
    return ExpressDispatchResult(
        n_processed=len(rows),
        n_dispatched_scout=n_scout,
        n_dispatched_hold=n_hold,
        held_symbols=tuple(sorted(held_hits)),
    )
