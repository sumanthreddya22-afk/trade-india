"""Live market regime detection.

Inputs: SPY bars (trend + realized vol) and optionally VIX (FRED VIXCLS).

Returns one of: trending_up | trending_down | sideways | risk_off.

VIX rules (when supplied):
    VIX > 28          → force risk_off  (high implied vol = professionals hedging)
    VIX in (22, 28]   → force at least sideways (no trending_up classification)
    VIX missing/stale → fall back to bars-only logic
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from trading_bot.market_data import MarketDataClient

DEFAULT_VOL_THRESHOLD_PCT = 22.0  # was 30 — see Phase 0b
VIX_RISK_OFF = 28.0
VIX_SIDEWAYS = 22.0


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    SIDEWAYS = "sideways"
    RISK_OFF = "risk_off"


@dataclass(frozen=True)
class RegimeReading:
    regime: Regime
    spy_close: float
    ema_50: float
    ema_200: float
    vol_annualized_pct: float
    confidence: str  # "high" | "medium" | "low"
    notes: str
    vix: float | None = None


def detect_regime_from_bars(
    spy_bars: pd.DataFrame,
    *,
    vix: float | None = None,
    vol_threshold_pct: float = DEFAULT_VOL_THRESHOLD_PCT,
) -> RegimeReading:
    """Pure function: compute regime from SPY bars + optional VIX."""
    if len(spy_bars) < 60:
        return RegimeReading(
            regime=Regime.SIDEWAYS,
            spy_close=float(spy_bars["close"].iloc[-1]) if len(spy_bars) else 0.0,
            ema_50=0.0,
            ema_200=0.0,
            vol_annualized_pct=0.0,
            confidence="low",
            notes=f"only {len(spy_bars)} bars — insufficient for confident regime",
            vix=vix,
        )

    close = spy_bars["close"]
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    ema200 = float(close.ewm(span=min(200, len(close)), adjust=False).mean().iloc[-1])
    last = float(close.iloc[-1])

    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) >= 20:
        vol_20d = float(log_ret.tail(20).std(ddof=1) * np.sqrt(252) * 100)
    else:
        vol_20d = float(log_ret.std(ddof=1) * np.sqrt(252) * 100) if len(log_ret) else 0.0

    recent_drawdown = float((close.iloc[-1] - close.tail(20).max()) / close.tail(20).max() * 100)

    # VIX hard override → risk_off (highest priority signal when present)
    if vix is not None and vix > VIX_RISK_OFF:
        return RegimeReading(
            regime=Regime.RISK_OFF,
            spy_close=last, ema_50=ema50, ema_200=ema200,
            vol_annualized_pct=vol_20d, confidence="high",
            notes=f"VIX {vix:.1f} > {VIX_RISK_OFF} (override)",
            vix=vix,
        )

    # Realized-vol risk_off (now configurable; default 22% per Phase 0b)
    if vol_20d > vol_threshold_pct or recent_drawdown < -10:
        return RegimeReading(
            regime=Regime.RISK_OFF,
            spy_close=last, ema_50=ema50, ema_200=ema200,
            vol_annualized_pct=vol_20d, confidence="high",
            notes=(f"vol {vol_20d:.1f}% > {vol_threshold_pct}% "
                   f"or 20d drawdown {recent_drawdown:.1f}% < -10%"),
            vix=vix,
        )

    # VIX soft override → no trending_up; minimum sideways
    vix_caps_to_sideways = vix is not None and vix > VIX_SIDEWAYS

    # Trend up: above both EMAs and EMA50 > EMA200, calm vol, VIX not elevated
    if last > ema50 and ema50 > ema200 and vol_20d < 25 and not vix_caps_to_sideways:
        return RegimeReading(
            regime=Regime.TRENDING_UP,
            spy_close=last, ema_50=ema50, ema_200=ema200,
            vol_annualized_pct=vol_20d, confidence="high",
            notes="close > EMA50 > EMA200, vol calm" + (f", VIX {vix:.1f}" if vix else ""),
            vix=vix,
        )

    # Trend down
    if last < ema50 and ema50 < ema200:
        return RegimeReading(
            regime=Regime.TRENDING_DOWN,
            spy_close=last, ema_50=ema50, ema_200=ema200,
            vol_annualized_pct=vol_20d, confidence="high",
            notes="close < EMA50 < EMA200",
            vix=vix,
        )

    # Default: sideways
    sideways_note = "mixed: between EMAs or short-term vol elevated"
    if vix_caps_to_sideways:
        sideways_note = f"VIX {vix:.1f} > {VIX_SIDEWAYS} caps regime to sideways"
    return RegimeReading(
        regime=Regime.SIDEWAYS,
        spy_close=last, ema_50=ema50, ema_200=ema200,
        vol_annualized_pct=vol_20d, confidence="medium",
        notes=sideways_note,
        vix=vix,
    )


def detect_regime(
    market: MarketDataClient,
    *,
    vix: float | None = None,
    vol_threshold_pct: float = DEFAULT_VOL_THRESHOLD_PCT,
) -> RegimeReading:
    """Live regime detection: fetch SPY bars and analyze with optional VIX override."""
    spy_bars = market.get_daily_bars("SPY", lookback_days=250)
    return detect_regime_from_bars(
        spy_bars, vix=vix, vol_threshold_pct=vol_threshold_pct,
    )
