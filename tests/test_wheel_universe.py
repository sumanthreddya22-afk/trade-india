# tests/test_wheel_universe.py
import datetime as dt
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.options.wheel_universe import filter_universe, UniverseInputs
from trading_bot.config import WheelConfig
from trading_bot.state_db import Base, WheelUniverseCache
from trading_bot.intelligence_finnhub import CompanyProfile


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'u.db'}")
    Base.metadata.create_all(e)
    return e


def _profile(symbol, mcap, ipo):
    return CompanyProfile(symbol=symbol, market_cap_musd=mcap,
                          ipo_date=dt.date.fromisoformat(ipo), exchange="NASDAQ")


def test_passes_when_all_filters_satisfied(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("AAPL", 3_000_000.0, "1980-12-12")
    inputs = UniverseInputs(
        candidates=["AAPL"], optionable_set={"AAPL"},
        avg_dollar_volume_50d={"AAPL": 5_000_000_000.0},
        avg_option_volume_30d={"AAPL": 100_000},
        finnhub=fin, blocklist=set(), allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert out == {"AAPL"}


def test_blocked_by_market_cap(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("XYZ", 5_000.0, "2024-01-01")  # $5M cap
    inputs = UniverseInputs(
        candidates=["XYZ"], optionable_set={"XYZ"},
        avg_dollar_volume_50d={"XYZ": 100_000_000.0},
        avg_option_volume_30d={"XYZ": 50_000},
        finnhub=fin, blocklist=set(), allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert "XYZ" not in out


def test_blocklist_overrides_pass(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("AAPL", 3_000_000.0, "1980-12-12")
    inputs = UniverseInputs(
        candidates=["AAPL"], optionable_set={"AAPL"},
        avg_dollar_volume_50d={"AAPL": 5_000_000_000.0},
        avg_option_volume_30d={"AAPL": 100_000},
        finnhub=fin, blocklist={"AAPL"}, allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert "AAPL" not in out


def test_allowlist_forces_inclusion_even_if_filters_fail(engine):
    fin = MagicMock()
    fin.company_profile.return_value = _profile("ZZZ", 1_000.0, "2025-01-01")
    inputs = UniverseInputs(
        candidates=["ZZZ"], optionable_set={"ZZZ"},
        avg_dollar_volume_50d={"ZZZ": 10_000_000.0},
        avg_option_volume_30d={"ZZZ": 1_000},
        finnhub=fin, blocklist=set(), allowlist={"ZZZ"},
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28))
    assert out == {"ZZZ"}


def test_cache_hit_skips_recomputation(engine):
    cached_at = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(WheelUniverseCache(symbol="AAPL", eligible=True, reason="", cached_at=cached_at))
        s.add(WheelUniverseCache(symbol="MSFT", eligible=False, reason="market_cap", cached_at=cached_at))
        s.commit()
    fin = MagicMock()  # never called
    inputs = UniverseInputs(
        candidates=["AAPL", "MSFT"], optionable_set={"AAPL", "MSFT"},
        avg_dollar_volume_50d={}, avg_option_volume_30d={},
        finnhub=fin, blocklist=set(), allowlist=set(),
    )
    out = filter_universe(inputs, cfg=WheelConfig(enabled=True), engine=engine,
                          today=dt.date(2026, 4, 28), use_cache=True)
    assert out == {"AAPL"}
    fin.company_profile.assert_not_called()
