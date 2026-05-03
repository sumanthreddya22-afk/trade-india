"""Crypto regime detector (Phase 1E.5).

Crypto markets do not follow the SPY-based regime the stocks pipeline
uses (``trading_bot.regime``) — equity regime is irrelevant on a Sunday
crypto crash. This module derives a crypto-native regime from four
independent inputs:

  - BTC dominance (BTC market cap / total market cap)
  - Total crypto market cap moving average (50-day)
  - Crypto Fear & Greed index (Alternative.me)
  - BTC 30-day realized volatility

Output regime:
  ``crypto_trending_up``    — capital flowing in, BTC dominance falling
                              (alt season tilt), low/normal vol
  ``crypto_range``          — neutral; cap-MA flat, dominance flat,
                              F&G in 40-60 band
  ``crypto_trending_down``  — cap-MA declining, dominance rising
                              (flight to BTC), elevated vol
  ``crypto_risk_off``       — F&G < 25 OR realized vol > 2× normal —
                              hard wall: no new crypto entries

The regime feeds Phase 1E adaptive thresholds (different intel-override
threshold per regime, regime-aware TP ratios). Risk_off is the only
regime that's NOT overrideable by intel — it acts as a hard halt.

All inputs are passed in (no live HTTP). Caller is responsible for
fetching from CoinGecko / Alternative.me / market_data layer. This
keeps the regime function pure + testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regime enum + thresholds
# ---------------------------------------------------------------------------


class CryptoRegime(str, Enum):
    TRENDING_UP = "crypto_trending_up"
    RANGE = "crypto_range"
    TRENDING_DOWN = "crypto_trending_down"
    RISK_OFF = "crypto_risk_off"


@dataclass
class CryptoRegimeThresholds:
    fear_greed_risk_off: int = 25            # F&G below this = risk_off
    realized_vol_risk_off_multiplier: float = 2.0  # vs trailing baseline
    btc_dominance_uptrend_max: float = 50.0  # alt season when BTC.D drops below this
    btc_dominance_downtrend_min: float = 60.0  # flight-to-BTC when above
    cap_ma_trending_up_pct: float = 5.0      # total cap > MA by this much = uptrend
    cap_ma_trending_down_pct: float = -5.0   # below MA by this much = downtrend


@dataclass
class CryptoRegimeReading:
    """Carries the regime decision PLUS the inputs that drove it.

    The caller logs this so a dashboard can show "why did the bot decide
    the market was risk_off?" — Diane Pereira's audit trail principle
    applied to regime classification.
    """
    regime: CryptoRegime
    btc_dominance_pct: Optional[float]
    total_cap_pct_vs_ma_50d: Optional[float]
    fear_greed_index: Optional[int]
    btc_realized_vol_30d: Optional[float]
    btc_realized_vol_baseline: Optional[float]
    primary_driver: str = ""


# ---------------------------------------------------------------------------
# Pure detector — no HTTP, no DB
# ---------------------------------------------------------------------------


def detect_crypto_regime(
    *,
    btc_dominance_pct: Optional[float] = None,
    total_cap_pct_vs_ma_50d: Optional[float] = None,
    fear_greed_index: Optional[int] = None,
    btc_realized_vol_30d: Optional[float] = None,
    btc_realized_vol_baseline: Optional[float] = None,
    thresholds: Optional[CryptoRegimeThresholds] = None,
) -> CryptoRegimeReading:
    """Map the four inputs to a CryptoRegime decision.

    Decision rubric (priority order):
      1. RISK_OFF if F&G < 25 OR realized_vol > 2× baseline
      2. TRENDING_DOWN if cap_pct_vs_ma < -5% OR BTC.D > 60%
      3. TRENDING_UP if cap_pct_vs_ma > +5% AND BTC.D < 50%
      4. RANGE otherwise (neutral)
    """
    th = thresholds or CryptoRegimeThresholds()

    # 1. RISK_OFF (highest priority — overrides everything)
    if fear_greed_index is not None and fear_greed_index < th.fear_greed_risk_off:
        return CryptoRegimeReading(
            regime=CryptoRegime.RISK_OFF,
            btc_dominance_pct=btc_dominance_pct,
            total_cap_pct_vs_ma_50d=total_cap_pct_vs_ma_50d,
            fear_greed_index=fear_greed_index,
            btc_realized_vol_30d=btc_realized_vol_30d,
            btc_realized_vol_baseline=btc_realized_vol_baseline,
            primary_driver=f"fear_greed_index={fear_greed_index} < {th.fear_greed_risk_off}",
        )

    if (btc_realized_vol_30d is not None
        and btc_realized_vol_baseline is not None
        and btc_realized_vol_baseline > 0
        and btc_realized_vol_30d / btc_realized_vol_baseline >= th.realized_vol_risk_off_multiplier):
        return CryptoRegimeReading(
            regime=CryptoRegime.RISK_OFF,
            btc_dominance_pct=btc_dominance_pct,
            total_cap_pct_vs_ma_50d=total_cap_pct_vs_ma_50d,
            fear_greed_index=fear_greed_index,
            btc_realized_vol_30d=btc_realized_vol_30d,
            btc_realized_vol_baseline=btc_realized_vol_baseline,
            primary_driver=(
                f"realized_vol={btc_realized_vol_30d:.3f} ≥ "
                f"{th.realized_vol_risk_off_multiplier}× baseline {btc_realized_vol_baseline:.3f}"
            ),
        )

    # 2. TRENDING_DOWN
    if (total_cap_pct_vs_ma_50d is not None
        and total_cap_pct_vs_ma_50d <= th.cap_ma_trending_down_pct):
        return CryptoRegimeReading(
            regime=CryptoRegime.TRENDING_DOWN,
            btc_dominance_pct=btc_dominance_pct,
            total_cap_pct_vs_ma_50d=total_cap_pct_vs_ma_50d,
            fear_greed_index=fear_greed_index,
            btc_realized_vol_30d=btc_realized_vol_30d,
            btc_realized_vol_baseline=btc_realized_vol_baseline,
            primary_driver=(
                f"total_cap {total_cap_pct_vs_ma_50d:.1f}% vs MA50d ≤ "
                f"{th.cap_ma_trending_down_pct}%"
            ),
        )

    if btc_dominance_pct is not None and btc_dominance_pct >= th.btc_dominance_downtrend_min:
        return CryptoRegimeReading(
            regime=CryptoRegime.TRENDING_DOWN,
            btc_dominance_pct=btc_dominance_pct,
            total_cap_pct_vs_ma_50d=total_cap_pct_vs_ma_50d,
            fear_greed_index=fear_greed_index,
            btc_realized_vol_30d=btc_realized_vol_30d,
            btc_realized_vol_baseline=btc_realized_vol_baseline,
            primary_driver=f"BTC.D={btc_dominance_pct:.1f}% ≥ {th.btc_dominance_downtrend_min}% (flight to BTC)",
        )

    # 3. TRENDING_UP — both signals must align
    if (total_cap_pct_vs_ma_50d is not None
        and btc_dominance_pct is not None
        and total_cap_pct_vs_ma_50d >= th.cap_ma_trending_up_pct
        and btc_dominance_pct <= th.btc_dominance_uptrend_max):
        return CryptoRegimeReading(
            regime=CryptoRegime.TRENDING_UP,
            btc_dominance_pct=btc_dominance_pct,
            total_cap_pct_vs_ma_50d=total_cap_pct_vs_ma_50d,
            fear_greed_index=fear_greed_index,
            btc_realized_vol_30d=btc_realized_vol_30d,
            btc_realized_vol_baseline=btc_realized_vol_baseline,
            primary_driver=(
                f"total_cap {total_cap_pct_vs_ma_50d:.1f}% > MA50d AND "
                f"BTC.D={btc_dominance_pct:.1f}% < {th.btc_dominance_uptrend_max}%"
            ),
        )

    # 4. Default → RANGE
    return CryptoRegimeReading(
        regime=CryptoRegime.RANGE,
        btc_dominance_pct=btc_dominance_pct,
        total_cap_pct_vs_ma_50d=total_cap_pct_vs_ma_50d,
        fear_greed_index=fear_greed_index,
        btc_realized_vol_30d=btc_realized_vol_30d,
        btc_realized_vol_baseline=btc_realized_vol_baseline,
        primary_driver="no trend / risk-off conditions met → range",
    )


# ---------------------------------------------------------------------------
# Regime-aware TP ratio (used at order-placement time)
# ---------------------------------------------------------------------------


REGIME_TP_RATIO = {
    CryptoRegime.TRENDING_UP:   3.0,   # cycle trends extend
    CryptoRegime.RANGE:         1.5,   # mean-reverting
    CryptoRegime.TRENDING_DOWN: 1.2,   # counter-trend, in-and-out
    CryptoRegime.RISK_OFF:      1.0,   # no entries should fire here, but
                                        # if one slips through, target
                                        # becomes 1:1 (bare risk-reward)
}


def regime_aware_tp_ratio(regime: CryptoRegime) -> float:
    """Return the TP:SL ratio for a given regime."""
    return REGIME_TP_RATIO.get(regime, 1.5)
