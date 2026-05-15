"""Phase A — universe_discovery tests."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable, Sequence

import pytest

from trading_bot.ingest.universe import AssetRecord, DiscoveryUnavailable
from trading_bot.research.universe_discovery import (
    compute_audit, discover, discover_sleeves,
)


def _etf(symbol: str, *, adv: float = 1e9, attrs=("ETF",)) -> AssetRecord:
    return AssetRecord(
        symbol=symbol, asset_class="us_equity", tradable=True,
        fractionable=True, avg_daily_volume_usd=adv,
        name=symbol, attributes=tuple(attrs),
    )


def _fetcher(records: Sequence[AssetRecord]):
    def _f(asset_class: str) -> Sequence[AssetRecord]:
        return [r for r in records if r.asset_class == asset_class]
    return _f


def _write_etf_policy(tmp_path: Path) -> Path:
    p = tmp_path / "etf_universe_v1.json"
    p.write_text(json.dumps({
        "asset_class": "us_equity",
        "must_have_attributes": ["ETF"],
        "exclude_attributes": ["LEVERAGED", "INVERSE"],
        "min_aum_usd": 0,
        "tradable_required": True,
        "top_n_by_adv": 3,
        "min_universe_size": 1,
    }))
    return p


def test_discover_returns_top_n_by_adv(tmp_path: Path) -> None:
    records = [
        _etf("SPY", adv=4e10),
        _etf("QQQ", adv=2e10),
        _etf("IWM", adv=1e10),
        _etf("XLF", adv=5e9),
        _etf("LEV3X", adv=3e10, attrs=("ETF", "LEVERAGED")),
    ]
    policy = _write_etf_policy(tmp_path)
    ru = discover(
        strategy_id="ETF_MOMENTUM_v3",
        policy_path=policy,
        asset_fetcher=_fetcher(records),
        decision_date=dt.date(2026, 5, 15),
        fallback_symbols=("SPY",),
    )
    assert ru.symbols == ("SPY", "QQQ", "IWM")
    assert ru.payload["n_candidates"] == 5
    assert "LEV3X" not in ru.symbols


def test_discover_uses_fallback_when_no_fetcher(tmp_path: Path) -> None:
    policy = _write_etf_policy(tmp_path)
    ru = discover(
        strategy_id="ETF_MOMENTUM_v3",
        policy_path=policy,
        asset_fetcher=None,
        fallback_symbols=("SPY", "QQQ"),
    )
    assert ru.symbols == ("SPY", "QQQ")
    assert "_fallback_reason" in ru.payload


def test_discover_raises_when_no_survivors_and_no_fallback(tmp_path: Path) -> None:
    records = [_etf("LEV3X", attrs=("ETF", "LEVERAGED"))]
    policy = _write_etf_policy(tmp_path)
    with pytest.raises(DiscoveryUnavailable):
        discover(
            strategy_id="X", policy_path=policy,
            asset_fetcher=_fetcher(records),
        )


def test_discover_sleeves(tmp_path: Path) -> None:
    p = tmp_path / "dm_v1.json"
    p.write_text(json.dumps({
        "asset_class": "us_equity",
        "sleeves": {
            "equity": {
                "must_have_attributes": ["ETF"],
                "fallback_classifier_allowlist": ["SPY", "QQQ", "VOO"],
                "top_n_by_adv": 2,
            },
            "treasury": {
                "must_have_attributes": ["ETF", "FIXED_INCOME"],
                "fallback_classifier_allowlist": ["TLT", "IEF"],
                "top_n_by_adv": 1,
            },
        }
    }))
    records = [
        _etf("SPY", adv=5e10),
        _etf("QQQ", adv=3e10),
        _etf("VOO", adv=2e10),
        _etf("TLT", adv=1e10, attrs=("ETF", "FIXED_INCOME")),
        _etf("IEF", adv=5e9, attrs=("ETF", "FIXED_INCOME")),
    ]
    out = discover_sleeves(
        strategy_id="DUAL_MOMENTUM_v3", policy_path=p,
        asset_fetcher=_fetcher(records),
        fallback_per_sleeve={"equity": ("SPY",), "treasury": ("TLT",)},
    )
    assert set(out.keys()) == {"equity", "treasury"}
    assert out["equity"].symbols == ("SPY", "QQQ")
    assert out["treasury"].symbols == ("TLT",)


def test_compute_audit_diff() -> None:
    audit = compute_audit(
        strategy_id="X",
        current_members=("SPY", "QQQ", "VTI"),
        previous_members=("SPY", "QQQ", "IWM"),
    )
    assert audit["additions"] == ["VTI"]
    assert audit["removals"] == ["IWM"]
    assert 40 < audit["turnover_pct"] < 60


def test_compute_audit_empty_baseline() -> None:
    audit = compute_audit(
        strategy_id="X", current_members=("SPY",),
    )
    assert audit["additions"] == ["SPY"]
    assert audit["removals"] == []
    assert audit["turnover_pct"] == 100.0
