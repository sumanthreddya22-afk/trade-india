"""Universe expansion: fetch full Alpaca tradable universe, apply liquidity
screen, tag by sector, write markdown snapshot for downstream readers.

Inspired by trading-codex intelligence.collect_market_universe (which we
copied to .codex-inspiration/ for reference) but extended with ADV-based
liquidity filtering and a richer sector taxonomy.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from trading_bot.alpaca_client import AlpacaClient


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


DEFAULT_MIN_PRICE = Decimal("10")  # raised from $5 — penny-stock filter
DEFAULT_MIN_ADV = Decimal("10000000")  # $10M average daily dollar volume — raised from $5M


def apply_liquidity_filter(
    assets: Iterable[LiquidAsset],
    *,
    min_price: Decimal = DEFAULT_MIN_PRICE,
    min_adv: Decimal = DEFAULT_MIN_ADV,
) -> list[LiquidAsset]:
    """Keep only assets whose last price >= min_price and avg dollar volume >= min_adv.

    Equity rules: both price and ADV filters apply.

    Crypto rules: neither filter applies. The price floor is meaningless for
    pairs like DOGE/USD ($0.09) or PEPE/USD that are still highly liquid in
    real markets. The ADV filter is also wrong here: Alpaca paper crypto
    bars carry sandbox-level volume (BTC/USD reports ~$160k/day vs ~$30B in
    reality). So we trust Alpaca's `tradable` flag for crypto and let all
    pairs through; downstream lanes still gate on signal strength per-symbol.
    """
    out: list[LiquidAsset] = []
    for a in assets:
        is_crypto = "/" in a.symbol or "crypto" in a.asset_class.lower()
        if is_crypto:
            out.append(a)
            continue
        if a.last_price < min_price:
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


def build_universe(
    alpaca: AlpacaClient,
    *,
    bar_loader: Callable[[str], pd.DataFrame],
    min_price: Decimal = DEFAULT_MIN_PRICE,
    min_adv: Decimal = DEFAULT_MIN_ADV,
) -> list[LiquidAsset]:
    """Pull tradable universe, score liquidity, tag sectors, return LiquidAssets.

    bar_loader is injected so tests can supply canned data without hitting Alpaca.
    """
    raw_equities = alpaca.get_active_assets("us_equity")
    raw_crypto = alpaca.get_active_assets("crypto")

    candidates: list[LiquidAsset] = []
    for asset in list(raw_equities) + list(raw_crypto):
        bars = bar_loader(asset.symbol)
        if bars.empty:
            continue
        last_price = Decimal(str(float(bars["close"].iloc[-1])))
        adv = compute_adv(bars)
        candidates.append(
            LiquidAsset(
                symbol=asset.symbol,
                name=asset.name,
                asset_class=asset.asset_class,
                exchange=asset.exchange,
                last_price=last_price,
                avg_dollar_volume=adv,
                fractionable=asset.fractionable,
                sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
            )
        )
    return apply_liquidity_filter(candidates, min_price=min_price, min_adv=min_adv)


from collections import Counter
from datetime import datetime
from pathlib import Path


def render_universe_snapshot(
    assets: list[LiquidAsset],
    *,
    generated_at: datetime,
    top_n_per_sector: int = 5,
) -> str:
    """Render a markdown snapshot summarizing the universe."""
    lines = [
        "# Universe Snapshot",
        "",
        f"Generated: {generated_at.isoformat(timespec='seconds')}",
        f"Total liquid assets: {len(assets)}",
        "",
    ]

    sector_counts: Counter[str] = Counter()
    for a in assets:
        for tag in a.sector_tags:
            sector_counts[tag] += 1

    lines.extend(["## Sector Breakdown", ""])
    for sector, count in sector_counts.most_common():
        lines.append(f"- {sector}: {count}")
    if not sector_counts:
        lines.append("- (no sector tags applied)")
    lines.append("")

    lines.extend(["## Top Names by ADV (per sector)", ""])
    by_sector: dict[str, list[LiquidAsset]] = {}
    for a in assets:
        for tag in a.sector_tags or ("untagged",):
            by_sector.setdefault(tag, []).append(a)
    for sector in sorted(by_sector):
        ranked = sorted(by_sector[sector], key=lambda x: x.avg_dollar_volume, reverse=True)[:top_n_per_sector]
        lines.append(f"### {sector}")
        for a in ranked:
            lines.append(
                f"- {a.symbol} ({a.exchange}) — ${a.last_price} — ADV ${a.avg_dollar_volume:,.0f}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_universe_snapshot(assets: list[LiquidAsset], path: Path, *, generated_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_universe_snapshot(assets, generated_at=generated_at))
