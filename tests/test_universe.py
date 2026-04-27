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


# test_build_universe_filters_and_tags was removed in the Plan-6 rate-limit
# hardening: _legacy_build_universe (the per-ticker fanout) was deleted
# because it was the source of 20+ min bot rank stalls. The replacement
# (build_universe_from_seed_list) is covered by tests below.


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


# --- Plan-6 follow-up: seed-list fallback ---


from dataclasses import dataclass


@dataclass
class _FakeAlpacaAsset:
    symbol: str
    name: str
    asset_class: str
    exchange: str
    fractionable: bool


class _FakeAlpacaClient:
    def __init__(self, equities, crypto):
        self._eq = equities
        self._cr = crypto

    def get_active_assets(self, kind):
        if kind == "us_equity":
            return self._eq
        if kind == "crypto":
            return self._cr
        return []


def test_build_universe_from_seed_list_intersects_with_alpaca_tradable():
    from trading_bot.universe import build_universe_from_seed_list

    alpaca_eq = [
        _FakeAlpacaAsset("AAPL", "Apple", "us_equity", "NASDAQ", True),
        _FakeAlpacaAsset("MSFT", "Microsoft", "us_equity", "NASDAQ", True),
        _FakeAlpacaAsset("OBSCURE_PENNY", "Random Penny", "us_equity", "NYSE", True),
    ]
    alpaca_cr = [
        _FakeAlpacaAsset("BTC/USD", "Bitcoin", "crypto", "FTX", True),
    ]
    client = _FakeAlpacaClient(alpaca_eq, alpaca_cr)

    universe = build_universe_from_seed_list(client)

    symbols = {a.symbol for a in universe}
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "OBSCURE_PENNY" not in symbols
    assert "BTC/USD" in symbols


def test_build_universe_from_seed_list_returns_liquid_assets_with_zero_adv():
    from trading_bot.universe import (
        CORE_LIQUID_TICKERS,
        build_universe_from_seed_list,
    )

    sample = list(CORE_LIQUID_TICKERS)[:1]
    alpaca_eq = [_FakeAlpacaAsset(sample[0], "X", "us_equity", "NASDAQ", True)]
    client = _FakeAlpacaClient(alpaca_eq, [])
    universe = build_universe_from_seed_list(client)
    assert len(universe) == 1
    assert universe[0].avg_dollar_volume == Decimal("0")
    assert universe[0].last_price == Decimal("0")


def test_core_liquid_tickers_is_substantial_and_unique():
    from trading_bot.universe import CORE_LIQUID_TICKERS

    assert len(CORE_LIQUID_TICKERS) >= 150
    assert len(set(CORE_LIQUID_TICKERS)) == len(CORE_LIQUID_TICKERS)
    for t in CORE_LIQUID_TICKERS:
        assert t.isupper()
        assert "/" not in t
