"""Unit tests for WebullAdapter — pure logic, no network.

The live-smoke test that actually places a $1 order lives in
``tests/test_webull_adapter_live.py`` and is gated on
``WEBULL_LIVE_SMOKE=1`` (WS3 step 5).
"""
from __future__ import annotations

import urllib.error

import pytest

from trading_bot.ingest.webull_adapter import (
    WebullAdapter,
    WebullCreds,
    _to_canonical_asset_class,
    _to_canonical_status,
    _to_canonical_symbol,
    _to_webull_symbol,
)


def _creds() -> WebullCreds:
    return WebullCreds(
        api_key="k", api_secret="s", account_id="A1",
        base_url="https://api.example",
    )


# ---- Normalization ----
def test_status_canonicalisation() -> None:
    assert _to_canonical_status("SUBMITTED") == "accepted"
    assert _to_canonical_status("FILLED") == "filled"
    assert _to_canonical_status("CANCELLED") == "canceled"
    assert _to_canonical_status("Rejected") == "rejected"
    assert _to_canonical_status("PARTIAL_FILLED") == "accepted"


def test_asset_class_canonicalisation() -> None:
    assert _to_canonical_asset_class("stock") == "us_equity"
    assert _to_canonical_asset_class("CRYPTO") == "crypto"
    assert _to_canonical_asset_class("US_OPTION") == "us_option"


def test_symbol_normalisation_crypto() -> None:
    assert _to_canonical_symbol("BTCUSDT", "crypto") == "BTC/USD"
    assert _to_canonical_symbol("ETHUSDT", "crypto") == "ETH/USD"
    assert _to_canonical_symbol("DOGEUSD", "crypto") == "DOGE/USD"
    # Equity passthrough.
    assert _to_canonical_symbol("SPY", "us_equity") == "SPY"


def test_symbol_to_webull_inverse() -> None:
    assert _to_webull_symbol("BTC/USD", "crypto") == "BTCUSDT"
    assert _to_webull_symbol("SPY", "us_equity") == "SPY"


# ---- Shadow mode ----
def test_submit_order_shadow_mode_default() -> None:
    a = WebullAdapter(_creds(), enable_submit=False)
    r = a.submit_order(
        client_order_id="x1", symbol="SPY", qty=1, side="buy",
        asset_class="us_equity",
    )
    assert r["ok"] is False
    assert "shadow_mode" in r["error"]
    assert r["status"] == "rejected"
    assert r["asset_class"] == "us_equity"


# ---- Session refresh on 401 ----
class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int):
        super().__init__("u", code, "msg", None, None)  # type: ignore[arg-type]


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


def test_session_refresh_on_401(monkeypatch) -> None:
    a = WebullAdapter(_creds(), enable_submit=True)

    # Track every urlopen call so we can assert "401 → refresh → retry".
    calls: list[str] = []
    state = {"trade_attempt": 0}

    def fake_urlopen(req, timeout=None):
        path = req.full_url.split("api.example", 1)[1].split("?", 1)[0]
        calls.append(path)
        if path == "/auth/v1/token":
            return _FakeResp(b'{"access_token":"tok","expires_in":3600}')
        if path == "/trade/v1/order":
            state["trade_attempt"] += 1
            if state["trade_attempt"] == 1:
                raise _FakeHTTPError(401)
            return _FakeResp(b'{"order_id":"B1","status":"SUBMITTED"}')
        return _FakeResp(b"{}")

    import trading_bot.ingest.webull_adapter as wb
    monkeypatch.setattr(wb.urllib.request, "urlopen", fake_urlopen)

    r = a.submit_order(
        client_order_id="x1", symbol="SPY", qty=1, side="buy",
        asset_class="us_equity",
    )
    assert r["ok"] is True
    assert r["status"] == "accepted"
    assert r["broker_order_id"] == "B1"
    # 2 trade attempts (401 + retry) + at least 1 token refresh.
    assert calls.count("/trade/v1/order") == 2
    assert calls.count("/auth/v1/token") >= 1


def test_submit_order_failure_returns_ok_false(monkeypatch) -> None:
    a = WebullAdapter(_creds(), enable_submit=True)

    def fake_urlopen(req, timeout=None):
        path = req.full_url.split("api.example", 1)[1].split("?", 1)[0]
        if path == "/auth/v1/token":
            return _FakeResp(b'{"access_token":"tok","expires_in":3600}')
        raise RuntimeError("network down")

    import trading_bot.ingest.webull_adapter as wb
    monkeypatch.setattr(wb.urllib.request, "urlopen", fake_urlopen)
    r = a.submit_order(
        client_order_id="x1", symbol="SPY", qty=1, side="buy",
    )
    assert r["ok"] is False
    assert "network down" in r["error"]


# ---- BrokerAdapter ABC compliance ----
def test_webull_adapter_implements_broker_adapter() -> None:
    from trading_bot.ingest.broker_adapter import BrokerAdapter
    assert issubclass(WebullAdapter, BrokerAdapter)


def test_alpaca_adapter_implements_broker_adapter() -> None:
    from trading_bot.ingest.alpaca_adapter import AlpacaAdapter
    from trading_bot.ingest.broker_adapter import BrokerAdapter
    assert issubclass(AlpacaAdapter, BrokerAdapter)


# ---- Crypto IOC override ----
def test_crypto_orders_use_ioc(monkeypatch) -> None:
    a = WebullAdapter(_creds(), enable_submit=True)
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        path = req.full_url.split("api.example", 1)[1].split("?", 1)[0]
        if path == "/auth/v1/token":
            return _FakeResp(b'{"access_token":"tok","expires_in":3600}')
        if path == "/trade/v1/order":
            import json as _json
            captured["body"] = _json.loads((req.data or b"{}").decode())
            return _FakeResp(b'{"order_id":"C1","status":"SUBMITTED"}')
        return _FakeResp(b"{}")

    import trading_bot.ingest.webull_adapter as wb
    monkeypatch.setattr(wb.urllib.request, "urlopen", fake_urlopen)
    a.submit_order(
        client_order_id="x", symbol="BTC/USD", qty=0.001, side="buy",
        asset_class="crypto", time_in_force="day",
    )
    assert captured["body"]["time_in_force"] == "IOC"
    assert captured["body"]["symbol"] == "BTCUSDT"
    assert captured["body"]["asset_class"] == "crypto"
