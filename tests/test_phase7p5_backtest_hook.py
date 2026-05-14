"""Backtest hook — stub safety + deterministic output."""
from __future__ import annotations

import pytest

from trading_bot.research.backtest_hook import StubBacktest


def test_stub_disabled_by_default():
    with pytest.raises(RuntimeError):
        StubBacktest()


def test_stub_enabled_with_env(monkeypatch):
    monkeypatch.setenv("TRADING_BOT_ALLOW_STUB_BACKTEST", "1")
    stub = StubBacktest()

    class FakeCandidate:
        candidate_id = "MR.LOOKBACK=20;MR.ZSCORE_ENTRY=2.0"

    p, sanity = stub(FakeCandidate())
    assert 0.0001 <= p <= 0.9999
    assert sanity["data_window"] == "stub"
    # Determinism: same candidate id → same p.
    p2, _ = stub(FakeCandidate())
    assert p == p2


def test_stub_distinct_candidates_distinct_p(monkeypatch):
    monkeypatch.setenv("TRADING_BOT_ALLOW_STUB_BACKTEST", "1")
    stub = StubBacktest()

    class A: candidate_id = "A"
    class B: candidate_id = "B"

    pa, _ = stub(A())
    pb, _ = stub(B())
    assert pa != pb
