"""Tests for sector-exposure tracking + gating.

The bot's existing risk model caps per-symbol concentration (5%) and per-asset-class
allocation (stocks/crypto/options), but has no sector limit. The wheel allowlist is
~60% tech-correlated. A tech drawdown could assign multiple wheel cycles
simultaneously and concentrate the equity book in one sector. This module adds
a sector-classifier (yfinance + local cache) and a sector-exposure gate."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.sector_exposure import (
    SectorClassifier, SectorExposure, classify_symbol, compute_exposure,
    sector_cap_ok,
)
from trading_bot.state_db import Base, SectorCache


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'sec.db'}")
    Base.metadata.create_all(e)
    return e


def test_classify_returns_yfinance_sector(engine):
    """First lookup hits yfinance, caches result in SectorCache."""
    mock_ticker = MagicMock()
    mock_ticker.info = {"sector": "Technology", "industry": "Software"}
    with patch("yfinance.Ticker", return_value=mock_ticker):
        out = classify_symbol("MSFT", engine)
    assert out == "Technology"
    with Session(engine) as s:
        cached = s.query(SectorCache).filter_by(symbol="MSFT").one()
    assert cached.sector == "Technology"


def test_classify_uses_cache_on_second_call(engine):
    """Second lookup serves from SectorCache without re-hitting yfinance."""
    with Session(engine) as s:
        s.add(SectorCache(symbol="AAPL", sector="Technology",
                          industry="Consumer Electronics",
                          cached_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()
    with patch("yfinance.Ticker") as yf_mock:
        out = classify_symbol("AAPL", engine)
    assert out == "Technology"
    yf_mock.assert_not_called()


def test_classify_returns_unknown_when_yfinance_fails(engine):
    """Network errors must not crash the bot — return 'Unknown' and cache it
    briefly so we don't retry every scan."""
    with patch("yfinance.Ticker", side_effect=Exception("net")):
        out = classify_symbol("XYZ", engine)
    assert out == "Unknown"
    with Session(engine) as s:
        cached = s.query(SectorCache).filter_by(symbol="XYZ").one()
    assert cached.sector == "Unknown"


def test_classify_crypto_pairs_short_circuit(engine):
    """Crypto pairs (BTCUSD, ETHUSD, ...) are classified as 'Crypto' without
    hitting yfinance — that endpoint 404s on this symbol naming."""
    with patch("yfinance.Ticker") as yf_mock:
        out = classify_symbol("BTCUSD", engine)
    assert out == "Crypto"
    yf_mock.assert_not_called()


def test_classify_etfs_use_static_map(engine):
    """ETFs don't have a sector in yfinance's `info`. We map well-known
    sector ETFs explicitly so SPY/QQQ/XLK get classified correctly. Sector
    names MUST match yfinance's terminology to avoid bucket-splitting (XLF
    'Financials' vs JPM 'Financial Services' would be two buckets)."""
    assert classify_symbol("XLK", engine) == "Technology"
    assert classify_symbol("XLF", engine) == "Financial Services"
    assert classify_symbol("SPY", engine) == "Diversified"


def test_compute_exposure_aggregates_by_sector(engine):
    classifier = SectorClassifier(engine)
    classifier._memo = {  # pre-load to avoid yfinance calls
        "AAPL": "Technology", "MSFT": "Technology",
        "JPM": "Financials", "JNJ": "Healthcare",
    }
    positions = [
        MagicMock(symbol="AAPL", market_value=Decimal("10000"), asset_class="us_equity"),
        MagicMock(symbol="MSFT", market_value=Decimal("8000"), asset_class="us_equity"),
        MagicMock(symbol="JPM", market_value=Decimal("5000"), asset_class="us_equity"),
        MagicMock(symbol="JNJ", market_value=Decimal("3000"), asset_class="us_equity"),
    ]
    out = compute_exposure(positions, equity=Decimal("100000"), classifier=classifier)
    assert out["Technology"] == pytest.approx(0.18)  # (10000+8000)/100000
    assert out["Financials"] == pytest.approx(0.05)
    assert out["Healthcare"] == pytest.approx(0.03)


def test_compute_exposure_includes_pending_option_collateral(engine):
    """Option positions (short puts) reserve collateral in their underlying's
    sector, not 'Options'. A short AAPL put concentrates Technology exposure
    via potential assignment."""
    classifier = SectorClassifier(engine)
    classifier._memo = {"AAPL": "Technology"}
    positions = [
        MagicMock(symbol="AAPL250516P00190000",
                  market_value=Decimal("-200"), asset_class="us_option"),
    ]
    out = compute_exposure(
        positions, equity=Decimal("100000"), classifier=classifier,
        option_collateral_by_symbol={"AAPL": Decimal("19000")},
    )
    # Technology = 19000 collateral / 100000 = 19%
    assert out["Technology"] == pytest.approx(0.19)


def test_sector_cap_ok_passes_under_limit(engine):
    classifier = SectorClassifier(engine)
    classifier._memo = {"NEW": "Healthcare"}
    existing = {"Technology": 0.20, "Healthcare": 0.10}
    ok, reason = sector_cap_ok(
        symbol="NEW", prospective_dollars=Decimal("3000"),
        equity=Decimal("100000"), existing_exposure=existing,
        classifier=classifier, cap_pct=0.25,
    )
    assert ok and reason == ""


def test_sector_cap_ok_blocks_when_adding_breaches_cap(engine):
    classifier = SectorClassifier(engine)
    classifier._memo = {"NEW": "Technology"}
    existing = {"Technology": 0.22}  # already 22%
    ok, reason = sector_cap_ok(
        symbol="NEW", prospective_dollars=Decimal("5000"),
        equity=Decimal("100000"), existing_exposure=existing,
        classifier=classifier, cap_pct=0.25,
    )
    assert ok is False
    assert "Technology" in reason and "27" in reason  # 22% + 5%


def test_sector_cap_ok_unknown_sector_doesnt_block(engine):
    """If we can't classify, don't block — the per-symbol cap still applies."""
    classifier = SectorClassifier(engine)
    classifier._memo = {"WEIRD": "Unknown"}
    ok, reason = sector_cap_ok(
        symbol="WEIRD", prospective_dollars=Decimal("3000"),
        equity=Decimal("100000"), existing_exposure={"Unknown": 0.05},
        classifier=classifier, cap_pct=0.25,
    )
    assert ok is True
