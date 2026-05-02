"""Live price ticks for held + top-N watchlist symbols (Phase 8).

Runs **inside the dashboard process**, not the daemon. Two reasons:

1. The daemon already opens an Alpaca ``TradingStream`` (Phase 1) under
   the same API key. Opening a second stream of a *different class*
   (``StockDataStream``) is generally allowed by Alpaca, but starting it
   in the dashboard process keeps the failure modes isolated — a market
   data outage doesn't kill the trade stream and vice versa.
2. Ticks are ephemeral. They never go through SQLite and never get
   persisted; the dashboard process is exactly where the consumer
   lives, so the data path is dashboard → broadcaster → SSE → browser
   with zero detours.

Throttle: at most 1 update per symbol per ``THROTTLE_MS`` to keep the
SSE payload sane even on volatile names.

Symbol set: held positions ∪ top-N opportunities. Recomputed on bus
events (``position.changed``, ``opportunities.updated``,
``intel.updated``) with a 5-second debounce so a flurry of scanner
ticks doesn't churn ``subscribe``/``unsubscribe`` calls.

Feature flag: ``TRADING_BOT_DASHBOARD_LIVE_PRICES=1`` (or ``"true"``)
opts in. Default OFF — paper-tier Alpaca behavior with two stream
classes is operator-verified before we ship.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from trading_bot.event_bus.subscriber import Broadcaster, Event

logger = logging.getLogger(__name__)


# Per-symbol max tick rate. 500ms is fast enough to feel live and
# slow enough that a chatty symbol can't drown out semantic events
# on the SSE channel. Tunable via env for tests.
_THROTTLE_MS = int(os.environ.get("TRADING_BOT_DASHBOARD_PRICE_THROTTLE_MS", "500"))

# Debounce window for symbol-set updates. Triggers come in bursts
# (a scanner tick fires position.changed for every adjustment) and
# we don't want to subscribe/unsubscribe on every one.
_DEBOUNCE_S = float(os.environ.get("TRADING_BOT_DASHBOARD_SYMBOL_DEBOUNCE_S", "5"))


def _is_enabled() -> bool:
    raw = os.environ.get("TRADING_BOT_DASHBOARD_LIVE_PRICES", "")
    return raw.lower() in ("1", "true", "yes", "on")


class MarketDataStreamRunner:
    """Owns the Alpaca StockDataStream lifecycle for the dashboard.

    Lifecycle:
      * ``start()`` — spawns SDK loop on its own thread, primes the
        symbol set from the provider, subscribes.
      * ``update_symbols()`` — debounced; triggered by bus events.
      * ``stop()`` — graceful shutdown.

    Stateless w.r.t. the bus singleton: receives a ``Broadcaster``
    explicitly so tests can wire a fake.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        symbol_provider: Callable[[], list[str]],
        broadcaster: Broadcaster,
        loop: asyncio.AbstractEventLoop,
        paper: bool = True,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._provider = symbol_provider
        self._broadcaster = broadcaster
        self._loop = loop
        self._paper = paper
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # Per-symbol throttle: last-tick timestamp (monotonic seconds).
        self._last_tick: dict[str, float] = {}
        self._subscribed: set[str] = set()
        self._subscribe_lock = threading.Lock()

        # Symbol-set debounce.
        self._debounce_timer: threading.Timer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not (self._api_key and self._api_secret):
            logger.warning("market_data_stream: missing creds — disabled")
            return
        self._stop.clear()
        t = threading.Thread(target=self._run, name="market-data-stream", daemon=True)
        t.start()
        self._thread = t

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._debounce_timer is not None:
            try:
                self._debounce_timer.cancel()
            except Exception:
                pass
            self._debounce_timer = None
        try:
            if self._stream is not None:
                stop_fn = getattr(self._stream, "stop_ws", None) or getattr(
                    self._stream, "stop", None
                )
                if stop_fn is not None:
                    if asyncio.iscoroutinefunction(stop_fn):
                        try:
                            asyncio.run(stop_fn())
                        except Exception:
                            pass
                    else:
                        try:
                            stop_fn()
                        except Exception:
                            pass
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ------------------------------------------------------------------
    # Symbol set
    # ------------------------------------------------------------------
    def update_symbols(self) -> None:
        """Recompute the desired symbol set from the provider and
        subscribe/unsubscribe deltas. Debounced.
        """
        if self._debounce_timer is not None:
            try:
                self._debounce_timer.cancel()
            except Exception:
                pass
        self._debounce_timer = threading.Timer(_DEBOUNCE_S, self._apply_symbol_update)
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _apply_symbol_update(self) -> None:
        try:
            wanted = {s.upper() for s in self._provider() if s}
        except Exception:
            logger.exception("market_data_stream: symbol provider failed")
            return
        with self._subscribe_lock:
            current = set(self._subscribed)
            to_add = wanted - current
            to_drop = current - wanted
            if not to_add and not to_drop:
                return
            self._subscribed = wanted
        # Delegate the actual SDK calls to the SDK loop. They're
        # synchronous as far as our thread cares.
        try:
            if self._stream is not None:
                if to_add:
                    self._stream.subscribe_trades(self._on_trade, *sorted(to_add))
                if to_drop:
                    # alpaca-py exposes ``unsubscribe_trades`` taking *args
                    unsub = getattr(self._stream, "unsubscribe_trades", None)
                    if unsub is not None:
                        unsub(*sorted(to_drop))
            logger.info(
                "market_data_stream: symbol set updated +%d -%d (now %d)",
                len(to_add), len(to_drop), len(self._subscribed),
            )
        except Exception:
            logger.exception("market_data_stream: subscribe delta failed")

    # ------------------------------------------------------------------
    # SDK callbacks
    # ------------------------------------------------------------------
    async def _on_trade(self, data: Any) -> None:
        """Alpaca trade tick. Throttle, then forward to the broadcaster."""
        try:
            symbol = str(getattr(data, "symbol", "") or "").upper()
            price = getattr(data, "price", None)
            ts = getattr(data, "timestamp", None)
            if not symbol or price is None:
                return
            now = time.monotonic()
            last = self._last_tick.get(symbol, 0.0)
            if (now - last) * 1000.0 < _THROTTLE_MS:
                return
            self._last_tick[symbol] = now
            ev = Event(
                id=0,  # ephemeral — no DB row, no Last-Event-ID impact
                type="price.update",
                payload={
                    "symbol": symbol,
                    "price": float(price),
                    "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "size": getattr(data, "size", None),
                },
                source="market_data_stream",
                process="dashboard",
                created_at=datetime.now(timezone.utc),
            )
            self._broadcaster.broadcast_threadsafe(ev, self._loop)
        except Exception:
            logger.exception("market_data_stream: trade handler error")

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------
    def _run(self) -> None:
        from alpaca.data.live import StockDataStream
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._stream = StockDataStream(
                    api_key=self._api_key,
                    secret_key=self._api_secret,
                )
                # Prime the symbol set immediately.
                wanted = sorted({s.upper() for s in (self._provider() or []) if s})
                if wanted:
                    self._stream.subscribe_trades(self._on_trade, *wanted)
                    with self._subscribe_lock:
                        self._subscribed = set(wanted)
                logger.info(
                    "market_data_stream: connecting (paper=%s, %d symbols)",
                    self._paper, len(wanted),
                )
                self._stream.run()
                logger.info("market_data_stream: stream returned cleanly")
                if self._stop.is_set():
                    break
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.warning(
                    "market_data_stream: connection error: %s — backing off %.1fs",
                    e, backoff,
                )
            slept = 0.0
            while slept < backoff and not self._stop.is_set():
                time.sleep(0.1)
                slept += 0.1
            backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Dashboard-side bootstrap helper
# ---------------------------------------------------------------------------
def maybe_start(
    *,
    settings,
    broadcaster: Broadcaster,
    loop: asyncio.AbstractEventLoop,
    symbol_provider: Callable[[], list[str]],
) -> MarketDataStreamRunner | None:
    """Start the runner if the feature flag is on and creds exist.

    Returns the runner so the dashboard can stop it on shutdown and
    drive ``update_symbols`` from bus events. Returns ``None`` otherwise.
    """
    if not _is_enabled():
        logger.info("market_data_stream: TRADING_BOT_DASHBOARD_LIVE_PRICES not set — disabled")
        return None
    api_key = getattr(settings, "alpaca_api_key", "") or ""
    api_secret = getattr(settings, "alpaca_api_secret", "") or ""
    bot_mode = getattr(settings, "bot_mode", "paper")
    if not api_key or not api_secret:
        logger.info("market_data_stream: missing creds; skipping")
        return None
    runner = MarketDataStreamRunner(
        api_key=api_key, api_secret=api_secret,
        symbol_provider=symbol_provider,
        broadcaster=broadcaster, loop=loop,
        paper=(bot_mode == "paper"),
    )
    runner.start()
    return runner
