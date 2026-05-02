"""Phase 8 — market-data stream tests.

We can't connect to Alpaca in tests; the interesting code is the
throttle, the symbol-set debounce, and the ephemeral broadcast path.
All of those are exercised here with stubs.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading_bot.event_bus.subscriber import Broadcaster, Event
from trading_bot.streams.market_data_stream import (
    MarketDataStreamRunner,
    _is_enabled,
    maybe_start,
)


def _create_events_table(p: str) -> None:
    with sqlite3.connect(p) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', "
            "source TEXT NOT NULL DEFAULT '', "
            "process TEXT NOT NULL DEFAULT 'unknown', "
            "created_at DATETIME NOT NULL)"
        )
        conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    p = str(tmp_path / "state.db")
    _create_events_table(p)
    return p


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------
class TestEnableFlag:
    def test_off_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("TRADING_BOT_DASHBOARD_LIVE_PRICES", raising=False)
        assert _is_enabled() is False

    def test_truthy_values_enable(self, monkeypatch) -> None:
        for v in ("1", "true", "yes", "on"):
            monkeypatch.setenv("TRADING_BOT_DASHBOARD_LIVE_PRICES", v)
            assert _is_enabled() is True, v

    def test_falsy_values_dont_enable(self, monkeypatch) -> None:
        for v in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("TRADING_BOT_DASHBOARD_LIVE_PRICES", v)
            assert _is_enabled() is False, v

    def test_maybe_start_returns_none_when_flag_off(self, monkeypatch) -> None:
        monkeypatch.delenv("TRADING_BOT_DASHBOARD_LIVE_PRICES", raising=False)
        cfg = SimpleNamespace(alpaca_api_key="x", alpaca_api_secret="y", bot_mode="paper")
        loop = asyncio.new_event_loop()
        try:
            r = maybe_start(
                settings=cfg, broadcaster=MagicMock(), loop=loop,
                symbol_provider=lambda: ["AAPL"],
            )
            assert r is None
        finally:
            loop.close()

    def test_maybe_start_returns_none_when_creds_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADING_BOT_DASHBOARD_LIVE_PRICES", "1")
        cfg = SimpleNamespace(alpaca_api_key="", alpaca_api_secret="y", bot_mode="paper")
        loop = asyncio.new_event_loop()
        try:
            r = maybe_start(
                settings=cfg, broadcaster=MagicMock(), loop=loop,
                symbol_provider=lambda: ["AAPL"],
            )
            assert r is None
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Tick handler — throttle + payload shape
# ---------------------------------------------------------------------------
def _runner_with_fake_broadcaster() -> tuple[MarketDataStreamRunner, list]:
    captured: list[Event] = []

    class FakeBroadcaster:
        def broadcast_threadsafe(self, ev, _loop):
            captured.append(ev)

    runner = MarketDataStreamRunner(
        api_key="k", api_secret="s",
        symbol_provider=lambda: ["AAPL", "MSFT"],
        broadcaster=FakeBroadcaster(),  # type: ignore[arg-type]
        loop=asyncio.new_event_loop(),
        paper=True,
    )
    return runner, captured


class TestTickHandler:
    def test_first_tick_passes_throttle(self) -> None:
        runner, captured = _runner_with_fake_broadcaster()
        tick = SimpleNamespace(
            symbol="AAPL", price=187.42,
            timestamp=datetime(2026, 5, 2, 13, 0, tzinfo=timezone.utc),
            size=100,
        )
        asyncio.run(runner._on_trade(tick))
        assert len(captured) == 1
        ev = captured[0]
        assert ev.type == "price.update"
        assert ev.id == 0  # ephemeral — no DB row
        assert ev.payload["symbol"] == "AAPL"
        assert ev.payload["price"] == 187.42

    def test_second_tick_within_throttle_window_dropped(self, monkeypatch) -> None:
        # Set a long throttle so two back-to-back ticks definitely collide.
        monkeypatch.setenv("TRADING_BOT_DASHBOARD_PRICE_THROTTLE_MS", "5000")
        # Re-import to pick up the env change.
        import importlib

        import trading_bot.streams.market_data_stream as md
        importlib.reload(md)
        captured: list[Event] = []

        class FakeBroadcaster:
            def broadcast_threadsafe(self, ev, _loop):
                captured.append(ev)

        runner = md.MarketDataStreamRunner(
            api_key="k", api_secret="s",
            symbol_provider=lambda: ["AAPL"],
            broadcaster=FakeBroadcaster(),  # type: ignore[arg-type]
            loop=asyncio.new_event_loop(),
            paper=True,
        )
        tick = SimpleNamespace(symbol="AAPL", price=187.0, timestamp=None, size=10)
        asyncio.run(runner._on_trade(tick))
        asyncio.run(runner._on_trade(tick))
        assert len(captured) == 1
        # Reset the env so other tests use the default 500ms.
        monkeypatch.delenv("TRADING_BOT_DASHBOARD_PRICE_THROTTLE_MS")
        importlib.reload(md)

    def test_throttle_is_per_symbol(self) -> None:
        runner, captured = _runner_with_fake_broadcaster()
        for sym in ("AAPL", "MSFT"):
            asyncio.run(runner._on_trade(
                SimpleNamespace(symbol=sym, price=100.0, timestamp=None, size=1)
            ))
        # Both should pass — separate symbols don't share the throttle bucket.
        assert {e.payload["symbol"] for e in captured} == {"AAPL", "MSFT"}

    def test_invalid_tick_silently_dropped(self) -> None:
        runner, captured = _runner_with_fake_broadcaster()
        # Missing price.
        asyncio.run(runner._on_trade(
            SimpleNamespace(symbol="AAPL", price=None, timestamp=None, size=1)
        ))
        # Missing symbol.
        asyncio.run(runner._on_trade(
            SimpleNamespace(symbol="", price=100.0, timestamp=None, size=1)
        ))
        assert captured == []


# ---------------------------------------------------------------------------
# Broadcaster ephemeral path — also verifies the SSE wire format omits
# the `id:` line for id=0 events.
# ---------------------------------------------------------------------------
class TestEphemeralBroadcast:
    def test_id_zero_event_omits_id_line(self) -> None:
        ev = Event(
            id=0, type="price.update",
            payload={"symbol": "AAPL", "price": 187.42},
            source="market_data_stream", process="dashboard",
            created_at=datetime.now(timezone.utc),
        )
        line = ev.to_sse_line()
        assert line.startswith("event: price.update\n")
        assert "id: " not in line
        assert "data: " in line

    def test_durable_event_includes_id_line(self) -> None:
        ev = Event(
            id=42, type="decision.created",
            payload={}, source="x", process="daemon",
            created_at=datetime.now(timezone.utc),
        )
        line = ev.to_sse_line()
        assert line.startswith("id: 42\n")
        assert "event: decision.created\n" in line

    def test_broadcast_skips_sqlite(self, db: str) -> None:
        async def _run() -> None:
            b = Broadcaster(db, poll_interval=0.05)
            await b.start()
            try:
                cq = await b.subscribe()
                ev = Event(
                    id=0, type="price.update",
                    payload={"symbol": "AAPL", "price": 100.0},
                    source="md", process="dashboard",
                    created_at=datetime.now(timezone.utc),
                )
                await b.broadcast(ev)
                got = await asyncio.wait_for(cq.q.get(), timeout=1.0)
                assert got.type == "price.update"
                # SQLite was never touched — events table still empty.
                with sqlite3.connect(db) as conn:
                    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                assert n == 0
            finally:
                await b.stop()

        asyncio.run(_run())

    def test_broadcast_threadsafe_from_other_thread(self, db: str) -> None:
        import threading

        async def _run() -> None:
            b = Broadcaster(db, poll_interval=0.05)
            await b.start()
            try:
                cq = await b.subscribe()
                loop = asyncio.get_running_loop()

                def _other_thread() -> None:
                    ev = Event(
                        id=0, type="price.update",
                        payload={"symbol": "MSFT", "price": 411.0},
                        source="md", process="dashboard",
                        created_at=datetime.now(timezone.utc),
                    )
                    b.broadcast_threadsafe(ev, loop)

                t = threading.Thread(target=_other_thread)
                t.start()
                got = await asyncio.wait_for(cq.q.get(), timeout=2.0)
                t.join(1.0)
                assert got.payload["symbol"] == "MSFT"
            finally:
                await b.stop()

        asyncio.run(_run())
