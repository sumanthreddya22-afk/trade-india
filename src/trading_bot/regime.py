"""Live market regime detection.

Uses SPY bars to compute trend (50d vs 200d EMA), realized vol (10d), and a
VIX proxy via the rolling 20d standard deviation of SPY daily returns.

Returns one of: trending_up | trending_down | sideways | risk_off.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from trading_bot.market_data import MarketDataClient


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


def detect_regime_from_bars(spy_bars: pd.DataFrame) -> RegimeReading:
    """Pure function: compute regime from a SPY bars dataframe."""
    if len(spy_bars) < 60:
        return RegimeReading(
            regime=Regime.SIDEWAYS,
            spy_close=float(spy_bars["close"].iloc[-1]) if len(spy_bars) else 0.0,
            ema_50=0.0,
            ema_200=0.0,
            vol_annualized_pct=0.0,
            confidence="low",
            notes=f"only {len(spy_bars)} bars — insufficient for confident regime",
        )

    close = spy_bars["close"]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=min(200, len(close)), adjust=False).mean().iloc[-1]
    last = float(close.iloc[-1])

    # Realized annualized volatility from log returns over the last 20 days.
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) >= 20:
        vol_20d = float(log_ret.tail(20).std(ddof=1) * np.sqrt(252) * 100)
    else:
        vol_20d = float(log_ret.std(ddof=1) * np.sqrt(252) * 100) if len(log_ret) else 0.0

    # Risk-off: realized vol over 30% annualized OR sharp recent drawdown
    recent_drawdown = float((close.iloc[-1] - close.tail(20).max()) / close.tail(20).max() * 100)
    if vol_20d > 30 or recent_drawdown < -10:
        return RegimeReading(
            regime=Regime.RISK_OFF,
            spy_close=last,
            ema_50=float(ema50),
            ema_200=float(ema200),
            vol_annualized_pct=vol_20d,
            confidence="high",
            notes=f"vol {vol_20d:.1f}% > 30% or 20d drawdown {recent_drawdown:.1f}% < -10%",
        )

    # Trend up: above both EMAs and EMA50 > EMA200 (golden cross territory)
    if last > ema50 and ema50 > ema200 and vol_20d < 25:
        return RegimeReading(
            regime=Regime.TRENDING_UP,
            spy_close=last,
            ema_50=float(ema50),
            ema_200=float(ema200),
            vol_annualized_pct=vol_20d,
            confidence="high",
            notes="close > EMA50 > EMA200, vol calm",
        )

    # Trend down: below both EMAs
    if last < ema50 and ema50 < ema200:
        return RegimeReading(
            regime=Regime.TRENDING_DOWN,
            spy_close=last,
            ema_50=float(ema50),
            ema_200=float(ema200),
            vol_annualized_pct=vol_20d,
            confidence="high",
            notes="close < EMA50 < EMA200",
        )

    # Default: sideways (mixed signals)
    return RegimeReading(
        regime=Regime.SIDEWAYS,
        spy_close=last,
        ema_50=float(ema50),
        ema_200=float(ema200),
        vol_annualized_pct=vol_20d,
        confidence="medium",
        notes="mixed: between EMAs or short-term vol elevated",
    )


def detect_regime(market: MarketDataClient) -> RegimeReading:
    """Live regime detection by fetching SPY bars and analyzing them."""
    spy_bars = market.get_daily_bars("SPY", lookback_days=250)
    return detect_regime_from_bars(spy_bars)
