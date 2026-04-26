from decimal import Decimal

from trading_bot.universe import LiquidAsset


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
