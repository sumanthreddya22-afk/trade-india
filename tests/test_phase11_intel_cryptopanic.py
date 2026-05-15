"""CryptoPanic intel feed — offline tests with a stubbed urlopen."""
from __future__ import annotations

import datetime as dt
import io
import json
from unittest.mock import patch

import pytest

from trading_bot.ingest.intel import IntelUnavailable
from trading_bot.ingest.intel.cryptopanic import CryptoPanicFeed


def _resp(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def _post(published_at: str, currencies: list[str]) -> dict:
    return {
        "published_at": published_at,
        "currencies": [{"code": c} for c in currencies],
    }


def test_counts_posts_within_window() -> None:
    """Posts in the last window_hours count; older posts don't."""
    feed = CryptoPanicFeed(
        currencies=("BTC", "ETH"), window_hours=24,
    )
    now = dt.datetime.now(dt.timezone.utc)
    fake = {"results": [
        _post((now - dt.timedelta(hours=1)).isoformat(), ["BTC"]),
        _post((now - dt.timedelta(hours=12)).isoformat(), ["BTC", "ETH"]),
        _post((now - dt.timedelta(hours=30)).isoformat(), ["BTC"]),  # too old
        _post((now - dt.timedelta(hours=6)).isoformat(), ["ETH"]),
    ]}
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        records = feed.fetch(dt.date.today())
    assert records["BTC_news_24H"].value == 2.0
    assert records["ETH_news_24H"].value == 2.0
    assert records["BTC_news_24H"].unit == "count"


def test_returns_zero_when_no_matching_posts() -> None:
    feed = CryptoPanicFeed(currencies=("BTC",), window_hours=24)
    fake = {"results": []}
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        records = feed.fetch(dt.date.today())
    assert records["BTC_news_24H"].value == 0.0


def test_zulu_timestamps_are_parsed() -> None:
    """CryptoPanic emits ``...Z`` timestamps; the feed must accept
    them without raising."""
    feed = CryptoPanicFeed(currencies=("BTC",), window_hours=24)
    now = dt.datetime.now(dt.timezone.utc)
    zulu = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = {"results": [{"published_at": zulu,
                          "currencies": [{"code": "BTC"}]}]}
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        records = feed.fetch(dt.date.today())
    assert records["BTC_news_24H"].value == 1.0


def test_unavailable_on_network_error() -> None:
    feed = CryptoPanicFeed(currencies=("BTC",))
    def _raise(*a, **kw):
        raise OSError("connection refused")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date.today())


def test_unavailable_on_non_json_response() -> None:
    feed = CryptoPanicFeed(currencies=("BTC",))
    with patch(
        "urllib.request.urlopen",
        return_value=io.BytesIO(b"<html>rate limited</html>"),
    ):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date.today())


def test_source_hash_changes_when_count_changes() -> None:
    feed = CryptoPanicFeed(currencies=("BTC",), window_hours=24)
    now = dt.datetime.now(dt.timezone.utc)
    a = {"results": [_post((now - dt.timedelta(hours=1)).isoformat(),
                            ["BTC"])]}
    b = {"results": [
        _post((now - dt.timedelta(hours=1)).isoformat(), ["BTC"]),
        _post((now - dt.timedelta(hours=2)).isoformat(), ["BTC"]),
    ]}
    with patch("urllib.request.urlopen", return_value=_resp(a)):
        r1 = feed.fetch(dt.date.today())["BTC_news_24H"]
    with patch("urllib.request.urlopen", return_value=_resp(b)):
        r2 = feed.fetch(dt.date.today())["BTC_news_24H"]
    assert r1.source_hash != r2.source_hash
