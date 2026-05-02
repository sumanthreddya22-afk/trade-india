"""Alpaca Trading websocket → event bus.

Subscribes to Alpaca's ``trade_updates`` stream and emits one bus event
per state transition. The dashboard's ``_orders.html`` and
``_action_required.html`` fragments listen for these via the SSE
bridge, so a fill / rejection appears within ~1s instead of the next
2-minute poll.

Threading model: ``alpaca-py`` runs the SDK on its own asyncio loop
inside a worker thread (``TradingStream._run_forever`` creates the
loop). The handler we register is an ``async def`` that runs on that
loop. We keep the handler trivial — it computes a payload dict and
calls ``bus.emit()``, which is non-blocking by design (bounded
producer queue + dedicated writer thread). We never ``await`` SQLite
from the SDK callback, so a slow WAL checkpoint cannot stall a fill.

Reconnect: the SDK's own ``_run_forever`` reconnects on transport
errors. We also wrap the outer ``run()`` in our own retry loop so a
hard failure (e.g., bad creds bouncing) doesn't kill the daemon.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from trading_bot.event_bus import bus as bus_mod

logger = logging.getLogger(__name__)

# Map Alpaca's ``event`` enum onto our bus type. We don't emit for every
# Alpaca event — only the ones the dashboard renders. Anything not in
# this map gets logged at debug level and dropped.
_EVENT_MAP = {
    "new": "order.placed",
    "accepted": "order.placed",  # not always seen; alias to placed for safety
    "fill": "order.filled",
    "partial_fill": "order.partial_fill",
    "canceled": "order.canceled",
    "expired": "order.canceled",
    "rejected": "order.rejected",
    "replaced": "order.placed",  # treat as a fresh placed for UI purposes
    # We deliberately ignore: pending_new, pending_cancel, pending_replace,
    # pending_replaced, restated, calculated, suspended, order_replace_rejected
    # — they're useful for compliance audit but noisy on a UI feed.
}


def _safe(v: Any) -> Any:
    """Coerce alpaca-py / decimal types into JSON-friendly primitives."""
    if v is None:
        return None
    try:
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if isinstance(v, (int, float, str, bool)):
            return v
        return str(v)
    except Exception:
        return None


def _build_payload(data: Any) -> dict[str, Any]:
    """Translate Alpaca's TradeUpdate object into a compact bus payload.

    Defensive against shape changes between alpaca-py releases — every
    field is wrapped in try/except. Keep the payload small; the
    dashboard fragment refetch will pull the full order list anyway.
    """
    order = getattr(data, "order", None)
    payload: dict[str, Any] = {
        "alpaca_event": str(getattr(data, "event", "")) or None,
        "timestamp": _safe(getattr(data, "timestamp", None)),
    }
    if order is not None:
        payload.update({
            "symbol": _safe(getattr(order, "symbol", None)),
            "side": _safe(getattr(order, "side", None)),
            "qty": _safe(getattr(order, "qty", None)),
            "filled_qty": _safe(getattr(order, "filled_qty", None)),
            "order_type": _safe(getattr(order, "order_type", None)),
            "status": _safe(getattr(order, "status", None)),
            "limit_price": _safe(getattr(order, "limit_price", None)),
            "stop_price": _safe(getattr(order, "stop_price", None)),
            "filled_avg_price": _safe(getattr(order, "filled_avg_price", None)),
            "asset_class": _safe(getattr(order, "asset_class", None)),
            "order_id": _safe(getattr(order, "id", None)),
            "client_order_id": _safe(getattr(order, "client_order_id", None)),
        })
    # Fill-specific fields land at the top level so the UI doesn't have
    # to guess where to look.
    for fname in ("price", "qty"):
        v = getattr(data, fname, None)
        if v is not None:
            payload[f"fill_{fname}"] = _safe(v)
    return payload


async def _trade_update_handler(data: Any) -> None:
    """Async callback registered with TradingStream.subscribe_trade_updates."""
    try:
        alpaca_event = str(getattr(data, "event", "") or "").lower()
        bus_type = _EVENT_MAP.get(alpaca_event)
        if bus_type is None:
            # Quiet — these are normal but uninteresting for the dashboard.
            logger.debug("alpaca_trade_stream: ignoring event %r", alpaca_event)
            return
        payload = _build_payload(data)
        bus_mod.emit(bus_type, payload, source="alpaca_trade_stream")
        # Fills also imply a position change. Emit a second event so the
        # holdings tile / system view's Position Tracker node know to
        # refresh without waiting for a separate REST poll.
        if alpaca_event == "fill":
            bus_mod.emit(
                "position.changed",
                {"symbol": payload.get("symbol"), "trigger": "fill"},
                source="alpaca_trade_stream",
            )
    except Exception:
        # Never let a callback exception kill the SDK loop.
        logger.exception("alpaca_trade_stream: handler error")


class AlpacaTradeStreamRunner:
    """Wraps an alpaca-py TradingStream in a managed background thread.

    Why a thread (not just letting the SDK manage its own): we want the
    daemon's main loop and APScheduler to remain in control. The SDK
    internally uses an asyncio loop on a worker thread; we just need a
    place to call ``stream.run()`` that we can stop cleanly on SIGTERM.
    """

    def __init__(self, api_key: str, api_secret: str, *, paper: bool = True) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._paper = paper
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconnects = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._api_key or not self._api_secret:
            logger.warning("alpaca_trade_stream: missing creds — stream disabled")
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._run_with_retry, name="alpaca-trade-stream", daemon=True,
        )
        t.start()
        self._thread = t
        bus_mod.emit(
            "process.started",
            {"component": "alpaca_trade_stream", "paper": self._paper},
            source="alpaca_trade_stream",
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            if self._stream is not None:
                # ``stop_ws`` is the public way to ask the SDK to break
                # out of run_forever; call from any thread.
                stop_fn = getattr(self._stream, "stop_ws", None) or getattr(
                    self._stream, "stop", None
                )
                if stop_fn is not None:
                    try:
                        # stop_ws is a coroutine on alpaca-py>=0.30; run it
                        # to completion if possible.
                        if asyncio.iscoroutinefunction(stop_fn):
                            asyncio.run(stop_fn())
                        else:
                            stop_fn()
                    except Exception:
                        pass
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run_with_retry(self) -> None:
        from alpaca.trading.stream import TradingStream

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._stream = TradingStream(
                    api_key=self._api_key,
                    secret_key=self._api_secret,
                    paper=self._paper,
                )
                # alpaca-py uses ``subscribe_trade_updates`` to register
                # an async handler; ``run()`` blocks until disconnected.
                self._stream.subscribe_trade_updates(_trade_update_handler)
                logger.info("alpaca_trade_stream: connecting (paper=%s)", self._paper)
                self._stream.run()
                logger.info("alpaca_trade_stream: stream returned cleanly")
                # If run() returned without exception we're done unless
                # asked to restart. Honor the stop flag.
                if self._stop.is_set():
                    break
                # Otherwise, try to reconnect.
                self._reconnects += 1
            except KeyboardInterrupt:
                break
            except Exception as e:
                self._reconnects += 1
                logger.warning(
                    "alpaca_trade_stream: connection error (#%d): %s — backing off %.1fs",
                    self._reconnects, e, backoff,
                )
                bus_mod.emit(
                    "role.failed",
                    {"role": "alpaca_trade_stream", "error": str(e)[:200]},
                    source="alpaca_trade_stream",
                )
            # Exponential backoff capped at 30s.
            slept = 0.0
            while slept < backoff and not self._stop.is_set():
                time.sleep(0.1)
                slept += 0.1
            backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Daemon integration helper
# ---------------------------------------------------------------------------
def maybe_start(settings) -> AlpacaTradeStreamRunner | None:
    """Construct + start the runner from a Settings instance.

    Returns the runner so the daemon can call ``.stop()`` on shutdown.
    Returns None when creds are missing (or the user disabled it via
    ``TRADING_BOT_TRADE_STREAM_DISABLED=1``) — the dashboard still works
    without the live stream; fragments fall back to polling.
    """
    import os
    if os.environ.get("TRADING_BOT_TRADE_STREAM_DISABLED") == "1":
        logger.info("alpaca_trade_stream: disabled via env")
        return None
    api_key = getattr(settings, "alpaca_api_key", "") or ""
    api_secret = getattr(settings, "alpaca_api_secret", "") or ""
    bot_mode = getattr(settings, "bot_mode", "paper")
    if not api_key or not api_secret:
        logger.info("alpaca_trade_stream: missing creds; skipping")
        return None
    runner = AlpacaTradeStreamRunner(api_key, api_secret, paper=(bot_mode == "paper"))
    runner.start()
    return runner
