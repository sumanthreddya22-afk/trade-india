"""Options chain — dataclass + contract pickers + liquidity gate."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trading_bot.shared.config import WheelConfig


@dataclass(frozen=True)
class ChainContract:
    contract_symbol: str
    underlying: str
    expiration: dt.date
    kind: str  # "C" | "P"
    strike: float
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_volatility: float
    delta: float  # signed: puts negative, calls positive


def _dte(c: ChainContract, today: dt.date) -> int:
    return (c.expiration - today).days


def passes_liquidity(c: ChainContract, cfg: WheelConfig) -> bool:
    """Liquidity gate: open interest floor + spread guards.

    Spread requires BOTH absolute (default <= $0.10) AND relative (default
    <= 5%). Both thresholds are config-driven (cfg.liquidity_max_spread_abs
    / cfg.liquidity_max_spread_rel) so weekend / after-hours / cold-start
    runs (where Alpaca's indicative feed returns 0 OI and snapshot spreads
    are wide) can use looser values without code changes.
    """
    if c.open_interest < cfg.min_open_interest:
        return False
    mid = (c.bid + c.ask) / 2.0
    if mid <= 0:
        return False
    spread = c.ask - c.bid
    return (
        spread <= cfg.liquidity_max_spread_abs
        and (spread / mid) <= cfg.liquidity_max_spread_rel
    )


def _resolved_premium_floor(cfg: WheelConfig, engine, regime: str | None) -> float:
    """min_premium_abs with override consultation. Falls back to static cfg
    when no engine or no fresh override exists."""
    if engine is not None:
        try:
            from trading_bot.threshold_overrides import lookup
            v = lookup(engine, knob="min_premium_abs", regime=regime)
            if v is not None:
                return float(v)
        except Exception:
            pass
    return float(cfg.min_premium_abs)


def _resolved_yield_floor(cfg: WheelConfig, engine, regime: str | None) -> float:
    """min_annualized_yield with override consultation."""
    if engine is not None:
        try:
            from trading_bot.threshold_overrides import lookup
            v = lookup(engine, knob="min_annualized_yield", regime=regime)
            if v is not None:
                return float(v)
        except Exception:
            pass
    return float(cfg.min_annualized_yield)


def pick_csp_contract(
    chain: list[ChainContract], *, cfg: WheelConfig, today: dt.date,
    engine=None, regime: str | None = None,
) -> ChainContract | None:
    """Pick the put with abs(delta) closest to 0.25 inside [delta_target_low, high]
    and DTE inside [dte_min, dte_max]. Liquidity, min_premium_abs, and
    min_annualized_yield must all pass. Returns None if no fit.

    Adaptive thresholds: when ``engine`` is supplied, ``min_premium_abs``
    and ``min_annualized_yield`` are read from the ``threshold_overrides``
    table first (with safety bounds clamping), falling back to the static
    cfg values when no fresh override exists.
    """
    target = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
    premium_floor = _resolved_premium_floor(cfg, engine, regime)
    yield_floor = _resolved_yield_floor(cfg, engine, regime)
    candidates = [
        c for c in chain
        if c.kind == "P"
        and cfg.dte_min <= _dte(c, today) <= cfg.dte_max
        and cfg.delta_target_low <= abs(c.delta) <= cfg.delta_target_high
        and passes_liquidity(c, cfg)
        and c.bid >= premium_floor
        and annualized_yield(c, today) >= yield_floor
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def pick_cc_contract(
    chain: list[ChainContract], *, cost_basis: float, cfg: WheelConfig, today: dt.date,
    engine=None, regime: str | None = None,
) -> ChainContract | None:
    """Pick a call with strike >= cost_basis, abs(delta) inside band, DTE in window.

    Same override-consultation pattern as ``pick_csp_contract``.
    """
    target = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
    premium_floor = _resolved_premium_floor(cfg, engine, regime)
    yield_floor = _resolved_yield_floor(cfg, engine, regime)
    candidates = [
        c for c in chain
        if c.kind == "C"
        and c.strike >= cost_basis
        and cfg.dte_min <= _dte(c, today) <= cfg.dte_max
        and cfg.delta_target_low <= abs(c.delta) <= cfg.delta_target_high
        and passes_liquidity(c, cfg)
        and c.bid >= premium_floor
        and annualized_yield(c, today) >= yield_floor
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def annualized_yield(c: ChainContract, today: dt.date) -> float:
    """Annualized yield = (bid * 100 / collateral) * (365 / DTE).
    Used by wheel pickers to enforce ``min_annualized_yield`` (Bucket C)."""
    dte = max(_dte(c, today), 1)
    collateral = c.strike * 100.0
    if collateral <= 0:
        return 0.0
    return (c.bid * 100.0 / collateral) * (365.0 / dte)
