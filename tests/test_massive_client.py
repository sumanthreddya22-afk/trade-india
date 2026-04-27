"""Tests for the rate-limit handling in MassiveClient. The HTTP layer
is mocked so tests don't hit Polygon."""
from __future__ import annotations

import time

import pytest

from trading_bot.massive_client import (
    BACKOFF_SCHEDULE,
    MIN_CALL_INTERVAL_S,
    MassiveClient,
    MassiveRateLimitError,
)


class _FakeResp:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_throttle_spaces_consecutive_calls(monkeypatch):
    """Two consecutive calls on the same client instance must be at
    least MIN_CALL_INTERVAL_S apart."""
    client = MassiveClient(api_key="test")

    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.massive_client.time.sleep", lambda s: sleeps.append(s))

    monkeypatch.setattr(
        "trading_bot.massive_client.requests.get",
        lambda *a, **kw: _FakeResp(200, {"results": []}),
    )
    client._get("/foo")
    client._last_call_at = time.monotonic() - 1.0
    client._get("/foo")
    assert any(s >= MIN_CALL_INTERVAL_S - 1.5 for s in sleeps), f"sleeps={sleeps}"


def test_backoff_retries_on_429_then_succeeds(monkeypatch):
    """After a 429, sleep per backoff schedule and retry; should not raise."""
    client = MassiveClient(api_key="test")

    sleeps: list[float] = []
    monkeypatch.setattr("trading_bot.massive_client.time.sleep", lambda s: sleeps.append(s))

    responses = iter([_FakeResp(429), _FakeResp(200, {"results": []})])
    monkeypatch.setattr(
        "trading_bot.massive_client.requests.get",
        lambda *a, **kw: next(responses),
    )

    r = client._get("/foo")
    assert r.status_code == 200
    assert BACKOFF_SCHEDULE[0] in sleeps


def test_backoff_exhausts_then_raises(monkeypatch):
    client = MassiveClient(api_key="test")
    monkeypatch.setattr("trading_bot.massive_client.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "trading_bot.massive_client.requests.get",
        lambda *a, **kw: _FakeResp(429),
    )
    with pytest.raises(MassiveRateLimitError):
        client._get("/foo")
