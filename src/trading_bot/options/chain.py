"""Options chain — dataclass + contract pickers + liquidity gate."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trading_bot.config import WheelConfig


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
    if c.open_interest < cfg.min_open_interest:
        return False
    mid = (c.bid + c.ask) / 2.0
    if mid <= 0:
        return False
    spread = c.ask - c.bid
    if spread <= 0.10 or (spread / mid) <= 0.05:
        return True
    return False


def pick_csp_contract(
    chain: list[ChainContract], *, cfg: WheelConfig, today: dt.date,
) -> ChainContract | None:
    """Pick the put with abs(delta) closest to 0.25 inside [delta_target_low, high]
    and DTE inside [dte_min, dte_max]. Liquidity must pass. Returns None if no fit."""
    target = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
    candidates = [
        c for c in chain
        if c.kind == "P"
        and cfg.dte_min <= _dte(c, today) <= cfg.dte_max
        and cfg.delta_target_low <= abs(c.delta) <= cfg.delta_target_high
        and passes_liquidity(c, cfg)
        and c.bid >= cfg.min_premium_abs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def pick_cc_contract(
    chain: list[ChainContract], *, cost_basis: float, cfg: WheelConfig, today: dt.date,
) -> ChainContract | None:
    """Pick a call with strike >= cost_basis, abs(delta) inside band, DTE in window."""
    target = (cfg.delta_target_low + cfg.delta_target_high) / 2.0
    candidates = [
        c for c in chain
        if c.kind == "C"
        and c.strike >= cost_basis
        and cfg.dte_min <= _dte(c, today) <= cfg.dte_max
        and cfg.delta_target_low <= abs(c.delta) <= cfg.delta_target_high
        and passes_liquidity(c, cfg)
        and c.bid >= cfg.min_premium_abs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def annualized_yield(c: ChainContract, today: dt.date) -> float:
    dte = max(_dte(c, today), 1)
    collateral = c.strike * 100.0
    if collateral <= 0:
        return 0.0
    return (c.bid * 100.0 / collateral) * (365.0 / dte)
