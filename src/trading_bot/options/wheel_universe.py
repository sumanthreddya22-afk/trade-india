# src/trading_bot/options/wheel_universe.py
"""Dynamic wheel universe filter — runs candidates through size / liquidity /
listing-age / blocklist/allowlist filters with a 24h SQLite cache."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.config import WheelConfig
from trading_bot.intelligence_finnhub import FinnhubClient, FinnhubUnavailable
from trading_bot.state_db import WheelUniverseCache


_MIN_MARKET_CAP_MUSD = 10_000.0  # $10B in millions
_MIN_DOLLAR_VOLUME_50D = 50_000_000.0
_MIN_OPTION_VOLUME_30D = 5_000
_MIN_LISTING_YEARS = 3


@dataclass(frozen=True)
class UniverseInputs:
    candidates: list[str]
    optionable_set: set[str]
    avg_dollar_volume_50d: dict[str, float]
    avg_option_volume_30d: dict[str, float]
    finnhub: FinnhubClient
    blocklist: set[str]
    allowlist: set[str]


def _eligibility(
    sym: str, inp: UniverseInputs, today: dt.date,
) -> tuple[bool, str]:
    if sym in inp.blocklist:
        return False, "blocklist"
    if sym not in inp.optionable_set:
        return False, "not_optionable"
    # Volume filters are skipped entirely when the caller passes an empty
    # dict (deferred screener-integration: data not wired yet). When data IS
    # present for a symbol, enforce the floor.
    if inp.avg_dollar_volume_50d:
        if inp.avg_dollar_volume_50d.get(sym, 0.0) < _MIN_DOLLAR_VOLUME_50D:
            return False, "dollar_volume"
    if inp.avg_option_volume_30d:
        if inp.avg_option_volume_30d.get(sym, 0) < _MIN_OPTION_VOLUME_30D:
            return False, "option_volume"
    try:
        prof = inp.finnhub.company_profile(sym)
    except FinnhubUnavailable:
        return False, "finnhub_unavailable"
    is_etf = (prof.market_cap_musd is None and prof.exchange.upper() in {"ARCA", "BATS", "NYSE ARCA"})
    if not is_etf and (prof.market_cap_musd or 0.0) < _MIN_MARKET_CAP_MUSD:
        return False, "market_cap"
    if prof.ipo_date is not None:
        years = (today - prof.ipo_date).days / 365.25
        if years < _MIN_LISTING_YEARS:
            return False, "listing_age"
    return True, ""


def filter_universe(
    inp: UniverseInputs, *, cfg: WheelConfig, engine: Engine, today: dt.date,
    use_cache: bool = True,
) -> set[str]:
    eligible: set[str] = set(inp.allowlist)  # forced inclusion
    cache_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cfg.universe_cache_hours)
    cached: dict[str, tuple[bool, dt.datetime]] = {}
    if use_cache:
        with Session(engine) as s:
            for row in s.query(WheelUniverseCache).all():
                ts = row.cached_at
                if ts is not None and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                cached[row.symbol] = (bool(row.eligible), ts)

    fresh_rows: list[tuple[str, bool, str]] = []
    for sym in inp.candidates:
        if sym in inp.allowlist:
            continue  # already added
        c = cached.get(sym)
        if c is not None and c[1] >= cache_cutoff:
            if c[0]:
                eligible.add(sym)
            continue
        ok, reason = _eligibility(sym, inp, today)
        if ok:
            eligible.add(sym)
        fresh_rows.append((sym, ok, reason))

    if fresh_rows:
        now = dt.datetime.now(dt.timezone.utc)
        with Session(engine) as s:
            symbols = [r[0] for r in fresh_rows]
            s.execute(delete(WheelUniverseCache)
                      .where(WheelUniverseCache.symbol.in_(symbols)))
            for sym, ok, reason in fresh_rows:
                s.add(WheelUniverseCache(symbol=sym, eligible=ok, reason=reason, cached_at=now))
            s.commit()
    return eligible
