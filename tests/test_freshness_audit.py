"""Tests for the daily freshness audit (reports stale caches in the digest)."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from trading_bot.freshness_audit import (
    FreshnessFinding, audit_freshness, render_text_summary,
)


@pytest.fixture
def fresh_dbs(tmp_path: Path, monkeypatch):
    """Build minimal in-tmp DBs with one row each, freshly stamped."""
    state = tmp_path / "state.db"
    news = tmp_path / "news.db"
    massive = tmp_path / "massive.db"

    now = dt.datetime(2026, 4, 30, 1, 0, tzinfo=dt.timezone.utc)
    fresh_ts = (now - dt.timedelta(hours=1)).isoformat()

    with sqlite3.connect(state) as c:
        c.execute("CREATE TABLE wheel_universe_cache (symbol TEXT, cached_at TEXT)")
        c.execute("INSERT INTO wheel_universe_cache VALUES (?,?)", ("X", fresh_ts))
        c.execute("CREATE TABLE option_iv_history (symbol TEXT, recorded_at TEXT)")
        c.execute("INSERT INTO option_iv_history VALUES (?,?)", ("X", fresh_ts))
        c.commit()

    with sqlite3.connect(news) as c:
        c.execute("CREATE TABLE news_sentiment (symbol TEXT, cached_at TEXT)")
        c.execute("INSERT INTO news_sentiment VALUES (?,?)", ("X", fresh_ts))
        c.commit()

    with sqlite3.connect(massive) as c:
        c.execute("CREATE TABLE grouped_bars (trade_date TEXT)")
        c.execute(
            "INSERT INTO grouped_bars VALUES (?)",
            ((now - dt.timedelta(days=1)).date().isoformat(),),
        )
        c.commit()

    monkeypatch.setattr(
        "trading_bot.freshness_audit._CACHE_CHECKS",
        (
            ("wheel_universe_cache", state,
             "SELECT MAX(cached_at) FROM wheel_universe_cache", 24.0 * 14, "n"),
            ("option_iv_history", state,
             "SELECT MAX(recorded_at) FROM option_iv_history", 26.0, "n"),
            ("news_sentiment", news,
             "SELECT MAX(cached_at) FROM news_sentiment", 24.0, "n"),
            ("massive_grouped", massive,
             "SELECT MAX(trade_date) FROM grouped_bars", 72.0, "n"),
        ),
    )
    return now


def test_all_fresh_returns_ok(fresh_dbs):
    findings = audit_freshness(now=fresh_dbs)
    assert all(f.severity == "ok" for f in findings), [
        (f.cache, f.severity, f.age_hours) for f in findings
    ]


def test_stale_cache_flagged(tmp_path: Path, monkeypatch):
    """A cache older than its budget produces severity='stale'."""
    state = tmp_path / "state.db"
    now = dt.datetime(2026, 4, 30, 1, 0, tzinfo=dt.timezone.utc)
    stale_ts = (now - dt.timedelta(hours=200)).isoformat()
    with sqlite3.connect(state) as c:
        c.execute("CREATE TABLE option_iv_history (symbol TEXT, recorded_at TEXT)")
        c.execute("INSERT INTO option_iv_history VALUES (?,?)", ("X", stale_ts))
        c.commit()
    monkeypatch.setattr(
        "trading_bot.freshness_audit._CACHE_CHECKS",
        (
            ("option_iv_history", state,
             "SELECT MAX(recorded_at) FROM option_iv_history", 26.0, "n"),
        ),
    )
    findings = audit_freshness(now=now)
    assert findings[0].severity == "stale"
    assert findings[0].age_hours > 26.0


def test_missing_db_flagged(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "trading_bot.freshness_audit._CACHE_CHECKS",
        (
            ("nope", tmp_path / "absent.db",
             "SELECT MAX(x) FROM nope", 24.0, "n"),
        ),
    )
    findings = audit_freshness()
    assert findings[0].severity == "missing"


def test_render_summary_includes_each_cache(fresh_dbs):
    findings = audit_freshness(now=fresh_dbs)
    text = render_text_summary(findings)
    for f in findings:
        assert f.cache in text
    assert "Worst: ok" in text
