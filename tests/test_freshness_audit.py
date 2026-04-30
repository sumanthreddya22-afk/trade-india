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


def _stub_opportunities(tmp_path: Path, monkeypatch, age_hours: float = 1.0,
                        now: dt.datetime | None = None) -> Path:
    """Bucket B: write a fresh opportunities.md so freshness tests see ``ok``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    md = tmp_path / "opportunities.md"
    ts = (now - dt.timedelta(hours=age_hours)).isoformat(timespec="seconds")
    md.write_text(f"# Opportunities\n\nGenerated: {ts}\nTotal endorsed: 0\n")
    monkeypatch.setattr("trading_bot.freshness_audit._OPPORTUNITIES_PATH", md)
    return md


@pytest.fixture
def fresh_dbs(tmp_path: Path, monkeypatch):
    """Build minimal in-tmp DBs with one row each, freshly stamped."""
    state = tmp_path / "state.db"
    news = tmp_path / "news.db"
    massive = tmp_path / "massive.db"

    now = dt.datetime(2026, 4, 30, 1, 0, tzinfo=dt.timezone.utc)
    fresh_ts = (now - dt.timedelta(hours=1)).isoformat()
    _stub_opportunities(tmp_path, monkeypatch, age_hours=1.0, now=now)

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


def _by_cache(findings, cache: str):
    """Helper: pick a finding by cache name (post-Bucket-B opportunities_md
    is added to the audit list, so positional indexing is brittle)."""
    matches = [f for f in findings if f.cache == cache]
    assert matches, f"no finding for cache {cache}; got {[f.cache for f in findings]}"
    return matches[0]


def test_stale_cache_flagged(tmp_path: Path, monkeypatch):
    """A cache older than its budget produces severity='stale'."""
    state = tmp_path / "state.db"
    now = dt.datetime(2026, 4, 30, 1, 0, tzinfo=dt.timezone.utc)
    stale_ts = (now - dt.timedelta(hours=200)).isoformat()
    with sqlite3.connect(state) as c:
        c.execute("CREATE TABLE option_iv_history (symbol TEXT, recorded_at TEXT)")
        c.execute("INSERT INTO option_iv_history VALUES (?,?)", ("X", stale_ts))
        c.commit()
    _stub_opportunities(tmp_path, monkeypatch, age_hours=1.0, now=now)
    monkeypatch.setattr(
        "trading_bot.freshness_audit._CACHE_CHECKS",
        (
            ("option_iv_history", state,
             "SELECT MAX(recorded_at) FROM option_iv_history", 26.0, "n"),
        ),
    )
    findings = audit_freshness(now=now)
    f = _by_cache(findings, "option_iv_history")
    assert f.severity == "stale"
    assert f.age_hours > 26.0


def test_missing_db_flagged(tmp_path: Path, monkeypatch):
    _stub_opportunities(tmp_path, monkeypatch, age_hours=1.0)
    monkeypatch.setattr(
        "trading_bot.freshness_audit._CACHE_CHECKS",
        (
            ("nope", tmp_path / "absent.db",
             "SELECT MAX(x) FROM nope", 24.0, "n"),
        ),
    )
    findings = audit_freshness()
    f = _by_cache(findings, "nope")
    assert f.severity == "missing"


def test_opportunities_md_stale_flagged(tmp_path: Path, monkeypatch):
    """Bucket B: stale opportunities.md surfaces as a freshness finding."""
    _stub_opportunities(tmp_path, monkeypatch, age_hours=24.0)
    monkeypatch.setattr("trading_bot.freshness_audit._CACHE_CHECKS", ())
    f = _by_cache(audit_freshness(), "opportunities_md")
    assert f.severity == "stale"
    assert f.age_hours > 12.0


def test_opportunities_md_missing_flagged(tmp_path: Path, monkeypatch):
    """Bucket B: a missing opportunities.md is a missing-cache finding."""
    monkeypatch.setattr(
        "trading_bot.freshness_audit._OPPORTUNITIES_PATH",
        tmp_path / "absent.md",
    )
    monkeypatch.setattr("trading_bot.freshness_audit._CACHE_CHECKS", ())
    f = _by_cache(audit_freshness(), "opportunities_md")
    assert f.severity == "missing"


def test_render_summary_includes_each_cache(fresh_dbs):
    findings = audit_freshness(now=fresh_dbs)
    text = render_text_summary(findings)
    for f in findings:
        assert f.cache in text
    assert "Worst: ok" in text
