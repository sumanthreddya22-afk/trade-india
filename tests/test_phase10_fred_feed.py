"""FRED intel feed — offline tests with a stubbed urlopen."""
from __future__ import annotations

import datetime as dt
import io
import json
from unittest.mock import patch

import pytest

from trading_bot.ingest.intel import IntelUnavailable
from trading_bot.ingest.intel.fred import FredFeed


def _resp(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def test_returns_record_for_each_series() -> None:
    feed = FredFeed(series=("VIXCLS", "DGS10"), api_key="testkey")
    fake = {"observations": [{"date": "2026-05-14", "value": "14.20"}]}
    with patch(
        "urllib.request.urlopen",
        side_effect=lambda *a, **kw: _resp(fake),
    ) as mock_uo:
        records = feed.fetch(dt.date(2026, 5, 15))
    assert set(records.keys()) == {"VIXCLS", "DGS10"}
    vix = records["VIXCLS"]
    assert vix.value == 14.20
    assert vix.unit == "index"
    assert vix.series_id == "VIXCLS"
    assert vix.source_ts == "2026-05-14"
    assert vix.source_hash  # deterministic
    # Two series → two calls
    assert mock_uo.call_count == 2


def test_unavailable_when_observations_empty() -> None:
    feed = FredFeed(series=("DGS10",), api_key="k")
    with patch("urllib.request.urlopen", return_value=_resp({"observations": []})):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date(2026, 5, 15))


def test_unavailable_when_sentinel_value() -> None:
    feed = FredFeed(series=("DGS10",), api_key="k")
    fake = {"observations": [{"date": "2026-05-14", "value": "."}]}
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date(2026, 5, 15))


def test_unavailable_on_network_error() -> None:
    feed = FredFeed(series=("DGS10",), api_key="k")
    def _raise(*a, **kw):
        raise OSError("connection refused")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date(2026, 5, 15))


def test_source_hash_changes_when_value_changes() -> None:
    feed = FredFeed(series=("VIXCLS",), api_key="k")
    a = {"observations": [{"date": "2026-05-14", "value": "14.0"}]}
    b = {"observations": [{"date": "2026-05-14", "value": "20.0"}]}
    with patch("urllib.request.urlopen", return_value=_resp(a)):
        r1 = feed.fetch(dt.date(2026, 5, 15))["VIXCLS"]
    with patch("urllib.request.urlopen", return_value=_resp(b)):
        r2 = feed.fetch(dt.date(2026, 5, 15))["VIXCLS"]
    assert r1.source_hash != r2.source_hash
