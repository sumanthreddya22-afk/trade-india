"""Two-stage screener: stage-1 composite scoring on the full filtered universe;
stage-2 strategy-lane scoring on the shortlist (delegated to strategy_lanes.py)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from trading_bot.universe import LiquidAsset


@dataclass(frozen=True)
class RankedCandidate:
    symbol: str
    asset_class: str
    sector_tags: tuple[str, ...]
    last_price: Decimal
    one_day_return_pct: float
    five_day_return_pct: float
    relative_5d_pct: float
    volume_ratio: float
    score: float


def score_candidate(
    asset: LiquidAsset,
    *,
    bars: pd.DataFrame,
    benchmark_5d_pct: float,
) -> RankedCandidate:
    """Composite score = 1d_return * 1.4 + relative_5d + min(vol_ratio, 3) * 2.

    Mirrors the codex scoring formula (validated empirically), with `relative_5d`
    as the SPY-relative 5-day move so a "rising tide" doesn't lift all candidates.
    """
    if len(bars) < 6:
        return RankedCandidate(
            symbol=asset.symbol, asset_class=asset.asset_class,
            sector_tags=asset.sector_tags, last_price=asset.last_price,
            one_day_return_pct=0.0, five_day_return_pct=0.0,
            relative_5d_pct=0.0, volume_ratio=1.0, score=-1e9,
        )

    last = float(bars["close"].iloc[-1])
    prev = float(bars["close"].iloc[-2])
    fifth = float(bars["close"].iloc[-6])
    one_day = ((last - prev) / prev) * 100 if prev else 0.0
    five_day = ((last - fifth) / fifth) * 100 if fifth else 0.0
    relative_5d = five_day - benchmark_5d_pct

    avg_vol = float(bars["volume"].iloc[-6:-1].mean())
    last_vol = float(bars["volume"].iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    score = one_day * 1.4 + relative_5d + min(vol_ratio, 3.0) * 2.0

    return RankedCandidate(
        symbol=asset.symbol,
        asset_class=asset.asset_class,
        sector_tags=asset.sector_tags,
        last_price=asset.last_price,
        one_day_return_pct=one_day,
        five_day_return_pct=five_day,
        relative_5d_pct=relative_5d,
        volume_ratio=vol_ratio,
        score=score,
    )
