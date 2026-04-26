"""Universe expansion: fetch full Alpaca tradable universe, apply liquidity
screen, tag by sector, write markdown snapshot for downstream readers.

Inspired by trading-codex intelligence.collect_market_universe (which we
copied to .codex-inspiration/ for reference) but extended with ADV-based
liquidity filtering and a richer sector taxonomy.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd


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


def compute_adv(bars: pd.DataFrame) -> Decimal:
    """Average daily dollar volume across the bar window.

    Expects a DataFrame with `close` and `volume` columns. Returns Decimal.
    """
    if bars.empty:
        return Decimal("0")
    dollar_volume = bars["close"] * bars["volume"]
    return Decimal(str(float(dollar_volume.mean())))


# Sector taxonomy: tag → keyword set. Word-boundary matching avoids false positives.
SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "artificial intelligence", "machine learning"),
    "semiconductors": ("semiconductor", "chip", "silicon", "fab", "foundry"),
    "biotech": ("biotech", "biopharma", "pharma", "therapeutics", "medical"),
    "energy": ("energy", "oil", "gas", "petroleum", "exploration"),
    "uranium": ("uranium", "nuclear"),
    "metals": ("gold", "silver", "copper", "mining", "miner", "metals"),
    "crypto_equity": ("bitcoin", "blockchain", "crypto", "digital asset"),
    "consumer": ("consumer", "retail", "apparel", "restaurant"),
    "financials": ("bank", "insurance", "financial", "mortgage"),
    "real_estate": ("reit", "real estate", "property"),
    "utilities": ("utility", "utilities", "electric", "water"),
    "transport": ("airline", "shipping", "trucking", "rail"),
}


def tag_sectors(*, symbol: str, name: str) -> tuple[str, ...]:
    """Return sorted unique tags inferred from symbol+name keywords.

    Word-boundary regex prevents 'gold' from matching 'goldman' etc.
    """
    text = f"{symbol} {name}".lower()
    matched: set[str] = set()
    for tag, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, text):
                matched.add(tag)
                break
    return tuple(sorted(matched))
