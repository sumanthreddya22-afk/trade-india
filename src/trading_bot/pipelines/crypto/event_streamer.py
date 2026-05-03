"""Crypto event streamer (Phase 1G.1).

Ingests stream events from external real-time sources (Whale Alert REST
poll, Coinbase WebSocket, Binance funding/liquidation feeds, DefiLlama
TVL poll) into ``intel_stream_events_crypto`` and dispatches the
express-lane handler:

  inbound stream event
    ↓
  1. Persist row in intel_stream_events_crypto (idempotent via event_hash)
  2. Roll up the symbol's intel_candidates row immediately
  3. If symbol now has ``scout_verdict='elevate'`` → no-op (already known)
     If symbol is currently HELD (per the broker) → fire express hold_debate
     within 60s
     If symbol is NOT HELD AND newly elevated → fire express scout_debate
  4. Mark row processed_at = now

Per ADR 0003 (optimistic concurrency) the express handler runs in the
same sequential dispatch flow — no parallelism within the decision
chain, but multiple symbols can have their own express chains
running concurrently (each with its own per-symbol mutex).

Failure mode: every step is fail-soft. A bad event payload doesn't
crash the streamer loop; the express debate's rate-limit + error path
is the same skip-to-deterministic-gates fallback the regular debates
use.

Phase 1G.1 ships the framework + the Whale Alert REST poller as the
reference integration. Coinbase / Binance / DefiLlama get added next
(they share the StreamerBase surface).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import (
    IntelStreamEventCrypto,
    IntelCandidateCrypto,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stream-event shape (what each integration produces)
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """Normalised representation of one external stream signal.

    Each integration adapter (whale_alert / coinbase_ws / binance_ws / ...)
    converts its native payload into this shape before calling
    ``ingest_stream_event``.
    """
    symbol: str                     # e.g. "ETH/USD"
    source: str                     # e.g. "whale_alert"
    event_at: dt.datetime           # when the event occurred (not when received)
    sentiment: Optional[float] = None
    chain: Optional[str] = None
    tx_hash: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    natural_id: Optional[str] = None  # stable per-event id (tx_hash, post_id, etc.)


# ---------------------------------------------------------------------------
# Persist + dedup
# ---------------------------------------------------------------------------


def _make_event_hash(ev: StreamEvent) -> str:
    """Stable hash for dedup. Prefers ``natural_id`` when supplied."""
    h = hashlib.sha1()
    h.update(ev.source.encode())
    if ev.natural_id:
        h.update(b"|"); h.update(ev.natural_id.encode())
    elif ev.tx_hash:
        h.update(b"|"); h.update(ev.tx_hash.encode())
    else:
        # Fall back to (event_at_iso + payload_repr) — best-effort.
        h.update(b"|"); h.update(ev.event_at.isoformat().encode())
        h.update(b"|"); h.update(json.dumps(ev.payload, sort_keys=True, default=str).encode())
    return h.hexdigest()


def ingest_stream_event(
    engine: Any,
    *,
    event: StreamEvent,
    now: Optional[dt.datetime] = None,
) -> Optional[int]:
    """Persist one stream event. Returns the new row id, or None if it was
    a duplicate (already seen via the event_hash unique index).

    Pure DB write — no debate dispatch. Caller (the express-lane handler)
    decides whether to dispatch a debate based on the returned id.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    event_hash = _make_event_hash(event)
    row = IntelStreamEventCrypto(
        symbol=event.symbol.upper(),
        source=event.source,
        payload=json.dumps(event.payload, sort_keys=True, default=str),
        sentiment=event.sentiment,
        event_at=event.event_at,
        received_at=now,
        chain=event.chain,
        tx_hash=event.tx_hash,
        event_hash=event_hash,
        processed_at=None,
    )
    try:
        with Session(engine) as session:
            session.add(row)
            session.commit()
            return row.id
    except IntegrityError:
        return None


def mark_processed(
    engine: Any,
    *,
    event_id: int,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Flip processed_at on a stream event row."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        result = session.execute(
            sa_update(IntelStreamEventCrypto)
            .where(IntelStreamEventCrypto.id == event_id)
            .values(processed_at=now)
        )
        session.commit()
        return result.rowcount > 0


def unprocessed_events(
    engine: Any,
    *,
    max_age_minutes: int = 60,
    now: Optional[dt.datetime] = None,
) -> List[IntelStreamEventCrypto]:
    """Read recent unprocessed stream rows. Used by the express dispatcher
    on tick + at startup (catch-up after restart)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(minutes=max_age_minutes)
    with Session(engine) as session:
        rows = (
            session.query(IntelStreamEventCrypto)
            .filter(IntelStreamEventCrypto.processed_at.is_(None))
            .filter(IntelStreamEventCrypto.received_at >= cutoff)
            .order_by(IntelStreamEventCrypto.received_at.asc())
            .all()
        )
        # Detach so caller can read fields after session close
        for r in rows:
            session.expunge(r)
    return rows


# ---------------------------------------------------------------------------
# Express-lane dispatch
# ---------------------------------------------------------------------------


@dataclass
class ExpressDispatchResult:
    """Per-event dispatch outcome."""
    event_id: int
    symbol: str
    action: str                # "hold_debate" | "scout_debate" | "skip" | "error"
    reason: str = ""


def _is_held(held_symbols: Sequence[str], symbol: str) -> bool:
    """Case-insensitive position check."""
    target = symbol.upper()
    return any(s.upper() == target for s in held_symbols)


def dispatch_express_lane(
    engine: Any,
    *,
    event: IntelStreamEventCrypto,
    held_symbols: Sequence[str],
    on_hold_trigger: Optional[Callable[[str, IntelStreamEventCrypto], None]] = None,
    on_scout_trigger: Optional[Callable[[str, IntelStreamEventCrypto], None]] = None,
    now: Optional[dt.datetime] = None,
) -> ExpressDispatchResult:
    """Decide what to do with one stream event and invoke the right callback.

    Routing rules:
      - symbol is held → invoke ``on_hold_trigger`` (express hold debate)
      - symbol not held, candidate exists with scout_verdict='elevate'
        → no-op (regular orchestrator scan will pick it up)
      - symbol not held, candidate exists but not yet elevated
        → invoke ``on_scout_trigger`` (express scout debate)
      - symbol not in candidates table at all → no-op (the next regular
        ingestor tick will pull it in via roll_up)
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    symbol = event.symbol.upper()

    # Case A: symbol is held → express hold debate
    if _is_held(held_symbols, symbol):
        if on_hold_trigger is not None:
            try:
                on_hold_trigger(symbol, event)
            except Exception as e:  # noqa: BLE001
                logger.exception("express hold_trigger failed for %s: %s", symbol, e)
                mark_processed(engine, event_id=event.id, now=now)
                return ExpressDispatchResult(
                    event_id=event.id, symbol=symbol,
                    action="error", reason=f"hold_trigger:{e}",
                )
        mark_processed(engine, event_id=event.id, now=now)
        return ExpressDispatchResult(event_id=event.id, symbol=symbol, action="hold_debate")

    # Case B: symbol not held → check candidate state
    with Session(engine) as session:
        cand = (
            session.query(IntelCandidateCrypto)
            .filter(IntelCandidateCrypto.symbol == symbol)
            .one_or_none()
        )

    if cand is None:
        mark_processed(engine, event_id=event.id, now=now)
        return ExpressDispatchResult(
            event_id=event.id, symbol=symbol, action="skip",
            reason="no candidate row yet — next ingestor tick will pull it",
        )

    if cand.scout_verdict == "elevate":
        mark_processed(engine, event_id=event.id, now=now)
        return ExpressDispatchResult(
            event_id=event.id, symbol=symbol, action="skip",
            reason="already elevated — regular scanner will pick up",
        )

    # Case C: candidate exists but not yet elevated → express scout debate
    if on_scout_trigger is not None:
        try:
            on_scout_trigger(symbol, event)
        except Exception as e:  # noqa: BLE001
            logger.exception("express scout_trigger failed for %s: %s", symbol, e)
            mark_processed(engine, event_id=event.id, now=now)
            return ExpressDispatchResult(
                event_id=event.id, symbol=symbol,
                action="error", reason=f"scout_trigger:{e}",
            )
    mark_processed(engine, event_id=event.id, now=now)
    return ExpressDispatchResult(event_id=event.id, symbol=symbol, action="scout_debate")


def dispatch_pending(
    engine: Any,
    *,
    held_symbols: Sequence[str],
    on_hold_trigger: Optional[Callable[[str, IntelStreamEventCrypto], None]] = None,
    on_scout_trigger: Optional[Callable[[str, IntelStreamEventCrypto], None]] = None,
    max_events: int = 100,
    now: Optional[dt.datetime] = None,
) -> List[ExpressDispatchResult]:
    """Process all unprocessed stream events sequentially (per ADR 0003)."""
    pending = unprocessed_events(engine, now=now)[:max_events]
    return [
        dispatch_express_lane(
            engine, event=ev, held_symbols=held_symbols,
            on_hold_trigger=on_hold_trigger, on_scout_trigger=on_scout_trigger,
            now=now,
        )
        for ev in pending
    ]


# ---------------------------------------------------------------------------
# Whale Alert REST poller integration (Phase 1G.1 reference adapter)
# ---------------------------------------------------------------------------


def whale_alert_payload_to_events(payload: Dict[str, Any]) -> List[StreamEvent]:
    """Convert a Whale Alert ``/v1/transactions`` response into StreamEvents.

    Mirrors the per-tx classifier in ``sources/whale_alert.py`` so the
    polled stream and the per-tick collector share sentiment heuristics.
    """
    from trading_bot.pipelines.crypto.sources.whale_alert import _classify_sentiment
    from trading_bot.pipelines.crypto.sources._base import normalize_crypto_symbol

    txs = (payload or {}).get("transactions") or []
    out: List[StreamEvent] = []
    for tx in txs:
        raw_symbol = tx.get("symbol") or ""
        tx_hash = tx.get("hash") or ""
        chain = (tx.get("blockchain") or "").lower() or None
        timestamp = tx.get("timestamp")
        amount_usd = float(tx.get("amount_usd") or 0.0)
        from_type = (tx.get("from") or {}).get("owner_type", "")
        to_type = (tx.get("to") or {}).get("owner_type", "")

        if not raw_symbol or not tx_hash:
            continue

        try:
            event_at = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc)
        except (TypeError, ValueError):
            continue

        out.append(StreamEvent(
            symbol=normalize_crypto_symbol(raw_symbol),
            source="whale_alert",
            event_at=event_at,
            sentiment=_classify_sentiment(from_type, to_type),
            chain=chain,
            tx_hash=tx_hash,
            payload={
                "amount_usd": amount_usd,
                "from": from_type, "to": to_type,
                "raw_symbol": raw_symbol,
            },
            natural_id=tx_hash,
        ))
    return out


def poll_whale_alert(
    engine: Any,
    *,
    fetcher: Callable[..., Dict[str, Any]],
    api_key: str,
    lookback_seconds: int = 300,
    min_usd: int = 1_000_000,
    now: Optional[dt.datetime] = None,
) -> int:
    """Poll Whale Alert once and ingest each transaction as a stream event.

    Returns the count of newly-ingested rows (dups skipped). The express
    dispatcher then picks them up via ``dispatch_pending``.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if not api_key:
        return 0
    cutoff = now - dt.timedelta(seconds=lookback_seconds)
    try:
        payload = fetcher(api_key=api_key, start_unix=int(cutoff.timestamp()),
                            min_usd=min_usd)
    except Exception as e:  # noqa: BLE001
        logger.warning("poll_whale_alert fetch failed: %s", e)
        return 0

    events = whale_alert_payload_to_events(payload)
    written = 0
    for ev in events:
        if ingest_stream_event(engine, event=ev, now=now) is not None:
            written += 1
    return written
