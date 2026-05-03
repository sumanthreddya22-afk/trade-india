"""Tests for the Binance geo-block latch.

Binance returns HTTP 451 to US IPs. The block is permanent for the
process lifetime, so every poll re-hitting it floods stderr with
identical warnings. The latch logs once and short-circuits subsequent
polls until daemon restart.
"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_stream_latch():
    from trading_bot.pipelines.crypto.streams import binance_funding_stream
    binance_funding_stream._GEO_BLOCKED = False
    yield
    binance_funding_stream._GEO_BLOCKED = False


@pytest.fixture(autouse=True)
def _reset_source_latch():
    from trading_bot.pipelines.crypto.sources import binance_funding
    binance_funding._GEO_BLOCKED = False
    yield
    binance_funding._GEO_BLOCKED = False


# ---------------------------------------------------------------------------
# Stream-level (poll_binance_funding) — fires every minute
# ---------------------------------------------------------------------------


def test_stream_first_451_logs_info_and_latches(caplog):
    from trading_bot.pipelines.crypto.streams.binance_funding_stream import (
        poll_binance_funding, _GEO_BLOCKED,
    )

    def boom():
        raise RuntimeError("451 Client Error: for url https://fapi.binance.com/...")

    caplog.set_level(logging.INFO,
                     logger="trading_bot.pipelines.crypto.streams.binance_funding_stream")
    n = poll_binance_funding(engine=None, fetcher=boom)
    assert n == 0
    assert any("US geo-block" in r.getMessage() for r in caplog.records), (
        "first 451 must log a one-line INFO explaining the suppression"
    )
    assert all(r.levelno != logging.WARNING for r in caplog.records), (
        "must NOT log a WARNING — that's what we're trying to silence"
    )

    # Subsequent polls don't even invoke the fetcher
    calls = []
    def counted():
        calls.append(1)
        return []
    caplog.clear()
    n = poll_binance_funding(engine=None, fetcher=counted)
    assert n == 0
    assert not calls, "latch must short-circuit before calling fetcher again"
    assert not caplog.records, "no further log lines on subsequent polls"


def test_stream_non_geo_failure_still_warns_and_does_not_latch(caplog):
    from trading_bot.pipelines.crypto.streams import binance_funding_stream
    from trading_bot.pipelines.crypto.streams.binance_funding_stream import (
        poll_binance_funding,
    )

    def timeout():
        raise RuntimeError("Read timed out")

    caplog.set_level(logging.WARNING)
    n = poll_binance_funding(engine=None, fetcher=timeout)
    assert n == 0
    assert any("Read timed out" in r.getMessage() for r in caplog.records)
    assert not binance_funding_stream._GEO_BLOCKED, (
        "non-451 failures must NOT latch — they may be transient"
    )


# ---------------------------------------------------------------------------
# Source-level (binance_funding.collect_binance_funding) — fires every 30m
# ---------------------------------------------------------------------------


def test_source_451_latches_and_returns_empty_extra(caplog):
    from trading_bot.pipelines.crypto.sources.binance_funding import (
        collect_binance_funding,
    )

    def boom(_targets):
        raise RuntimeError("451 Client Error: for url https://fapi.binance.com/...")

    caplog.set_level(logging.INFO,
                     logger="trading_bot.pipelines.crypto.sources.binance_funding")
    result = collect_binance_funding(engine=None, fetcher=boom)
    assert result.error is None or result.error == "", (
        "geo-block is not an error — operator should not see it as a failure"
    )
    assert result.extra == {"reason": "geo_blocked"}

    calls = []
    def counted(_t):
        calls.append(1)
        return []
    caplog.clear()
    result = collect_binance_funding(engine=None, fetcher=counted)
    assert not calls, "latch must short-circuit on subsequent calls"
    assert result.extra == {"reason": "geo_blocked"}
