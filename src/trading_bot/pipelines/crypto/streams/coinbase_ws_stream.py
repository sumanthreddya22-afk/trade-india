"""Coinbase Advanced Trade WebSocket → crypto event_streamer.

Subscribes to the public ``ticker`` channel on
``wss://advanced-trade-ws.coinbase.com`` and converts material price
moves into ``StreamEvent`` rows in ``intel_stream_events_crypto`` via
``event_streamer.ingest_stream_event``. The express-lane dispatcher
then picks them up the same way it picks up Whale Alert events — held
positions get an immediate hold debate, candidates get an immediate
scout debate.

Why ticker (not market_trades or level2):
  - ``ticker`` emits one compact message per price update with last,
    open_24h, volume_24h. Cheap to consume, easy to threshold.
  - ``market_trades`` is too granular (one row per fill) — we'd flood
    intel_stream_events_crypto with low-signal rows.
  - ``level2`` requires authenticated channel and is overkill for the
    candidate-screening use case.

Threshold-based emission: every ticker carries the last 24h open. We
record a per-symbol baseline on the first tick we see and emit a
``StreamEvent`` only when the move from that baseline crosses
``coinbase_ws_min_pct_move`` (default 3%). On emission, the baseline
resets so consecutive 3% legs each fire one event. This keeps the
table from filling with the same tick re-fired on every update.

Threading: same pattern as ``alpaca_trade_stream.AlpacaTradeStreamRunner``.
A daemon thread runs ``websocket.WebSocketApp.run_forever()`` which
blocks until disconnect; the outer wrapper retries with exponential
backoff (1s → 30s) until ``stop()``. Everything between callbacks and
the DB writer is non-blocking — message handlers compute a
``StreamEvent`` and call ``ingest_stream_event`` (one INSERT, idempotent
via the unique index), so a slow checkpoint cannot stall the WS loop.

Reconnect: ``websocket-client`` ``WebSocketApp.run_forever`` has its
own reconnect on transport errors; we add an outer retry loop in case
``run_forever`` returns or raises (e.g. bad subscribe response). The
reconnect counter + last-error are emitted to the bus for observability
through the same ``role.failed`` event the Alpaca stream uses.

Heartbeat: Coinbase publishes a ``heartbeats`` channel; subscribing
to it keeps the connection alive on Cloudflare's 30s idle timeout.
We accept those messages silently — they don't produce StreamEvents.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from trading_bot.event_bus import bus as bus_mod
from trading_bot.pipelines.crypto.event_streamer import (
    StreamEvent,
    ingest_stream_event,
)
from trading_bot.pipelines.crypto.sources._base import normalize_crypto_symbol

logger = logging.getLogger(__name__)


COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"


# ---------------------------------------------------------------------------
# Per-symbol baseline tracker
# ---------------------------------------------------------------------------


@dataclass
class _SymbolBaseline:
    """Last-emitted price per symbol so consecutive moves each fire once."""
    last_emitted_price: float
    last_emitted_at: dt.datetime
    last_seen_price: float = 0.0
    last_seen_at: Optional[dt.datetime] = None


# ---------------------------------------------------------------------------
# Message → StreamEvent conversion
# ---------------------------------------------------------------------------


def ticker_message_to_events(
    message: Dict[str, Any],
    *,
    baselines: Dict[str, _SymbolBaseline],
    min_pct_move: float,
    now: Optional[dt.datetime] = None,
) -> List[StreamEvent]:
    """Convert a Coinbase ``ticker`` channel message into zero or more
    ``StreamEvent`` rows.

    Coinbase Advanced Trade ticker payload (relevant fields):
      {
        "channel": "ticker",
        "client_id": "...",
        "timestamp": "2026-01-01T00:00:00.123Z",
        "sequence_num": 0,
        "events": [
          {
            "type": "update",
            "tickers": [
              {
                "type": "ticker",
                "product_id": "BTC-USD",
                "price": "65000.12",
                "volume_24_h": "1234.5",
                "low_24_h": "63000.0",
                "high_24_h": "66000.0",
                "low_52_w": "...",
                "high_52_w": "...",
                "price_percent_chg_24_h": "1.23"
              }
            ]
          }
        ]
      }

    Returns at most one StreamEvent per product_id per call (the latest
    ticker wins if Coinbase sends multiple in one frame). Mutates
    ``baselines`` in place.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if (message or {}).get("channel") != "ticker":
        return []

    out: List[StreamEvent] = []
    events = message.get("events") or []
    for ev in events:
        tickers = ev.get("tickers") or []
        for tick in tickers:
            product_id = tick.get("product_id") or ""
            if not product_id:
                continue
            try:
                price = float(tick.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0.0:
                continue

            symbol = normalize_crypto_symbol(product_id)
            baseline = baselines.get(symbol)
            if baseline is None:
                # First tick: record baseline, no event.
                baselines[symbol] = _SymbolBaseline(
                    last_emitted_price=price,
                    last_emitted_at=now,
                    last_seen_price=price,
                    last_seen_at=now,
                )
                continue

            # Update last-seen always (used for diagnostics / dashboard).
            baseline.last_seen_price = price
            baseline.last_seen_at = now

            pct_move = (price - baseline.last_emitted_price) / baseline.last_emitted_price * 100.0
            if abs(pct_move) < min_pct_move:
                continue

            # Material move: emit one event, reset the baseline.
            sentiment = max(-1.0, min(1.0, pct_move / 10.0))  # +10% = +1.0 sentiment
            out.append(StreamEvent(
                symbol=symbol,
                source="coinbase_ws",
                event_at=now,
                sentiment=sentiment,
                chain=None,
                tx_hash=None,
                payload={
                    "product_id": product_id,
                    "price": price,
                    "baseline_price": baseline.last_emitted_price,
                    "pct_move": round(pct_move, 4),
                    "volume_24h": tick.get("volume_24_h"),
                    "high_24h": tick.get("high_24_h"),
                    "low_24h": tick.get("low_24_h"),
                    "price_percent_chg_24h": tick.get("price_percent_chg_24_h"),
                },
                # Stable id so dup ticks within the same second don't double-write.
                natural_id=f"coinbase_ws|{symbol}|{int(now.timestamp())}|{round(pct_move, 2)}",
            ))
            baseline.last_emitted_price = price
            baseline.last_emitted_at = now
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class CoinbaseWsStreamRunner:
    """Wraps the websocket-client WebSocketApp in a managed daemon thread.

    The runner is purely a transport: it owns the WS connection,
    converts ticker messages into StreamEvents, and writes them through
    ``ingest_stream_event``. The express-lane dispatcher
    (``event_streamer.dispatch_pending``) is the consumer — it polls
    the unprocessed rows on its own cadence in the daemon's main loop.
    """

    def __init__(
        self,
        engine: Any,
        *,
        product_ids: Sequence[str],
        min_pct_move: float = 3.0,
        url: str = COINBASE_WS_URL,
    ) -> None:
        self._engine = engine
        self._product_ids = list(product_ids)
        self._min_pct_move = min_pct_move
        self._url = url

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._reconnects = 0
        self._messages_seen = 0
        self._events_written = 0
        self._ws_app: Optional[Any] = None  # websocket.WebSocketApp
        self._baselines: Dict[str, _SymbolBaseline] = {}

    # --- public API ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._product_ids:
            logger.warning("coinbase_ws_stream: no product_ids configured — disabled")
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._run_with_retry, name="coinbase-ws-stream", daemon=True,
        )
        t.start()
        self._thread = t
        bus_mod.emit(
            "process.started",
            {"component": "coinbase_ws_stream",
             "product_ids": self._product_ids,
             "min_pct_move": self._min_pct_move},
            source="coinbase_ws_stream",
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            if self._ws_app is not None:
                # close() is thread-safe; closes the underlying socket so
                # run_forever() unblocks promptly.
                try:
                    self._ws_app.close()
                except Exception:
                    pass
        finally:
            if self._thread is not None:
                self._thread.join(timeout=timeout)
                self._thread = None

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "reconnects": self._reconnects,
            "messages_seen": self._messages_seen,
            "events_written": self._events_written,
            "baselines": {
                sym: {
                    "last_emitted_price": b.last_emitted_price,
                    "last_seen_price": b.last_seen_price,
                }
                for sym, b in self._baselines.items()
            },
        }

    # --- internals -------------------------------------------------------

    def _subscribe_payload(self, channel: str) -> str:
        return json.dumps({
            "type": "subscribe",
            "channel": channel,
            "product_ids": self._product_ids,
        })

    def _on_open(self, ws: Any) -> None:
        logger.info(
            "coinbase_ws_stream: connected; subscribing channels=ticker,heartbeats "
            "products=%s", self._product_ids,
        )
        # ticker for price moves; heartbeats so Cloudflare doesn't drop us
        try:
            ws.send(self._subscribe_payload("ticker"))
            ws.send(self._subscribe_payload("heartbeats"))
        except Exception as e:
            logger.warning("coinbase_ws_stream: subscribe send failed: %s", e)

    def _on_message(self, ws: Any, raw_message: str) -> None:
        self._messages_seen += 1
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.debug("coinbase_ws_stream: non-JSON frame ignored")
            return
        events = ticker_message_to_events(
            message,
            baselines=self._baselines,
            min_pct_move=self._min_pct_move,
        )
        for ev in events:
            try:
                row_id = ingest_stream_event(self._engine, event=ev)
                if row_id is not None:
                    self._events_written += 1
            except Exception as e:  # noqa: BLE001
                # DB hiccup must not kill the WS loop. Log + continue.
                logger.exception("coinbase_ws_stream: ingest failed: %s", e)

    def _on_error(self, ws: Any, error: Any) -> None:
        # WebSocketApp invokes on_error on transport errors; logged at
        # warning so a noisy reconnect cycle is visible without spamming.
        logger.warning("coinbase_ws_stream: ws error: %s", error)

    def _on_close(self, ws: Any, status: Any, msg: Any) -> None:
        logger.info(
            "coinbase_ws_stream: closed (status=%s msg=%s)", status, msg,
        )

    def _run_with_retry(self) -> None:
        try:
            from websocket import WebSocketApp  # type: ignore[import-not-found]
        except ImportError:
            logger.error(
                "coinbase_ws_stream: websocket-client not installed; "
                "skipping. add `websocket-client>=1.7` to dependencies."
            )
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._ws_app = WebSocketApp(
                    self._url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                logger.info("coinbase_ws_stream: connecting %s", self._url)
                # ping_interval keeps the connection alive across NAT idle
                # timeouts even if heartbeats subscribe fails.
                self._ws_app.run_forever(ping_interval=20, ping_timeout=10)
                if self._stop.is_set():
                    break
                self._reconnects += 1
                logger.info("coinbase_ws_stream: run_forever returned; reconnecting (#%d)", self._reconnects)
            except KeyboardInterrupt:
                break
            except Exception as e:  # noqa: BLE001
                self._reconnects += 1
                logger.warning(
                    "coinbase_ws_stream: connection error (#%d): %s — backing off %.1fs",
                    self._reconnects, e, backoff,
                )
                bus_mod.emit(
                    "role.failed",
                    {"role": "coinbase_ws_stream", "error": str(e)[:200]},
                    source="coinbase_ws_stream",
                )

            slept = 0.0
            while slept < backoff and not self._stop.is_set():
                time.sleep(0.1)
                slept += 0.1
            backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Daemon integration helper
# ---------------------------------------------------------------------------


def maybe_start(
    settings: Any,
    engine: Any,
) -> Optional[CoinbaseWsStreamRunner]:
    """Construct + start the runner from a Settings instance + state engine.

    Returns the runner so the daemon can call ``.stop()`` on shutdown.
    Returns None when ``coinbase_ws_enabled=False`` (the default — opt-in
    feature) or when no product_ids are configured.
    """
    import os
    if os.environ.get("TRADING_BOT_COINBASE_WS_DISABLED") == "1":
        logger.info("coinbase_ws_stream: disabled via env")
        return None
    enabled = bool(getattr(settings, "coinbase_ws_enabled", False))
    if not enabled:
        logger.info("coinbase_ws_stream: disabled in settings (coinbase_ws_enabled=False)")
        return None
    raw = (getattr(settings, "coinbase_ws_product_ids", "") or "").strip()
    products = [p.strip() for p in raw.split(",") if p.strip()]
    if not products:
        logger.info("coinbase_ws_stream: no product_ids configured")
        return None
    min_move = float(getattr(settings, "coinbase_ws_min_pct_move", 3.0))
    runner = CoinbaseWsStreamRunner(
        engine, product_ids=products, min_pct_move=min_move,
    )
    runner.start()
    return runner
