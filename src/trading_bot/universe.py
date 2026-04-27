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


# --- Plan-6 follow-up: seed-list fallback for cold-start / Massive outage ---
#
# Hardcoded list of well-known liquid US-equity tickers. Used by
# build_universe_from_seed_list when the Massive grouped cache is empty
# and we still need *some* universe to rank against. Curated to cover:
#   - SPY/QQQ mega-caps, FAANG, big tech
#   - Semiconductors (AMD/NVDA/AVGO/...)
#   - Financials (JPM/BAC/WFC/...)
#   - Energy majors (XOM/CVX/COP/...)
#   - Healthcare/biotech leaders
#   - Defensives (KO/PG/JNJ/WMT/...)
#   - High-volume ETFs (SPY/QQQ/IWM/DIA/XLK/XLF/...)
# Tickers are intersected with Alpaca's current tradable list, so any
# delistings drop silently. Review quarterly.
_CORE_LIQUID_TICKERS_RAW: tuple[str, ...] = (
    # ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE",
    "XLC", "GLD", "SLV", "TLT", "HYG", "LQD", "GDX", "USO", "UNG", "ARKK",
    "SOXX", "SMH", "IBB", "XBI", "KRE", "KWEB", "FXI", "EWZ", "EWJ", "INDA",
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "NFLX", "ADBE",
    "CRM", "ORCL", "CSCO", "INTC", "AMD", "AVGO", "QCOM", "TXN", "MU", "AMAT",
    "ASML", "TSM", "LRCX", "KLAC", "MRVL", "NOW", "INTU", "PYPL", "SHOP", "SQ",
    "UBER", "ABNB", "SNOW", "PLTR", "CRWD", "ZS", "DDOG", "NET", "MDB", "TEAM",
    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "SCHW", "AXP", "USB",
    "PNC", "TFC", "COF", "BK", "STT", "V", "MA", "FIS", "FISV",
    "BX", "KKR", "APO",
    # Energy
    "XOM", "CVX", "COP", "EOG", "OXY", "PXD", "PSX", "VLO", "MPC", "SLB",
    "HAL", "BKR", "DVN", "FANG", "HES", "MRO", "APA",
    # Healthcare / biotech
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD", "BIIB",
    "REGN", "VRTX", "ISRG", "TMO", "DHR", "ABT", "MDT", "SYK", "ZTS", "CVS",
    "CI", "HUM", "ELV", "MRNA", "BNTX",
    # Industrials / defense
    "BA", "CAT", "DE", "MMM", "GE", "LMT", "RTX", "NOC", "GD", "HON",
    "UPS", "FDX", "UNP", "CSX", "NSC", "DAL", "UAL", "LUV", "AAL",
    # Consumer
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "MCD", "SBUX", "DIS", "CMCSA",
    "T", "VZ", "TMUS", "KO", "PEP", "PG", "MO", "PM", "CL",
    "KMB", "GIS", "K", "MDLZ", "HSY", "EL", "ULTA",
    # Materials
    "LIN", "APD", "SHW", "FCX", "NEM", "DOW", "DD", "NUE", "STLD", "X",
    "AA", "CLF",
    # Real estate
    "AMT", "PLD", "CCI", "EQIX", "PSA", "WELL", "O", "SPG",
    # Utilities
    "NEE", "DUK", "SO", "AEP", "EXC", "XEL", "SRE", "D", "PEG",
    # Misc large-cap / high-volume
    "WBA", "F", "GM", "RIVN", "LCID", "NIO", "BABA", "JD", "PDD",
    "ROKU", "SPOT", "PINS", "SNAP", "TWLO", "ZM", "DOCU", "OKTA", "FSLY",
    "MARA", "RIOT", "COIN", "HOOD", "SOFI", "AFRM",
)
CORE_LIQUID_TICKERS: tuple[str, ...] = tuple(sorted(set(_CORE_LIQUID_TICKERS_RAW)))


def build_universe_from_seed_list(alpaca: "AlpacaClient") -> list[LiquidAsset]:
    """Cold-start fallback when the grouped cache has no fresh data.

    Pulls Alpaca's tradable equity + crypto list, intersects equities
    with `CORE_LIQUID_TICKERS`, and returns LiquidAssets shaped for
    downstream stage-1 ranking. last_price/avg_dollar_volume are 0 —
    the screener recomputes both from per-symbol bars before ranking.

    Crypto is included in full (no seed-list filter): the set is small
    enough that liquidity filtering happens lane-side per symbol.
    """
    raw_equities = alpaca.get_active_assets("us_equity")
    raw_crypto = alpaca.get_active_assets("crypto")

    seed_set = set(CORE_LIQUID_TICKERS)
    out: list[LiquidAsset] = []

    for asset in raw_equities:
        if asset.symbol not in seed_set:
            continue
        out.append(LiquidAsset(
            symbol=asset.symbol, name=asset.name,
            asset_class=asset.asset_class, exchange=asset.exchange,
            last_price=Decimal("0"),
            avg_dollar_volume=Decimal("0"),
            fractionable=asset.fractionable,
            sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
        ))

    for asset in raw_crypto:
        out.append(LiquidAsset(
            symbol=asset.symbol, name=asset.name,
            asset_class=asset.asset_class, exchange=asset.exchange,
            last_price=Decimal("0"),
            avg_dollar_volume=Decimal("0"),
            fractionable=asset.fractionable,
            sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
        ))

    return out


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


def build_universe_from_grouped(
    alpaca: AlpacaClient,
    *,
    massive_grouped_loader: Callable[[], pd.DataFrame],
    crypto_bar_loader: Callable[[str], pd.DataFrame],
    min_price: Decimal = DEFAULT_MIN_PRICE,
    min_adv: Decimal = DEFAULT_MIN_ADV,
    adv_window_days: int = 5,
) -> list[LiquidAsset]:
    """Plan-6 universe: one Massive grouped-aggregates call covers every
    US equity in a single call. We approximate ADV from a single day
    (close × volume) which is biased — for a tighter ADV, the caller
    can pre-fetch multiple grouped days and average.

    Crypto stays on the per-ticker path (Massive supports crypto grouped
    too, but we already have the loader for it; not worth a refactor).

    Returns LiquidAssets that:
    - Are in Alpaca's tradable list (so we can place orders)
    - Pass our liquidity filter (price ≥ min_price, ADV ≥ min_adv) for
      stocks. Crypto bypasses (real-market depth isn't reflected in
      Alpaca paper volume).
    """
    grouped = massive_grouped_loader()  # DataFrame indexed by ticker
    if grouped.empty:
        # Empty grouped is only valid on holidays/weekends. Caller is
        # responsible for handling [] — we don't fall back to a 10k-symbol
        # legacy fanout (that path produced 20+ min stalls). Returning an
        # empty list signals "no usable equities from grouped".
        return []

    raw_equities = alpaca.get_active_assets("us_equity")
    raw_crypto = alpaca.get_active_assets("crypto")
    tradable_eq_by_symbol = {a.symbol: a for a in raw_equities}

    candidates: list[LiquidAsset] = []

    # --- stocks via grouped ---
    for ticker in grouped.index:
        a = tradable_eq_by_symbol.get(ticker)
        if a is None:
            continue  # not tradable on Alpaca
        row = grouped.loc[ticker]
        close = float(row["c"])
        if close <= 0:
            continue
        # ADV approximation: close × volume from this single day. The screener
        # also recomputes a more accurate ADV from per-ticker bars, so a rough
        # first-cut here is fine.
        adv = Decimal(str(close * float(row["v"])))
        candidates.append(LiquidAsset(
            symbol=a.symbol, name=a.name,
            asset_class=a.asset_class, exchange=a.exchange,
            last_price=Decimal(str(close)),
            avg_dollar_volume=adv,
            fractionable=a.fractionable,
            sector_tags=tag_sectors(symbol=a.symbol, name=a.name),
        ))

    # --- crypto via per-ticker (small set, no benefit from grouped) ---
    for asset in raw_crypto:
        bars = crypto_bar_loader(asset.symbol)
        if bars.empty:
            continue
        last_price = Decimal(str(float(bars["close"].iloc[-1])))
        adv = compute_adv(bars)
        candidates.append(LiquidAsset(
            symbol=asset.symbol, name=asset.name,
            asset_class=asset.asset_class, exchange=asset.exchange,
            last_price=last_price, avg_dollar_volume=adv,
            fractionable=asset.fractionable,
            sector_tags=tag_sectors(symbol=asset.symbol, name=asset.name),
        ))

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
