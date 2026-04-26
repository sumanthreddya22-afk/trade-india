from decimal import Decimal

from trading_bot.universe import LiquidAsset, apply_liquidity_filter


def test_liquid_asset_holds_screening_fields():
    asset = LiquidAsset(
        symbol="NVDA",
        name="NVIDIA Corp",
        asset_class="us_equity",
        exchange="NASDAQ",
        last_price=Decimal("450.00"),
        avg_dollar_volume=Decimal("8500000000"),
        fractionable=True,
        sector_tags=("ai", "semiconductors"),
    )
    assert asset.symbol == "NVDA"
    assert asset.avg_dollar_volume > Decimal("5000000")
    assert "ai" in asset.sector_tags


def _asset(symbol, price, adv, asset_class="us_equity"):
    return LiquidAsset(
        symbol=symbol,
        name=symbol,
        asset_class=asset_class,
        exchange="NASDAQ" if asset_class == "us_equity" else "CRYPTO",
        last_price=Decimal(str(price)),
        avg_dollar_volume=Decimal(str(adv)),
        fractionable=True,
        sector_tags=(),
    )


def test_liquidity_filter_passes_crypto_regardless_of_adv_or_price():
    """Alpaca paper crypto reports sandbox-level volume (e.g. BTC/USD ~$160k/day)
    even though real-market liquidity is in the billions. Crypto should bypass
    both the price floor (DOGE/USD = $0.09) and the ADV floor."""
    assets = [
        _asset("BTC/USD", 68000, 161000, asset_class="crypto"),
        _asset("DOGE/USD", 0.09, 25000, asset_class="crypto"),
        _asset("PENNY", 0.50, 50_000_000),  # equity below price floor — should drop
    ]
    kept = apply_liquidity_filter(assets, min_price=Decimal("10"), min_adv=Decimal("10000000"))
    syms = {a.symbol for a in kept}
    assert "BTC/USD" in syms
    assert "DOGE/USD" in syms
    assert "PENNY" not in syms


def test_liquidity_filter_keeps_qualified_assets():
    assets = [
        _asset("NVDA", 450, 8_500_000_000),
        _asset("AMD", 100, 2_000_000_000),
    ]
    kept = apply_liquidity_filter(assets, min_price=Decimal("5"), min_adv=Decimal("5000000"))
    assert {a.symbol for a in kept} == {"NVDA", "AMD"}


def test_liquidity_filter_drops_low_price():
    assets = [_asset("PENNY", 2.50, 100_000_000)]
    kept = apply_liquidity_filter(assets, min_price=Decimal("5"), min_adv=Decimal("5000000"))
    assert kept == []


def test_liquidity_filter_drops_low_adv():
    assets = [_asset("ILLIQ", 50, 100_000)]
    kept = apply_liquidity_filter(assets, min_price=Decimal("5"), min_adv=Decimal("5000000"))
    assert kept == []


from pathlib import Path
import pandas as pd

from trading_bot.universe import compute_adv


def test_compute_adv_returns_avg_dollar_volume():
    fixture = Path(__file__).parent / "fixtures" / "bars" / "nvda_20d.csv"
    bars = pd.read_csv(fixture, parse_dates=["timestamp"])
    adv = compute_adv(bars)
    # mean(close * volume) across 20 rows
    expected = (bars["close"] * bars["volume"]).mean()
    assert abs(float(adv) - float(expected)) < 1.0


from trading_bot.universe import tag_sectors


def test_tag_sectors_finds_ai_semiconductors():
    tags = tag_sectors(symbol="NVDA", name="NVIDIA Corp - AI semiconductor leader")
    assert "ai" in tags
    assert "semiconductors" in tags


def test_tag_sectors_finds_energy():
    tags = tag_sectors(symbol="XLE", name="Energy Select Sector SPDR Fund")
    assert "energy" in tags


def test_tag_sectors_returns_empty_on_no_match():
    tags = tag_sectors(symbol="ZZZ", name="Generic Holdings Inc")
    assert tags == ()


from unittest.mock import MagicMock
from decimal import Decimal
import pandas as pd

from trading_bot.alpaca_client import TradableAsset
from trading_bot.universe import build_universe


def test_build_universe_filters_and_tags():
    alpaca = MagicMock()
    alpaca.get_active_assets.side_effect = [
        [
            TradableAsset(symbol="NVDA", name="NVIDIA semiconductor",
                          exchange="NASDAQ", asset_class="us_equity",
                          tradable=True, fractionable=True),
            TradableAsset(symbol="ILLIQ", name="Illiquid Inc",
                          exchange="NYSE", asset_class="us_equity",
                          tradable=True, fractionable=False),
        ],
        [
            TradableAsset(symbol="BTC/USD", name="Bitcoin USD",
                          exchange="CRYPTO", asset_class="crypto",
                          tradable=True, fractionable=True),
        ],
    ]

    def fake_bar_loader(symbol: str) -> pd.DataFrame:
        if symbol == "NVDA":
            return pd.DataFrame({"close": [450]*20, "volume": [30_000_000]*20})
        if symbol == "ILLIQ":
            return pd.DataFrame({"close": [10]*20, "volume": [10_000]*20})
        if symbol == "BTC/USD":
            return pd.DataFrame({"close": [70000]*20, "volume": [1_000]*20})
        return pd.DataFrame()

    universe = build_universe(alpaca, bar_loader=fake_bar_loader)
    symbols = {a.symbol for a in universe}
    assert "NVDA" in symbols
    assert "BTC/USD" in symbols
    assert "ILLIQ" not in symbols  # filtered out by ADV
    nvda = next(a for a in universe if a.symbol == "NVDA")
    assert "semiconductors" in nvda.sector_tags


from datetime import datetime, timezone

from trading_bot.universe import render_universe_snapshot


def test_render_universe_snapshot_includes_counts_and_top_sectors():
    assets = [
        LiquidAsset(symbol="NVDA", name="NVIDIA",
                    asset_class="us_equity", exchange="NASDAQ",
                    last_price=Decimal("450"), avg_dollar_volume=Decimal("8e9"),
                    fractionable=True, sector_tags=("ai", "semiconductors")),
        LiquidAsset(symbol="GLD", name="Gold ETF",
                    asset_class="us_equity", exchange="NYSE",
                    last_price=Decimal("180"), avg_dollar_volume=Decimal("3e8"),
                    fractionable=True, sector_tags=("metals",)),
    ]
    md = render_universe_snapshot(assets, generated_at=datetime(2026, 4, 25, tzinfo=timezone.utc))
    assert "# Universe Snapshot" in md
    assert "Total liquid assets: 2" in md
    assert "NVDA" in md
    assert "semiconductors" in md
