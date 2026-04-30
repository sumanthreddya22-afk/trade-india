"""Tests for the nightly wheel-universe-builder. Discovers wheel-eligible
symbols from Alpaca's optionable universe + Finnhub quality filters; writes
to wheel_universe_cache. Operator YAMLs are override-only."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.options.wheel_universe_builder import (
    BuilderDeps, run_universe_build, _is_etf,
)
from trading_bot.intelligence_finnhub import CompanyProfile, FinnhubUnavailable
from trading_bot.state_db import Base, WheelUniverseCache


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'b.db'}")
    Base.metadata.create_all(e)
    return e


def _profile(*, market_cap_musd, ipo="1980-01-01", exchange="NASDAQ"):
    return CompanyProfile(
        symbol="X", market_cap_musd=market_cap_musd,
        ipo_date=dt.date.fromisoformat(ipo) if ipo else None,
        exchange=exchange,
    )


def _deps(engine, *, optionable, finnhub=None, blocklist=None, allowlist=None,
          today=None):
    fin = finnhub or MagicMock()
    return BuilderDeps(
        engine=engine,
        optionable_set=set(optionable),
        finnhub=fin,
        blocklist=set(blocklist or []),
        allowlist=set(allowlist or []),
        today=today or dt.date(2026, 4, 29),
        rate_delay_s=0.0,  # no sleep in tests
    )


def test_passes_quality_filters_writes_eligible_true(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile(market_cap_musd=50_000.0)  # $50B
    deps = _deps(engine, optionable={"AAPL"}, finnhub=fin)
    n = run_universe_build(deps)
    assert n["eligible"] == 1 and n["rejected"] == 0
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="AAPL").one()
    assert row.eligible is True


def test_rejects_market_cap_below_threshold(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile(market_cap_musd=2_000.0)  # $2B
    deps = _deps(engine, optionable={"SMOL"}, finnhub=fin)
    run_universe_build(deps)
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="SMOL").one()
    assert row.eligible is False
    assert "market_cap" in row.reason


def test_rejects_recently_ipoed(engine):
    fin = MagicMock()
    # Two-year-old IPO at $20B mkt cap — too new for the wheel
    fin.company_profile.return_value = _profile(
        market_cap_musd=20_000.0, ipo="2024-04-29",
    )
    deps = _deps(engine, optionable={"FRESH"}, finnhub=fin,
                 today=dt.date(2026, 4, 29))
    run_universe_build(deps)
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="FRESH").one()
    assert row.eligible is False
    assert "listing_age" in row.reason


def test_etfs_pass_without_market_cap(engine):
    """SPY/QQQ/XLK have no market_cap_musd in Finnhub; ETF detection lets them through."""
    fin = MagicMock()
    fin.company_profile.return_value = CompanyProfile(
        symbol="SPY", market_cap_musd=None, ipo_date=None, exchange="ARCA")
    deps = _deps(engine, optionable={"SPY"}, finnhub=fin)
    run_universe_build(deps)
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="SPY").one()
    assert row.eligible is True


def test_blocklist_marks_ineligible_without_finnhub_call(engine):
    fin = MagicMock()
    deps = _deps(engine, optionable={"BAD"}, finnhub=fin, blocklist={"BAD"})
    run_universe_build(deps)
    fin.company_profile.assert_not_called()
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="BAD").one()
    assert row.eligible is False and row.reason == "blocklist"


def test_allowlist_forces_eligible_even_when_finnhub_filter_fails(engine):
    fin = MagicMock()
    # Tiny market cap → would normally fail
    fin.company_profile.return_value = _profile(market_cap_musd=500.0)
    deps = _deps(engine, optionable={"SPECIAL"}, finnhub=fin,
                 allowlist={"SPECIAL"})
    run_universe_build(deps)
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="SPECIAL").one()
    assert row.eligible is True
    assert row.reason == "allowlist"


def test_finnhub_unavailable_caches_unknown_for_retry(engine):
    fin = MagicMock()
    fin.company_profile.side_effect = FinnhubUnavailable("rate limit")
    deps = _deps(engine, optionable={"X", "Y"}, finnhub=fin)
    out = run_universe_build(deps)
    # Both end up cached as ineligible/finnhub_unavailable so we don't
    # block other names but try again next run
    assert out["unavailable"] == 2
    with Session(engine) as s:
        rows = s.query(WheelUniverseCache).all()
    assert all(r.eligible is False and r.reason == "finnhub_unavailable" for r in rows)


def test_finnhub_unavailable_uses_short_retry_backoff(engine):
    """Bucket C: a Finnhub outage must NOT poison the cache for 14 days.

    The unavailable branch stamps cached_at well in the past so the next
    nightly build re-checks the symbol within ~24h instead of waiting two
    weeks for the TTL to expire.
    """
    fin = MagicMock()
    fin.company_profile.side_effect = FinnhubUnavailable("rate limit")
    deps = _deps(engine, optionable={"AAPL"}, finnhub=fin)
    run_universe_build(deps)
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="AAPL").one()
    cached_at = row.cached_at
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - cached_at).days
    # Backoff stamps cached_at ~13 days back so a 14d TTL re-checks tomorrow.
    assert 12 <= age <= 14


def test_unhealthy_build_emits_alert(engine, monkeypatch):
    """Bucket C: a build with eligible=0 fires a daemon_critical alert."""
    captured = []
    monkeypatch.setattr(
        "trading_bot.options.wheel_universe_builder.queue_alert",
        lambda ev: captured.append(ev),
    )
    fin = MagicMock()
    fin.company_profile.side_effect = FinnhubUnavailable("rate limit")
    deps = _deps(engine, optionable={"AAPL", "MSFT", "GOOG"}, finnhub=fin)
    run_universe_build(deps)
    assert any(ev.kind == "daemon_critical" for ev in captured), [
        (ev.kind, ev.title) for ev in captured
    ]


def test_healthy_build_does_not_alert(engine, monkeypatch):
    """Bucket C: an all-eligible build (well above the floor) emits no alert."""
    captured = []
    monkeypatch.setattr(
        "trading_bot.options.wheel_universe_builder.queue_alert",
        lambda ev: captured.append(ev),
    )
    fin = MagicMock()
    fin.company_profile.return_value = _profile(market_cap_musd=50_000.0)
    # 60 names — comfortably above the 50-name floor.
    universe = {f"X{i:02d}" for i in range(60)}
    deps = _deps(engine, optionable=universe, finnhub=fin)
    run_universe_build(deps)
    assert captured == []


def test_skips_recently_cached_symbols(engine):
    """Builder respects the 14d cache TTL — symbols cached <14d ago aren't
    re-queried (saves Finnhub quota)."""
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    with Session(engine) as s:
        s.add(WheelUniverseCache(symbol="AAPL", eligible=True, reason="ok",
                                 cached_at=recent))
        s.commit()
    fin = MagicMock()
    deps = _deps(engine, optionable={"AAPL"}, finnhub=fin)
    out = run_universe_build(deps)
    fin.company_profile.assert_not_called()
    assert out["cached_skip"] == 1


def test_refreshes_stale_cache_entries(engine):
    """Cache entries older than 14d are re-evaluated."""
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
    with Session(engine) as s:
        s.add(WheelUniverseCache(symbol="AAPL", eligible=False,
                                 reason="market_cap", cached_at=stale))
        s.commit()
    fin = MagicMock()
    fin.company_profile.return_value = _profile(market_cap_musd=200_000.0)
    deps = _deps(engine, optionable={"AAPL"}, finnhub=fin)
    out = run_universe_build(deps)
    assert out["eligible"] == 1
    fin.company_profile.assert_called_once()


def test_existing_eligible_falls_out_when_no_longer_optionable(engine):
    """If a name is no longer in the optionable set (e.g. delisted),
    its cache entry flips to ineligible immediately, regardless of TTL."""
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    with Session(engine) as s:
        s.add(WheelUniverseCache(symbol="GONE", eligible=True, reason="ok",
                                 cached_at=recent))
        s.commit()
    fin = MagicMock()
    deps = _deps(engine, optionable=set(), finnhub=fin)
    run_universe_build(deps)
    with Session(engine) as s:
        row = s.query(WheelUniverseCache).filter_by(symbol="GONE").one()
    assert row.eligible is False
    assert row.reason == "no_longer_optionable"


def test_is_etf_detects_known_sector_etf():
    """ETF detection: if the name appears in our static ETF map (SPY/QQQ/XL*)
    or has no market_cap_musd, treat as ETF."""
    assert _is_etf("SPY", _profile(market_cap_musd=None)) is True
    assert _is_etf("XLK", _profile(market_cap_musd=None)) is True
    # Equity with market cap → not an ETF
    assert _is_etf("AAPL", _profile(market_cap_musd=2_000_000.0)) is False
