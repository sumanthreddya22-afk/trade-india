"""Phase A — regime classifier + protocols + manual override + precheck overlay."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from trading_bot.risk.manual_regime_override import applies_to, load
from trading_bot.risk.regime_classifier import (
    RegimeSignals, classify,
)
from trading_bot.risk.regime_protocols import resolve


def test_classifier_normal_when_signals_low() -> None:
    v = classify(asset_class="stocks", signals=RegimeSignals(
        vix=12.0, drawdown_pct=1.0, fear_greed=60,
    ))
    assert v.regime == "normal"


def test_classifier_caution_when_vix_in_band() -> None:
    v = classify(asset_class="stocks", signals=RegimeSignals(
        vix=20.0, drawdown_pct=2.0,
    ))
    assert v.regime == "caution"


def test_classifier_stress_when_vix_jump() -> None:
    v = classify(asset_class="stocks", signals=RegimeSignals(
        vix=28.0, drawdown_pct=9.0,
    ))
    assert v.regime == "stress"


def test_classifier_crisis_on_vix_alone() -> None:
    v = classify(asset_class="stocks", signals=RegimeSignals(vix=40.0))
    assert v.regime == "crisis"


def test_classifier_fast_trigger_forces_crisis() -> None:
    v = classify(asset_class="crypto", signals=RegimeSignals(
        vix=10.0, fast_trigger_active=True,
        fast_trigger_reason="stablecoin depeg observed",
    ))
    assert v.regime == "crisis"
    assert v.source == "fast_trigger"


def test_classifier_crypto_caution() -> None:
    v = classify(asset_class="crypto", signals=RegimeSignals(
        annualized_vol_pct=70.0,
    ))
    assert v.regime == "caution"


def test_protocols_resolve_known_strategy() -> None:
    p = resolve(strategy_id="ETF_MOMENTUM_v3", regime="caution")
    assert p.size_multiplier == 0.5
    assert p.new_entries is True

    p2 = resolve(strategy_id="ETF_MOMENTUM_v3", regime="crisis")
    assert p2.close_all is True


def test_protocols_unknown_strategy_permissive() -> None:
    p = resolve(strategy_id="UNKNOWN", regime="caution")
    assert p.size_multiplier == 1.0
    assert p.new_entries is True


def test_manual_override_load_and_applies(tmp_path: Path) -> None:
    p = tmp_path / "manual_regime_lock"
    p.write_text(json.dumps({
        "lock_version": "test", "forced_regime": "crisis",
        "asset_class_scope": ["stocks"], "reason_md": "test",
        "expiry_iso": "2099-12-31T00:00:00Z",
    }))
    ov = load(tmp_path)
    assert ov is not None
    assert ov.forced_regime == "crisis"
    assert applies_to(ov, "stocks") is True
    assert applies_to(ov, "crypto") is False


def test_manual_override_expired_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "manual_regime_lock"
    p.write_text(json.dumps({
        "lock_version": "test", "forced_regime": "crisis",
        "expiry_iso": "2000-01-01T00:00:00Z",
    }))
    assert load(tmp_path) is None


def test_manual_override_null_releases(tmp_path: Path) -> None:
    p = tmp_path / "manual_regime_lock"
    p.write_text(json.dumps({"forced_regime": None}))
    assert load(tmp_path) is None
