"""SEC EDGAR intel feed — offline tests with a stubbed urlopen."""
from __future__ import annotations

import datetime as dt
import io
import json
from unittest.mock import patch

import pytest

from trading_bot.ingest.intel import IntelUnavailable
from trading_bot.ingest.intel.edgar import EdgarFeed


def _resp(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def _payload(forms, dates):
    return {
        "filings": {
            "recent": {"form": forms, "filingDate": dates},
        },
    }


def test_counts_recent_filings_within_window() -> None:
    """Filings inside the lookback window count; older ones don't."""
    feed = EdgarFeed(
        entities={"SPY": "0000884394"},
        lookback_days=7,
        user_agent="trading-bot-test contact@example.com",
    )
    # decision_date=2026-05-15, lookback=7 → cutoff exclusive >2026-05-08
    fake = _payload(
        forms=["8-K", "8-K", "10-Q", "8-K/A"],
        dates=["2026-05-14", "2026-05-09", "2026-05-12", "2026-05-01"],
    )
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        records = feed.fetch(dt.date(2026, 5, 15))
    # 8-K on 5-14 + 8-K on 5-09 → both in (5-08, 5-15], 10-Q excluded by form,
    # 8-K/A on 5-01 too old.
    assert len(records) == 1
    rec = next(iter(records.values()))
    assert rec.value == 2.0
    assert rec.source_ts == "2026-05-14"   # latest in-window date
    assert rec.unit == "count"


def test_returns_zero_when_no_recent_filings() -> None:
    feed = EdgarFeed(
        entities={"TLT": "0001100663"}, lookback_days=7,
        user_agent="trading-bot-test contact@example.com",
    )
    fake = _payload(forms=["10-K"], dates=["2025-12-01"])
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        records = feed.fetch(dt.date(2026, 5, 15))
    rec = next(iter(records.values()))
    assert rec.value == 0.0
    # source_ts falls back to decision_date when no in-window filing.
    assert rec.source_ts == "2026-05-15"


def test_unavailable_when_no_user_agent(monkeypatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    feed = EdgarFeed(entities={"SPY": "0000884394"})
    with pytest.raises(IntelUnavailable):
        feed.fetch(dt.date(2026, 5, 15))


def test_reads_user_agent_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "trading-bot test@example.com")
    feed = EdgarFeed(entities={"SPY": "0000884394"})
    fake = _payload(forms=[], dates=[])
    with patch("urllib.request.urlopen", return_value=_resp(fake)) as mock_uo:
        feed.fetch(dt.date(2026, 5, 15))
    req = mock_uo.call_args[0][0]
    # urlopen received a Request with the UA header attached.
    assert req.get_header("User-agent") == "trading-bot test@example.com"


def test_unavailable_on_network_error() -> None:
    feed = EdgarFeed(
        entities={"SPY": "0000884394"},
        user_agent="trading-bot-test contact@example.com",
    )
    def _raise(*a, **kw):
        raise OSError("connection refused")
    with patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date(2026, 5, 15))


def test_unavailable_when_malformed_recent_block() -> None:
    """Forms list != dates list is a parse-time invariant — surface
    immediately rather than silently miscounting."""
    feed = EdgarFeed(
        entities={"SPY": "0000884394"},
        user_agent="trading-bot-test contact@example.com",
    )
    fake = _payload(forms=["8-K", "8-K"], dates=["2026-05-14"])
    with patch("urllib.request.urlopen", return_value=_resp(fake)):
        with pytest.raises(IntelUnavailable):
            feed.fetch(dt.date(2026, 5, 15))


def test_source_hash_changes_when_count_changes() -> None:
    feed = EdgarFeed(
        entities={"SPY": "0000884394"}, lookback_days=7,
        user_agent="trading-bot-test contact@example.com",
    )
    a = _payload(forms=["8-K"], dates=["2026-05-14"])
    b = _payload(
        forms=["8-K", "8-K"], dates=["2026-05-14", "2026-05-13"],
    )
    with patch("urllib.request.urlopen", return_value=_resp(a)):
        r1 = next(iter(feed.fetch(dt.date(2026, 5, 15)).values()))
    with patch("urllib.request.urlopen", return_value=_resp(b)):
        r2 = next(iter(feed.fetch(dt.date(2026, 5, 15)).values()))
    assert r1.source_hash != r2.source_hash
