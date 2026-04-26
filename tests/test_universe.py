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


def _asset(symbol, price, adv):
    return LiquidAsset(
        symbol=symbol,
        name=symbol,
        asset_class="us_equity",
        exchange="NASDAQ",
        last_price=Decimal(str(price)),
        avg_dollar_volume=Decimal(str(adv)),
        fractionable=True,
        sector_tags=(),
    )


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
