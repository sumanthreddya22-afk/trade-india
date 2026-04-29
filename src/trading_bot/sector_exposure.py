"""Sector exposure tracker + concentration gate.

The risk model already caps:
  * per-symbol concentration (5%)
  * per-asset-class allocation (stocks/crypto/options)

But NOT sector. With a wheel allowlist that's ~60% tech-correlated, three
simultaneous CSPs on AAPL/MSFT/NVDA can concentrate 15% of equity in tech
without breaking any current rule. This module:

  1. Classifies each symbol → sector (yfinance for equities; static map for ETFs).
  2. Aggregates current dollar exposure per sector (positions + pending option collateral).
  3. Provides `sector_cap_ok()` as a pre-trade gate, defaulting to 25% per sector.

`Unknown` sectors don't gate (per-symbol cap still applies). yfinance failures
fall back to 'Unknown' and are cached so we don't retry every scan.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.state_db import SectorCache


log = logging.getLogger(__name__)

# Yfinance doesn't surface a sector for ETFs/index funds — use a static map
# for the well-known sector ETFs in our wheel allowlist + popular adjacencies.
# Sector names MUST match yfinance's `info.sector` terminology so a bucket
# isn't split (e.g., XLF "Financials" + JPM "Financial Services" = 2 buckets,
# both half the size, neither hits the cap).
_ETF_SECTOR_MAP: dict[str, str] = {
    "SPY": "Diversified", "QQQ": "Diversified", "IWM": "Diversified",
    "DIA": "Diversified", "VTI": "Diversified", "VOO": "Diversified",
    "XLK": "Technology", "VGT": "Technology",
    "XLF": "Financial Services", "VFH": "Financial Services",
    "XLE": "Energy", "VDE": "Energy",
    "XLV": "Healthcare", "VHT": "Healthcare",
    "XLI": "Industrials", "VIS": "Industrials",
    "XLY": "Consumer Cyclical", "VCR": "Consumer Cyclical",
    "XLP": "Consumer Defensive", "VDC": "Consumer Defensive",
    "XLU": "Utilities", "VPU": "Utilities",
    "XLB": "Basic Materials", "VAW": "Basic Materials",
    "XLRE": "Real Estate", "VNQ": "Real Estate",
    "XLC": "Communication Services", "VOX": "Communication Services",
}

# Cache TTL — sectors don't churn day-to-day. 14d strikes a balance: catches
# a corporate restructure within two weeks, doesn't hammer yfinance daily.
_CACHE_TTL = dt.timedelta(days=14)
_UNKNOWN_RETRY_TTL = dt.timedelta(hours=24)  # but DO retry 'Unknown' next day


@dataclass(frozen=True)
class SectorExposure:
    """Snapshot of current sector exposure as a fraction of equity (0.0–1.0)."""
    by_sector: dict[str, float]


class SectorClassifier:
    """Symbol → sector with local SQLite cache + in-memory memoization."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._memo: dict[str, str] = {}

    def get(self, symbol: str) -> str:
        return classify_symbol(symbol, self.engine, _memo=self._memo)


def classify_symbol(
    symbol: str, engine: Engine, *, _memo: dict[str, str] | None = None,
) -> str:
    """Return the sector for `symbol`. Cached for 14 days; ETFs use static
    map; yfinance failures cache as 'Unknown' for 24h to avoid retry loops."""
    sym = symbol.upper()
    if _memo is not None and sym in _memo:
        return _memo[sym]

    if sym in _ETF_SECTOR_MAP:
        out = _ETF_SECTOR_MAP[sym]
        if _memo is not None:
            _memo[sym] = out
        return out

    # Crypto pairs (BTCUSD, ETHUSD, ...) — yfinance doesn't have them under
    # this naming. Classify as 'Crypto' and skip the network call.
    if sym.endswith("USD") and len(sym) <= 8:
        if _memo is not None:
            _memo[sym] = "Crypto"
        return "Crypto"

    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        cached = s.query(SectorCache).filter_by(symbol=sym).one_or_none()
        if cached is not None:
            cached_at = cached.cached_at
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=dt.timezone.utc)
            ttl = _UNKNOWN_RETRY_TTL if cached.sector == "Unknown" else _CACHE_TTL
            if now - cached_at < ttl:
                if _memo is not None:
                    _memo[sym] = cached.sector
                return cached.sector

    sector = "Unknown"
    industry = ""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        info = getattr(t, "info", {}) or {}
        sector = info.get("sector") or "Unknown"
        industry = info.get("industry") or ""
    except Exception as e:
        log.info("sector classify yfinance failed for %s: %s", sym, e)

    with Session(engine) as s:
        existing = s.query(SectorCache).filter_by(symbol=sym).one_or_none()
        if existing is not None:
            existing.sector = sector
            existing.industry = industry
            existing.cached_at = now
        else:
            s.add(SectorCache(symbol=sym, sector=sector, industry=industry,
                              cached_at=now))
        s.commit()
    if _memo is not None:
        _memo[sym] = sector
    return sector


def compute_exposure(
    positions: list, *, equity: Decimal, classifier: SectorClassifier,
    option_collateral_by_symbol: dict[str, Decimal] | None = None,
) -> dict[str, float]:
    """Return {sector: fraction_of_equity}. For options we use the caller-supplied
    collateral_by_symbol (strike × 100 × |contracts|) since option market_value
    is tiny relative to the assignment risk; for everything else we use position
    market value."""
    if equity <= 0:
        return {}
    by_sector: dict[str, Decimal] = {}
    coll = option_collateral_by_symbol or {}

    for p in positions:
        ac = str(getattr(p, "asset_class", "")).lower()
        if "option" in ac:
            # Skip — option collateral is fed in via option_collateral_by_symbol
            continue
        symbol = str(getattr(p, "symbol", "")).upper()
        if not symbol:
            continue
        mv = abs(Decimal(str(getattr(p, "market_value", 0) or 0)))
        sector = classifier.get(symbol)
        by_sector[sector] = by_sector.get(sector, Decimal(0)) + mv

    for sym, dollars in coll.items():
        sector = classifier.get(sym)
        by_sector[sector] = by_sector.get(sector, Decimal(0)) + Decimal(str(dollars))

    return {k: float(v / equity) for k, v in by_sector.items()}


def sector_cap_ok(
    *, symbol: str, prospective_dollars: Decimal,
    equity: Decimal, existing_exposure: dict[str, float],
    classifier: SectorClassifier, cap_pct: float = 0.25,
) -> tuple[bool, str]:
    """Pre-trade gate. Returns (ok, reason). Unknown sectors never block.
    `existing_exposure` is the output of compute_exposure() pre-trade."""
    if equity <= 0:
        return False, "equity_zero"
    sector = classifier.get(symbol)
    if sector == "Unknown":
        return True, ""
    current = existing_exposure.get(sector, 0.0)
    addition = float(prospective_dollars / equity)
    proposed = current + addition
    if proposed > cap_pct:
        return False, (
            f"sector_cap ({sector}: {proposed*100:.0f}% > {cap_pct*100:.0f}%)"
        )
    return True, ""
