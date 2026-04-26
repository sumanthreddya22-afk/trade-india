"""Strategy lanes: independent candidate-scoring strategies that run in parallel
over the stage-1 shortlist. Each lane returns LaneCandidate objects; the
stage-2 orchestrator merges and dedupes across lanes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from trading_bot.screener import RankedCandidate


@dataclass(frozen=True)
class LaneCandidate:
    symbol: str
    lane: str
    conviction: float  # 0.0–1.0
    reason: str
    source_score: float  # the underlying strategy-specific score


class Lane(Protocol):
    name: str

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        """Return the subset of ranked candidates this lane endorses, with conviction."""
        ...


from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands


class MomentumLane:
    """RSI 55–70, MACD bullish, price > 20-EMA, 5d return > 0. Mirrors the
    Plan-1 momentum rules but applied to ranked-shortlist input.
    """
    name = "momentum"

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        out: list[LaneCandidate] = []
        for c in ranked:
            bars = bar_loader(c.symbol)
            if len(bars) < 26:
                continue
            close = bars["close"]
            rsi = float(RSIIndicator(close=close, window=14).rsi().iloc[-1])
            macd_obj = MACD(close=close)
            macd_line = float(macd_obj.macd().iloc[-1])
            macd_signal = float(macd_obj.macd_signal().iloc[-1])
            ema20 = float(EMAIndicator(close=close, window=20).ema_indicator().iloc[-1])
            last = float(close.iloc[-1])

            if not (55 <= rsi <= 70):
                continue
            if macd_line <= macd_signal:
                continue
            if last <= ema20:
                continue
            if c.five_day_return_pct <= 0:
                continue

            # Conviction: how cleanly the trend signal lines up. Range 0.4–0.9.
            conviction = 0.4
            conviction += 0.2 * min((rsi - 55) / 15, 1.0)        # RSI position in band
            conviction += 0.2 * min((macd_line - macd_signal), 1.0)
            conviction += 0.1 * min(c.relative_5d_pct / 5.0, 1.0)
            conviction = max(0.0, min(conviction, 0.9))

            out.append(LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=conviction,
                reason=f"RSI {rsi:.0f}, MACD>signal, price>EMA20, 5d {c.five_day_return_pct:.1f}%",
                source_score=c.score,
            ))
        return out


class MeanReversionLane:
    """RSI < 30 (oversold) AND price < lower Bollinger Band (2σ, 20-day).
    Conviction higher when farther below the band.
    """
    name = "mean_reversion"

    def evaluate(
        self,
        ranked: list[RankedCandidate],
        bar_loader: Callable[[str], pd.DataFrame],
    ) -> list[LaneCandidate]:
        out: list[LaneCandidate] = []
        for c in ranked:
            bars = bar_loader(c.symbol)
            if len(bars) < 22:
                continue
            close = bars["close"]
            rsi = float(RSIIndicator(close=close, window=14).rsi().iloc[-1])
            bb = BollingerBands(close=close, window=20, window_dev=2)
            lower = float(bb.bollinger_lband().iloc[-1])
            last = float(close.iloc[-1])

            if rsi >= 30:
                continue
            if last >= lower:
                continue

            # Conviction grows with how much we're below the band, capped at 0.85.
            below_pct = (lower - last) / lower if lower else 0.0
            conviction = 0.4 + min(below_pct * 5.0, 0.45)
            out.append(LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=conviction,
                reason=f"RSI {rsi:.0f}, price ${last:.2f} < lower BB ${lower:.2f}",
                source_score=c.score,
            ))
        return out
