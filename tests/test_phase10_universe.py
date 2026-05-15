"""Universe discovery rules — pure-function tests, no network."""
from __future__ import annotations

import datetime as dt
from typing import Sequence

import pytest

from trading_bot.ingest.universe import (
    AssetRecord,
    Composite,
    DiscoveryUnavailable,
    TopByVolume,
    resolve_universe,
)


def _spy_tlt_qqq_btc_eth() -> list[AssetRecord]:
    """A miniature asset list — the production daemon will inject the
    real Alpaca listing here. Volumes ordered so SPY > QQQ > TLT and
    BTC > ETH; that reflects today's actual liquidity rank."""
    return [
        AssetRecord(symbol="SPY", asset_class="us_equity", tradable=True,
                    fractionable=True, avg_daily_volume_usd=70e9,
                    name="SPDR S&P 500 ETF Trust",
                    attributes=("ETF", "EQUITY_BROAD_MARKET")),
        AssetRecord(symbol="QQQ", asset_class="us_equity", tradable=True,
                    fractionable=True, avg_daily_volume_usd=25e9,
                    attributes=("ETF", "EQUITY_TECH")),
        AssetRecord(symbol="TLT", asset_class="us_equity", tradable=True,
                    fractionable=True, avg_daily_volume_usd=2.5e9,
                    attributes=("ETF", "TREASURY_LONG")),
        AssetRecord(symbol="IEF", asset_class="us_equity", tradable=True,
                    fractionable=True, avg_daily_volume_usd=0.4e9,
                    attributes=("ETF", "TREASURY_INTERMEDIATE")),
        # An untradable record that must be ignored.
        AssetRecord(symbol="OLD", asset_class="us_equity", tradable=False,
                    fractionable=False, avg_daily_volume_usd=1e6,
                    attributes=("ETF",)),
        AssetRecord(symbol="BTC/USD", asset_class="crypto", tradable=True,
                    fractionable=True, avg_daily_volume_usd=30e9,
                    attributes=("CRYPTO",)),
        AssetRecord(symbol="ETH/USD", asset_class="crypto", tradable=True,
                    fractionable=True, avg_daily_volume_usd=10e9,
                    attributes=("CRYPTO",)),
    ]


def _fetcher(records: Sequence[AssetRecord]):
    def _f(asset_class: str) -> list[AssetRecord]:
        return [a for a in records if a.asset_class == asset_class]
    return _f


def test_top_by_volume_picks_most_liquid() -> None:
    rule = TopByVolume(asset_class="us_equity", top_n=1,
                       required_attributes=("ETF", "EQUITY_BROAD_MARKET"))
    res = resolve_universe(
        rule, asset_fetcher=_fetcher(_spy_tlt_qqq_btc_eth()),
        decision_date=dt.date(2026, 5, 15),
    )
    assert res.symbols == ("SPY",)
    assert res.rule_hash  # deterministic id


def test_top_by_volume_respects_allowlist() -> None:
    """Allowlist + liquidity rank lets the seed thesis pin the v1 ETF
    set while still being data-driven about ranking."""
    rule = TopByVolume(
        asset_class="us_equity", top_n=2,
        required_attributes=("ETF",),
        symbol_allowlist=("SPY", "TLT", "IEF"),
    )
    res = resolve_universe(
        rule, asset_fetcher=_fetcher(_spy_tlt_qqq_btc_eth()),
        decision_date=dt.date(2026, 5, 15),
    )
    assert res.symbols == ("SPY", "TLT")


def test_composite_concatenates_dedupes() -> None:
    equity_rule = TopByVolume(
        asset_class="us_equity", top_n=1,
        required_attributes=("ETF", "EQUITY_BROAD_MARKET"),
    )
    bond_rule = TopByVolume(
        asset_class="us_equity", top_n=1,
        required_attributes=("ETF", "TREASURY_LONG"),
    )
    composite = Composite(sub_rules=(equity_rule, bond_rule))
    res = resolve_universe(
        composite, asset_fetcher=_fetcher(_spy_tlt_qqq_btc_eth()),
        decision_date=dt.date(2026, 5, 15),
    )
    assert res.symbols == ("SPY", "TLT")


def test_unavailable_when_no_match() -> None:
    rule = TopByVolume(asset_class="us_equity", top_n=1,
                       required_attributes=("DOES_NOT_EXIST",))
    with pytest.raises(DiscoveryUnavailable):
        resolve_universe(
            rule, asset_fetcher=_fetcher(_spy_tlt_qqq_btc_eth()),
            decision_date=dt.date(2026, 5, 15),
        )


def test_unavailable_when_fetcher_returns_empty() -> None:
    rule = TopByVolume(asset_class="us_equity", top_n=1)
    with pytest.raises(DiscoveryUnavailable):
        resolve_universe(
            rule, asset_fetcher=lambda _cls: [],
            decision_date=dt.date(2026, 5, 15),
        )


def test_untradable_assets_are_excluded() -> None:
    rule = TopByVolume(
        asset_class="us_equity", top_n=10,
        required_attributes=("ETF",),
    )
    res = resolve_universe(
        rule, asset_fetcher=_fetcher(_spy_tlt_qqq_btc_eth()),
        decision_date=dt.date(2026, 5, 15),
    )
    assert "OLD" not in res.symbols


def test_snapshot_id_changes_when_symbols_change() -> None:
    rule = TopByVolume(asset_class="us_equity", top_n=1,
                       required_attributes=("ETF",))
    records = _spy_tlt_qqq_btc_eth()
    res_a = resolve_universe(
        rule, asset_fetcher=_fetcher(records),
        decision_date=dt.date(2026, 5, 15),
    )
    # Flip SPY → QQQ as most-liquid; snapshot id must differ.
    flipped = list(records)
    flipped[0] = AssetRecord(
        symbol="SPY", asset_class="us_equity", tradable=True,
        fractionable=True, avg_daily_volume_usd=1.0,
        attributes=("ETF", "EQUITY_BROAD_MARKET"),
    )
    res_b = resolve_universe(
        rule, asset_fetcher=_fetcher(flipped),
        decision_date=dt.date(2026, 5, 15),
    )
    assert res_a.symbols == ("SPY",)
    assert res_b.symbols == ("QQQ",)
    assert res_a.snapshot_id != res_b.snapshot_id
