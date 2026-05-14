"""Market-data writer: bars_fetcher → ``data_watermark``.

Plan v4 §1B + §6: per (source_id, lane) we keep the latest event_ts so
the freshness kill switch can fire when a lane goes stale.

This module is intentionally tiny: the heavy lifting (HTTP, schema,
auth) is in ``AlpacaAdapter``; the watermark write is in
``ingest.watermarks``. We just glue them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Callable, Iterable

from trading_bot.ingest.watermarks import write_watermark
from trading_bot.ledger import connect_writer

log = logging.getLogger(__name__)

SOURCE_ID_ALPACA = "alpaca_paper"
LANE_EQUITY = "equity"


def _payload_hash(bar: dict) -> str:
    canonical = json.dumps(
        {k: bar[k] for k in ("open", "high", "low", "close", "volume")},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest_bars_once(
    *,
    ledger_db: Path,
    symbols: tuple[str, ...] | Iterable[str],
    bars_fetcher: Callable[..., dict],
    source_id: str = SOURCE_ID_ALPACA,
    lane: str = LANE_EQUITY,
) -> int:
    """Pull latest bars for ``symbols`` and update the lane watermark.

    Returns the number of symbols for which a fresh bar was found.
    """
    symbols = tuple(symbols)
    if not symbols:
        return 0
    bars = bars_fetcher(symbols=symbols)
    if not bars:
        return 0

    # The lane-level watermark is the *max* event_ts across the universe,
    # because the freshness gate is "is *any* data fresh enough?", not
    # per-symbol. We still hash the latest payload so the operator can
    # debug drift.
    latest_ts = None
    latest_hash = None
    for sym, bar in bars.items():
        ts = bar.get("ts")
        if isinstance(ts, str):
            ts = dt.datetime.fromisoformat(ts)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_hash = _payload_hash(bar)

    if latest_ts is None:
        return 0

    conn = connect_writer(ledger_db)
    try:
        write_watermark(
            conn, source_id=source_id, lane=lane,
            event_ts=latest_ts, payload_hash=latest_hash,
        )
    finally:
        conn.close()
    return len(bars)


__all__ = ["LANE_EQUITY", "SOURCE_ID_ALPACA", "ingest_bars_once"]
