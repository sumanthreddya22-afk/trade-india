"""Coinbase WebSocket stream adapter tests.

We never open a real WebSocket. The unit tests cover the two pure
seams that carry the protocol logic:

  1. ``ticker_message_to_events`` — payload shape → StreamEvent list.
     Verified for: first-tick baseline, sub-threshold suppress, threshold
     emit + baseline reset, multi-symbol independence, malformed input.

  2. ``CoinbaseWsStreamRunner._on_message`` — feeds the raw JSON line
     through the adapter and asserts ``intel_stream_events_crypto`` rows
     land via the existing ``ingest_stream_event`` path.

The stop / start lifecycle is verified by checking the thread is
spawned + joined, but ``run_forever`` is monkey-patched out so we never
hit the network.
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import IntelStreamEventCrypto
from trading_bot.pipelines.crypto.streams.coinbase_ws_stream import (
    COINBASE_WS_URL,
    CoinbaseWsStreamRunner,
    _SymbolBaseline,
    maybe_start,
    ticker_message_to_events,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# ticker_message_to_events: pure conversion
# ---------------------------------------------------------------------------


def _ticker_msg(product_id: str, price: float, *, channel: str = "ticker",
                event_type: str = "update") -> Dict[str, Any]:
    return {
        "channel": channel,
        "timestamp": "2026-05-03T00:00:00.000Z",
        "events": [
            {
                "type": event_type,
                "tickers": [
                    {
                        "type": "ticker",
                        "product_id": product_id,
                        "price": str(price),
                        "volume_24_h": "100.0",
                        "low_24_h": "60000.0",
                        "high_24_h": "70000.0",
                        "price_percent_chg_24_h": "1.0",
                    },
                ],
            },
        ],
    }


def test_first_tick_records_baseline_no_event():
    baselines: Dict[str, _SymbolBaseline] = {}
    out = ticker_message_to_events(
        _ticker_msg("BTC-USD", 65000.0),
        baselines=baselines, min_pct_move=3.0,
    )
    assert out == []
    assert "BTC/USD" in baselines
    assert baselines["BTC/USD"].last_emitted_price == 65000.0


def test_sub_threshold_move_does_not_emit():
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    baselines = {
        "BTC/USD": _SymbolBaseline(
            last_emitted_price=65000.0, last_emitted_at=now,
        ),
    }
    out = ticker_message_to_events(
        _ticker_msg("BTC-USD", 65500.0),  # +0.77%
        baselines=baselines, min_pct_move=3.0, now=now,
    )
    assert out == []
    # baseline unchanged; last_seen updated for diagnostics
    assert baselines["BTC/USD"].last_emitted_price == 65000.0
    assert baselines["BTC/USD"].last_seen_price == 65500.0


def test_threshold_move_emits_and_resets_baseline():
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    baselines = {
        "BTC/USD": _SymbolBaseline(
            last_emitted_price=65000.0, last_emitted_at=now,
        ),
    }
    out = ticker_message_to_events(
        _ticker_msg("BTC-USD", 67500.0),  # +3.85%, > 3% threshold
        baselines=baselines, min_pct_move=3.0, now=now,
    )
    assert len(out) == 1
    ev = out[0]
    assert ev.symbol == "BTC/USD"
    assert ev.source == "coinbase_ws"
    assert ev.payload["product_id"] == "BTC-USD"
    assert ev.payload["price"] == 67500.0
    assert ev.payload["baseline_price"] == 65000.0
    # sentiment: +3.85% / 10 → +0.385, clamped within [-1, 1]
    assert 0.3 < ev.sentiment < 0.5
    # baseline reset to new price → next 3% leg from 67500, not 65000
    assert baselines["BTC/USD"].last_emitted_price == 67500.0


def test_negative_move_emits_with_negative_sentiment():
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    baselines = {
        "ETH/USD": _SymbolBaseline(
            last_emitted_price=3500.0, last_emitted_at=now,
        ),
    }
    out = ticker_message_to_events(
        _ticker_msg("ETH-USD", 3300.0),  # -5.71%
        baselines=baselines, min_pct_move=3.0, now=now,
    )
    assert len(out) == 1
    assert out[0].sentiment is not None and out[0].sentiment < 0


def test_multi_symbol_independence():
    """Each symbol tracks its own baseline."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    baselines: Dict[str, _SymbolBaseline] = {}

    # Bootstrap both
    ticker_message_to_events(
        _ticker_msg("BTC-USD", 65000.0),
        baselines=baselines, min_pct_move=3.0, now=now,
    )
    ticker_message_to_events(
        _ticker_msg("ETH-USD", 3500.0),
        baselines=baselines, min_pct_move=3.0, now=now,
    )

    # BTC moves 4% — should emit; ETH stays flat — should not.
    out_btc = ticker_message_to_events(
        _ticker_msg("BTC-USD", 67600.0),  # +4%
        baselines=baselines, min_pct_move=3.0, now=now,
    )
    out_eth = ticker_message_to_events(
        _ticker_msg("ETH-USD", 3520.0),   # +0.57%
        baselines=baselines, min_pct_move=3.0, now=now,
    )
    assert len(out_btc) == 1
    assert out_eth == []


def test_non_ticker_channel_ignored():
    """Heartbeat / status messages must not become StreamEvents."""
    out = ticker_message_to_events(
        {"channel": "heartbeats", "events": []},
        baselines={}, min_pct_move=3.0,
    )
    assert out == []


def test_malformed_ticker_is_skipped():
    baselines: Dict[str, _SymbolBaseline] = {}
    msg = {
        "channel": "ticker",
        "events": [{"type": "update", "tickers": [
            {"type": "ticker", "product_id": "BTC-USD", "price": "not-a-number"},
            {"type": "ticker", "product_id": "", "price": "100"},
            {"type": "ticker", "product_id": "ETH-USD", "price": "0"},
        ]}],
    }
    out = ticker_message_to_events(
        msg, baselines=baselines, min_pct_move=3.0,
    )
    # All three rows are malformed in different ways → no events, no baselines
    assert out == []
    assert baselines == {}


# ---------------------------------------------------------------------------
# Runner._on_message: integration with ingest_stream_event
# ---------------------------------------------------------------------------


def test_on_message_writes_row_to_db(engine):
    runner = CoinbaseWsStreamRunner(
        engine, product_ids=["BTC-USD"], min_pct_move=3.0,
    )

    # Bootstrap baseline (first tick is silent).
    runner._on_message(SimpleNamespace(), json.dumps(_ticker_msg("BTC-USD", 65000.0)))

    # Material move: should write one row.
    runner._on_message(SimpleNamespace(), json.dumps(_ticker_msg("BTC-USD", 67500.0)))

    with Session(engine) as session:
        rows = session.query(IntelStreamEventCrypto).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "BTC/USD"
    assert row.source == "coinbase_ws"
    payload = json.loads(row.payload)
    assert payload["product_id"] == "BTC-USD"
    assert payload["price"] == 67500.0


def test_on_message_dedups_via_natural_id(engine):
    """Two identical ticks within the same second shouldn't double-write."""
    runner = CoinbaseWsStreamRunner(
        engine, product_ids=["BTC-USD"], min_pct_move=3.0,
    )
    # Force a fixed timestamp so two events would collide on natural_id
    fixed = dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.timezone.utc)

    # Mock ticker_message_to_events at module scope so emits land at fixed now
    # — easier to do it by manually constructing two events with the same
    # natural_id and feeding through ingest_stream_event directly.
    from trading_bot.pipelines.crypto.event_streamer import (
        StreamEvent, ingest_stream_event,
    )
    ev = StreamEvent(
        symbol="BTC/USD",
        source="coinbase_ws",
        event_at=fixed,
        sentiment=0.5,
        chain=None, tx_hash=None,
        payload={"price": 67500.0},
        natural_id=f"coinbase_ws|BTC/USD|{int(fixed.timestamp())}|3.85",
    )
    first = ingest_stream_event(engine, event=ev, now=fixed)
    second = ingest_stream_event(engine, event=ev, now=fixed)
    assert first is not None
    assert second is None  # rejected by unique index
    with Session(engine) as session:
        rows = session.query(IntelStreamEventCrypto).all()
    assert len(rows) == 1


def test_on_message_handles_non_json(engine):
    runner = CoinbaseWsStreamRunner(
        engine, product_ids=["BTC-USD"], min_pct_move=3.0,
    )
    # Should not raise.
    runner._on_message(SimpleNamespace(), "<not json>")
    with Session(engine) as session:
        rows = session.query(IntelStreamEventCrypto).all()
    assert rows == []


# ---------------------------------------------------------------------------
# maybe_start: gating
# ---------------------------------------------------------------------------


def test_maybe_start_disabled_returns_none(engine):
    settings = SimpleNamespace(
        coinbase_ws_enabled=False,
        coinbase_ws_product_ids="BTC-USD",
        coinbase_ws_min_pct_move=3.0,
    )
    assert maybe_start(settings, engine) is None


def test_maybe_start_no_products_returns_none(engine):
    settings = SimpleNamespace(
        coinbase_ws_enabled=True,
        coinbase_ws_product_ids="",
        coinbase_ws_min_pct_move=3.0,
    )
    assert maybe_start(settings, engine) is None


def test_maybe_start_env_disable_takes_precedence(engine, monkeypatch):
    monkeypatch.setenv("TRADING_BOT_COINBASE_WS_DISABLED", "1")
    settings = SimpleNamespace(
        coinbase_ws_enabled=True,
        coinbase_ws_product_ids="BTC-USD,ETH-USD",
        coinbase_ws_min_pct_move=3.0,
    )
    assert maybe_start(settings, engine) is None


def test_runner_start_spawns_thread_then_stops(engine, monkeypatch):
    """Start the runner with a stubbed run_forever so no real socket opens."""
    runner = CoinbaseWsStreamRunner(
        engine, product_ids=["BTC-USD"], min_pct_move=3.0,
    )

    # Stub WebSocketApp so the thread's _run_with_retry returns immediately
    class _StubApp:
        def __init__(self, *args, **kwargs):
            self._kw = kwargs

        def run_forever(self, **kwargs):
            return None

        def close(self):
            pass

    import sys
    fake_module = SimpleNamespace(WebSocketApp=_StubApp)
    monkeypatch.setitem(sys.modules, "websocket", fake_module)

    runner.start()
    assert runner._thread is not None
    runner.stop(timeout=2.0)
    assert runner._thread is None or not runner._thread.is_alive()


def test_runner_default_url():
    assert COINBASE_WS_URL.startswith("wss://advanced-trade-ws.coinbase.com")
