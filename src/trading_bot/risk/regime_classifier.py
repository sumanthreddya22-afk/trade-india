"""Five-regime classifier (v4 Phase A — autonomy expansion).

Deterministic, threshold-based, asset-class-tuned. Reads
``policy/regime_protocols_v1.json`` for thresholds. The classifier
**proposes** regime transitions; the protocol resolver translates the
regime into per-strategy actions; the risk precheck enforces them.

LLM (regime_analyst persona) sits between classifier and recovery
transition as advisory + tiebreaker — it does NOT decide alone.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.risk import DEFAULT_POLICY_DIR


REGIMES = ("normal", "caution", "stress", "crisis", "recovery")
ASSET_CLASSES = ("stocks", "crypto", "options")


@dataclass(frozen=True)
class RegimeSignals:
    """Quantitative inputs the classifier consults.

    Fields use ``None`` for "unknown / not currently observable" — the
    classifier falls through to a less-bearish regime when a key signal
    is missing rather than assuming the worst (the operator would rather
    see ``regime=normal+signal_missing`` than a phantom crisis).
    """

    vix: Optional[float] = None
    drawdown_pct: Optional[float] = None
    fear_greed: Optional[float] = None
    annualized_vol_pct: Optional[float] = None
    put_call_ratio: Optional[float] = None
    # Fast-trigger latches; if set, force crisis regardless of slow signals.
    fast_trigger_active: bool = False
    fast_trigger_reason: str = ""


@dataclass(frozen=True)
class ClassifierVerdict:
    regime: str
    triggering_signals: dict[str, Any]
    source: str  # "classifier" | "fast_trigger"
    reason: str


def _load_policy(policy_dir: Path = DEFAULT_POLICY_DIR) -> Mapping[str, Any]:
    p = policy_dir / "regime_protocols_v1.json"
    return json.loads(p.read_text())


def classify(
    *,
    asset_class: str,
    signals: RegimeSignals,
    policy_dir: Path = DEFAULT_POLICY_DIR,
) -> ClassifierVerdict:
    """Return the regime verdict for an asset class given the signals."""
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"unknown asset_class {asset_class!r}")
    policy = _load_policy(policy_dir)
    cfg = (policy.get("asset_classes") or {}).get(asset_class, {})
    th: Mapping[str, Any] = cfg.get("thresholds") or {}
    triggering: dict[str, Any] = {}

    if signals.fast_trigger_active:
        return ClassifierVerdict(
            regime="crisis",
            triggering_signals={
                "fast_trigger_active": True,
                "fast_trigger_reason": signals.fast_trigger_reason,
            },
            source="fast_trigger",
            reason=signals.fast_trigger_reason or "fast-trigger latched",
        )

    # Crisis tier — any one of these is enough.
    in_crisis = False
    if asset_class == "stocks":
        if signals.vix is not None and signals.vix >= th.get("vix_crisis", 35.0):
            in_crisis = True; triggering["vix"] = signals.vix
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_crisis_pct", 15.0):
            in_crisis = True; triggering["drawdown_pct"] = signals.drawdown_pct
        if signals.fear_greed is not None and signals.fear_greed <= th.get("fear_greed_crisis_max", 10):
            in_crisis = True; triggering["fear_greed"] = signals.fear_greed
    elif asset_class == "crypto":
        if signals.annualized_vol_pct is not None and signals.annualized_vol_pct >= th.get("annualized_vol_crisis_pct", 130.0):
            in_crisis = True; triggering["annualized_vol_pct"] = signals.annualized_vol_pct
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_crisis_pct", 30.0):
            in_crisis = True; triggering["drawdown_pct"] = signals.drawdown_pct
        if signals.fear_greed is not None and signals.fear_greed <= th.get("fear_greed_crisis_max", 10):
            in_crisis = True; triggering["fear_greed"] = signals.fear_greed
    elif asset_class == "options":
        if signals.vix is not None and signals.vix >= th.get("vix_crisis", 30.0):
            in_crisis = True; triggering["vix"] = signals.vix
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_crisis_pct", 12.0):
            in_crisis = True; triggering["drawdown_pct"] = signals.drawdown_pct

    if in_crisis:
        return ClassifierVerdict(
            regime="crisis", triggering_signals=triggering,
            source="classifier",
            reason=f"{asset_class} crisis thresholds breached",
        )

    # Stress tier
    in_stress = False
    if asset_class == "stocks":
        if signals.vix is not None and signals.vix >= th.get("vix_stress", 25.0):
            in_stress = True; triggering["vix"] = signals.vix
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_stress_pct", 8.0):
            in_stress = True; triggering["drawdown_pct"] = signals.drawdown_pct
    elif asset_class == "crypto":
        if signals.annualized_vol_pct is not None and signals.annualized_vol_pct >= th.get("annualized_vol_stress_pct", 90.0):
            in_stress = True; triggering["annualized_vol_pct"] = signals.annualized_vol_pct
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_stress_pct", 16.0):
            in_stress = True; triggering["drawdown_pct"] = signals.drawdown_pct
    elif asset_class == "options":
        if signals.vix is not None and signals.vix >= th.get("vix_stress", 22.0):
            in_stress = True; triggering["vix"] = signals.vix

    if in_stress:
        return ClassifierVerdict(
            regime="stress", triggering_signals=triggering,
            source="classifier", reason=f"{asset_class} stress thresholds breached",
        )

    # Caution tier
    in_caution = False
    if asset_class == "stocks":
        if signals.vix is not None and signals.vix >= th.get("vix_caution", 18.0):
            in_caution = True; triggering["vix"] = signals.vix
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_caution_pct", 5.0):
            in_caution = True; triggering["drawdown_pct"] = signals.drawdown_pct
        if signals.fear_greed is not None and signals.fear_greed <= th.get("fear_greed_caution_max", 30):
            in_caution = True; triggering["fear_greed"] = signals.fear_greed
    elif asset_class == "crypto":
        if signals.annualized_vol_pct is not None and signals.annualized_vol_pct >= th.get("annualized_vol_caution_pct", 60.0):
            in_caution = True; triggering["annualized_vol_pct"] = signals.annualized_vol_pct
        if signals.drawdown_pct is not None and signals.drawdown_pct >= th.get("drawdown_caution_pct", 10.0):
            in_caution = True; triggering["drawdown_pct"] = signals.drawdown_pct
    elif asset_class == "options":
        if signals.vix is not None and signals.vix >= th.get("vix_caution", 16.0):
            in_caution = True; triggering["vix"] = signals.vix

    if in_caution:
        return ClassifierVerdict(
            regime="caution", triggering_signals=triggering,
            source="classifier", reason=f"{asset_class} caution thresholds breached",
        )

    return ClassifierVerdict(
        regime="normal", triggering_signals={},
        source="classifier", reason=f"{asset_class} signals within normal bounds",
    )


__all__ = [
    "ASSET_CLASSES",
    "ClassifierVerdict",
    "REGIMES",
    "RegimeSignals",
    "classify",
]
