"""Dual Momentum runner — verify the universe is data-driven, not
hardcoded, and the discovery payload is captured for the snapshot."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.ingest.universe import AssetRecord
from trading_bot.strategies.dual_momentum_v1.runner import (
    DISCOVERY_RULE,
    _resolve_universe_with_fallback,
    evaluate_strategy,
)


def _make_fetcher(records):
    def _f(asset_class):
        return [a for a in records if a.asset_class == asset_class]
    return _f


def test_discovery_rule_name_is_namespaced() -> None:
    """Rule name carries the strategy id — required for hash-lock
    governance (a rule rename = new strategy version)."""
    assert DISCOVERY_RULE.name == "dual_momentum_v1.default"


def test_resolver_returns_thesis_pair_today() -> None:
    """With SPY most-liquid equity ETF and TLT most-liquid long-treasury
    ETF (both in the thesis allowlist), the rule returns the pair."""
    records = [
        AssetRecord("SPY", "us_equity", True, True, 70e9,
                    attributes=("ETF",)),
        AssetRecord("QQQ", "us_equity", True, True, 30e9,
                    attributes=("ETF",)),
        AssetRecord("TLT", "us_equity", True, True, 2e9,
                    attributes=("ETF",)),
        AssetRecord("IEF", "us_equity", True, True, 0.5e9,
                    attributes=("ETF",)),
    ]
    universe, payload = _resolve_universe_with_fallback(
        asset_fetcher=_make_fetcher(records),
        decision_date=dt.date(2026, 5, 15),
    )
    assert universe == ("SPY", "TLT")
    assert payload["rule_name"] == "dual_momentum_v1.default"
    assert payload["rule_hash"] != "fallback:static"


def test_resolver_picks_qqq_if_qqq_outvolumes_spy() -> None:
    """Allowlist permits QQQ. If it overtakes SPY in volume the
    discovery picks it — the strategy is data-driven, not pinned."""
    records = [
        AssetRecord("SPY", "us_equity", True, True, 10e9,
                    attributes=("ETF",)),
        AssetRecord("QQQ", "us_equity", True, True, 80e9,
                    attributes=("ETF",)),
        AssetRecord("TLT", "us_equity", True, True, 2e9,
                    attributes=("ETF",)),
    ]
    universe, _ = _resolve_universe_with_fallback(
        asset_fetcher=_make_fetcher(records),
        decision_date=dt.date(2026, 5, 15),
    )
    assert universe == ("QQQ", "TLT")


def test_resolver_falls_back_when_no_fetcher() -> None:
    universe, payload = _resolve_universe_with_fallback(
        asset_fetcher=None, decision_date=dt.date(2026, 5, 15),
    )
    assert universe == ("SPY", "TLT")
    assert payload["rule_hash"] == "fallback:static"
    assert "_fallback_reason" in payload


def test_resolver_falls_back_when_discovery_unavailable() -> None:
    """When the fetcher returns nothing usable, the resolver falls
    back to the static thesis universe with an explicit breadcrumb in
    the payload so feature_snapshot shows the operator the slip."""
    universe, payload = _resolve_universe_with_fallback(
        asset_fetcher=lambda _cls: [],
        decision_date=dt.date(2026, 5, 15),
    )
    assert universe == ("SPY", "TLT")
    assert payload["rule_hash"] == "fallback:discovery_unavailable"
    assert "_fallback_reason" in payload


def test_resolver_uses_volume_provider_to_enrich() -> None:
    """Alpaca's asset list lacks ADV. The runner wires a volume
    provider (e.g. yfinance bars) so the ranking is still
    data-driven, not alphabetic."""
    records = [
        AssetRecord("SPY", "us_equity", True, True, None,
                    attributes=("ETF",)),
        AssetRecord("QQQ", "us_equity", True, True, None,
                    attributes=("ETF",)),
        AssetRecord("TLT", "us_equity", True, True, None,
                    attributes=("ETF",)),
    ]
    advs = {"SPY": 70e9, "QQQ": 30e9, "TLT": 2e9}
    universe, payload = _resolve_universe_with_fallback(
        asset_fetcher=_make_fetcher(records),
        decision_date=dt.date(2026, 5, 15),
        volume_provider=lambda s: advs.get(s),
    )
    assert universe == ("SPY", "TLT")
    assert payload["rule_hash"] not in ("fallback:static",
                                        "fallback:discovery_unavailable")


def test_evaluate_strategy_returns_empty_when_no_history(tmp_path) -> None:
    """When the historical store doesn't exist the runner shouldn't
    crash — it returns an empty decision so the dispatch loop logs a
    skip row."""
    bogus_db = tmp_path / "does_not_exist.db"
    out = evaluate_strategy(
        historical_db=bogus_db,
        decision_date=dt.date(2026, 5, 15),
    )
    assert out.intents == []
    assert out.universe == ()
