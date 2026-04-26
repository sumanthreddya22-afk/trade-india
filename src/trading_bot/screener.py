"""Two-stage screener: stage-1 composite scoring on the full filtered universe;
stage-2 strategy-lane scoring on the shortlist (delegated to strategy_lanes.py)."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

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


def build_stage1_shortlist(
    universe: list[LiquidAsset],
    *,
    bar_loader: Callable[[str], pd.DataFrame],
    top_n: int = 100,
    benchmark_symbol: str = "SPY",
) -> list[RankedCandidate]:
    """Score the entire universe and return top_n by composite score.

    Skips assets whose bars are unavailable or insufficient (< 6 rows).
    """
    benchmark_bars = bar_loader(benchmark_symbol)
    benchmark_5d = 0.0
    if len(benchmark_bars) >= 6:
        last = float(benchmark_bars["close"].iloc[-1])
        fifth = float(benchmark_bars["close"].iloc[-6])
        if fifth:
            benchmark_5d = ((last - fifth) / fifth) * 100

    candidates: list[RankedCandidate] = []
    for asset in universe:
        bars = bar_loader(asset.symbol)
        if len(bars) < 6:
            continue
        candidates.append(score_candidate(asset, bars=bars, benchmark_5d_pct=benchmark_5d))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]


from trading_bot.strategy_lanes import Lane, LaneCandidate  # noqa: E402


@dataclass(frozen=True)
class MergedCandidate:
    symbol: str
    asset_class: str
    sector_tags: tuple[str, ...]
    last_price: Decimal
    score: float
    conviction: float  # max conviction across lanes that endorsed it
    lane_attribution: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Stage2Result:
    candidates: list[MergedCandidate]
    lane_counts: dict[str, int]


def run_stage2(
    shortlist: list[RankedCandidate],
    *,
    lanes: list[Lane],
    bar_loader: Callable[[str], pd.DataFrame],
) -> Stage2Result:
    """Run all lanes in parallel over the shortlist, merge, dedupe by symbol.

    Conviction takes the max across lanes; lane attribution preserves which lanes
    endorsed each name; reasons concatenate.
    """
    with ThreadPoolExecutor(max_workers=max(1, len(lanes))) as ex:
        results = list(ex.map(lambda lane: lane.evaluate(shortlist, bar_loader), lanes))

    by_symbol: dict[str, list[LaneCandidate]] = {}
    lane_counts: dict[str, int] = {}
    for lane_out, lane in zip(results, lanes, strict=False):
        lane_counts[lane.name] = len(lane_out)
        for lc in lane_out:
            by_symbol.setdefault(lc.symbol, []).append(lc)

    short_by_sym = {c.symbol: c for c in shortlist}
    merged: list[MergedCandidate] = []
    for symbol, lane_cands in by_symbol.items():
        ref = short_by_sym.get(symbol)
        if ref is None:
            continue
        max_conviction = max(lc.conviction for lc in lane_cands)
        merged.append(MergedCandidate(
            symbol=symbol,
            asset_class=ref.asset_class,
            sector_tags=ref.sector_tags,
            last_price=ref.last_price,
            score=ref.score,
            conviction=max_conviction,
            lane_attribution=tuple(sorted({lc.lane for lc in lane_cands})),
            reasons=tuple(lc.reason for lc in lane_cands),
        ))

    merged.sort(key=lambda m: (m.conviction, m.score), reverse=True)
    return Stage2Result(candidates=merged, lane_counts=lane_counts)


def render_opportunities_snapshot(
    result: Stage2Result,
    *,
    generated_at: datetime,
    shortlist: "list[RankedCandidate] | None" = None,
) -> str:
    lines = [
        "# Opportunities (Stage-2)",
        "",
        f"Generated: {generated_at.isoformat(timespec='seconds')}",
        f"Total endorsed: {len(result.candidates)}",
        "",
        "## Lane Counts",
        "",
    ]
    for lane, count in result.lane_counts.items():
        lines.append(f"- {lane}: {count}")
    lines.extend(["", "## Ranked Candidates", ""])
    for idx, c in enumerate(result.candidates, start=1):
        lines.append(f"### {idx}. {c.symbol} ({c.asset_class})")
        lines.append("")
        lines.append(f"- Lanes: {', '.join(c.lane_attribution)}")
        lines.append(f"- Conviction: {c.conviction:.2f}")
        lines.append(f"- Stage-1 score: {c.score:.2f}")
        lines.append(f"- Last price: ${c.last_price}")
        if c.sector_tags:
            lines.append(f"- Sectors: {', '.join(c.sector_tags)}")
        for r in c.reasons:
            lines.append(f"- Why: {r}")
        lines.append("")
    # When a shortlist is provided and there are no stage-2 endorsed candidates,
    # fall back to listing stage-1 shortlist members so the file is non-empty.
    if shortlist and not result.candidates:
        lines.extend(["", "## Stage-1 Shortlist (no lane endorsements)", ""])
        for idx, c in enumerate(shortlist, start=1):
            lines.append(f"### {idx}. {c.symbol} ({c.asset_class})")
            lines.append("")
            lines.append(f"- Stage-1 score: {c.score:.2f}")
            lines.append(f"- Last price: ${c.last_price}")
            if c.sector_tags:
                lines.append(f"- Sectors: {', '.join(c.sector_tags)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_opportunities_snapshot(
    result: Stage2Result,
    path: Path,
    *,
    generated_at: datetime,
    shortlist: "list[RankedCandidate] | None" = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_opportunities_snapshot(result, generated_at=generated_at, shortlist=shortlist)
    )
