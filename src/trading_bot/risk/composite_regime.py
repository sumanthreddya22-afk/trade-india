"""WS5f Layer 1 — composite 6-signal regime classifier.

Separate from the existing 5-regime per-asset-class classifier in
``risk.regime_classifier`` (which feeds ``policy/regime_protocols_v1.json``).
This module produces a single global ``composite_score`` in [0, 1] and
maps it to a 4-band response, per Layer 1+2 of the safety stack design.

Signals (each weighted ~equally):

| Signal                    | Source                  | Elevated trigger             |
|---------------------------|-------------------------|------------------------------|
| VIX percentile            | CBOE (free)             | >75th = 0.5, >95th = 1.0     |
| SPY realized vol (20d pct)| ledger bars             | >90th = 0.6                  |
| Cross-asset correlation   | SPY/QQQ/IWM/TLT/BTC 1d  | >0.85 avg = 0.7              |
| Market breadth (% adv)    | S&P advancers           | <30 = 0.5, <20 = 0.8         |
| Credit spread velocity    | TLT vs HYG ratio Δ      | >2σ widening = 0.6           |
| Bid-ask spread (held)     | ledger                  | >2× normal = 0.4             |

Layer 5 (health) overlay:
  1 stale  → composite ≥ 0.5
  2 stale  → composite ≥ 0.7
  4+ stale → composite ≥ 0.85 (crisis)

Layer 2 (response bands):
  Normal   <0.3       size 1.0×, new entries on
  Elevated 0.3-0.6    size 0.5×, ≤1 new/strat/day, stops -30%
  High     0.6-0.8    size 0.25×, no new entries, stops -50%
  Crisis   ≥0.8       size 0×, hard halt (no auto-flatten — that's Layer 4)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


DEFAULT_WEIGHTS: dict[str, float] = {
    "vix_percentile":          0.20,
    "spy_realized_vol":        0.15,
    "cross_asset_correlation": 0.20,
    "market_breadth":          0.15,
    "credit_spread_velocity":  0.15,
    "bid_ask_spread":          0.15,
}


def _score_vix(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    if p >= 95:
        return 1.0
    if p >= 75:
        return 0.5
    return 0.1


def _score_realized_vol(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    if p >= 90:
        return 0.6
    return 0.1


def _score_correlation(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    if c >= 0.85:
        return 0.7
    if c >= 0.7:
        return 0.4
    return 0.1


def _score_breadth(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    if p < 20:
        return 0.8
    if p < 30:
        return 0.5
    return 0.1


def _score_credit_velocity(s: Optional[float]) -> Optional[float]:
    if s is None:
        return None
    if s >= 2.0:
        return 0.6
    return 0.1


def _score_spread(m: Optional[float]) -> Optional[float]:
    if m is None:
        return None
    if m >= 2.0:
        return 0.4
    return 0.1


_SCORERS = {
    "vix_percentile": _score_vix,
    "spy_realized_vol": _score_realized_vol,
    "cross_asset_correlation": _score_correlation,
    "market_breadth": _score_breadth,
    "credit_spread_velocity": _score_credit_velocity,
    "bid_ask_spread": _score_spread,
}


BAND_NORMAL_MAX = 0.3
BAND_ELEVATED_MAX = 0.6
BAND_HIGH_MAX = 0.8


@dataclass(frozen=True)
class CompositeAssessment:
    composite_score: float
    band: str                          # normal | elevated | high | crisis
    n_signals_used: int
    n_signals_stale: int
    per_signal_scores: dict[str, Optional[float]]
    size_multiplier: float
    new_entries_allowed: bool
    stop_tightening_pct: float         # 0 | 30 | 50 | 100
    halt: bool


def _band(score: float) -> str:
    if score < BAND_NORMAL_MAX:
        return "normal"
    if score < BAND_ELEVATED_MAX:
        return "elevated"
    if score < BAND_HIGH_MAX:
        return "high"
    return "crisis"


def _band_response(band: str) -> tuple[float, bool, float, bool]:
    if band == "normal":
        return 1.0, True, 0.0, False
    if band == "elevated":
        return 0.5, True, 30.0, False
    if band == "high":
        return 0.25, False, 50.0, False
    return 0.0, False, 100.0, True


def assess(
    signals: Mapping[str, Optional[float]],
    *,
    weights: Optional[Mapping[str, float]] = None,
) -> CompositeAssessment:
    """Compute composite regime score.

    Stale signals (missing/None) contribute 0.5 each (fail-conservative).
    """
    weights = weights or DEFAULT_WEIGHTS
    per_signal: dict[str, Optional[float]] = {}
    weighted_total = 0.0
    weight_total = 0.0
    stale = 0
    for name, weight in weights.items():
        scorer = _SCORERS.get(name)
        raw = signals.get(name)
        if scorer is None or raw is None:
            per_signal[name] = None
            weighted_total += 0.5 * weight
            stale += 1
        else:
            score = scorer(raw)
            per_signal[name] = score
            weighted_total += (score if score is not None else 0.5) * weight
        weight_total += weight
    composite = weighted_total / weight_total if weight_total > 0 else 0.5

    # Layer 5 health overlay.
    if stale >= 4:
        composite = max(composite, 0.85)
    elif stale >= 2:
        composite = max(composite, 0.7)
    elif stale >= 1:
        composite = max(composite, 0.5)

    band = _band(composite)
    size_mult, new_entries, stop_tighten, halt = _band_response(band)
    return CompositeAssessment(
        composite_score=round(composite, 4),
        band=band,
        n_signals_used=len(weights) - stale,
        n_signals_stale=stale,
        per_signal_scores=per_signal,
        size_multiplier=size_mult,
        new_entries_allowed=new_entries,
        stop_tightening_pct=stop_tighten,
        halt=halt,
    )


__all__ = [
    "BAND_ELEVATED_MAX", "BAND_HIGH_MAX", "BAND_NORMAL_MAX",
    "CompositeAssessment", "DEFAULT_WEIGHTS", "assess",
]
