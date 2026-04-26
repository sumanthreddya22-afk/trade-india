"""Universe expansion: fetch full Alpaca tradable universe, apply liquidity
screen, tag by sector, write markdown snapshot for downstream readers.

Inspired by trading-codex intelligence.collect_market_universe (which we
copied to .codex-inspiration/ for reference) but extended with ADV-based
liquidity filtering and a richer sector taxonomy.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LiquidAsset:
    symbol: str
    name: str
    asset_class: str  # "us_equity" | "crypto"
    exchange: str
    last_price: Decimal
    avg_dollar_volume: Decimal
    fractionable: bool
    sector_tags: tuple[str, ...]


DEFAULT_MIN_PRICE = Decimal("5")
DEFAULT_MIN_ADV = Decimal("5000000")  # $5M average daily dollar volume


def apply_liquidity_filter(
    assets: Iterable[LiquidAsset],
    *,
    min_price: Decimal = DEFAULT_MIN_PRICE,
    min_adv: Decimal = DEFAULT_MIN_ADV,
) -> list[LiquidAsset]:
    """Keep only assets whose last price >= min_price and avg dollar volume >= min_adv.

    Crypto assets bypass the equity-style price floor (BTC and ETH are always
    above $5; SOL/USD etc. are also fine), but ADV still applies.
    """
    out: list[LiquidAsset] = []
    for a in assets:
        if a.asset_class == "us_equity" and a.last_price < min_price:
            continue
        if a.avg_dollar_volume < min_adv:
            continue
        out.append(a)
    return out
