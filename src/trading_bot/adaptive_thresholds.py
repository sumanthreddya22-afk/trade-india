"""Phase E — Adaptive Thresholds: source-weight tuning from realised outcomes.

The Phase D lesson loop computed per-source winrates (which intel sources
drove winning vs losing entries). This module:

  1. ``propose_source_weights`` reads the latest DebateLesson, compares
     each source's winrate to its current weight in
     ``aggregator.SOURCE_WEIGHTS``, and proposes an updated weight bounded
     by ``[MIN_WEIGHT, MAX_WEIGHT]``. Sources with too few trades to be
     statistically meaningful are skipped.

  2. ``write_shadow_overrides`` persists each proposed weight as a
     SHADOW row in ``threshold_overrides``. Shadow rows do NOT feed
     live reads — the analyzer evaluates them for ~14 days, then the
     operator (or a future auto-promoter) flips ``shadow`` to False to
     promote.

  3. ``lookup_source_weight`` reads the live (non-shadow) override for a
     given source, falling back to ``SOURCE_WEIGHTS`` static config. The
     aggregator will call this on every event to use tuned weights.

Knob naming convention: ``source_weight:<source_name>``.

Sequential: aggregation queries run per-source one at a time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from trading_bot.intel.aggregator import SOURCE_WEIGHTS, DEFAULT_SOURCE_WEIGHT
from trading_bot.threshold_overrides import lookup as _override_lookup
from trading_bot.threshold_overrides import write_override


log = logging.getLogger(__name__)

# Bounded clamps prevent runaway tuning from a small streak.
MIN_WEIGHT = 0.5
MAX_WEIGHT = 6.0

# Statistical significance floor: don't tune a source until we have at
# least this many closed-trade outcomes.
MIN_TRADES_FOR_TUNING = 6


@dataclass(frozen=True)
class WeightProposal:
    source: str
    current_weight: float
    proposed_weight: float
    n_trades: int
    winrate: float
    avg_pnl_pct: float
    rationale: str


def _knob_name(source: str) -> str:
    return f"source_weight:{source}"


def lookup_source_weight(engine, source: str) -> float:
    """Read live (non-shadow) override for a source weight; fall back to
    the static SOURCE_WEIGHTS map. Used by ``aggregator.event_score`` to
    pick up tuned weights without changing aggregator code shape.
    """
    try:
        live = _override_lookup(engine, knob=_knob_name(source))
    except Exception:
        live = None
    if live is not None:
        return float(live)
    return SOURCE_WEIGHTS.get(source, DEFAULT_SOURCE_WEIGHT)


def _propose_weight_for_source(
    *,
    source: str,
    current_weight: float,
    n_trades: int,
    winrate: float,
    avg_pnl_pct: float,
) -> float:
    """Pure proposal function. Linear scaling around 50% winrate baseline:
    above 50% boosts the weight; below 50% trims it. Bounded by the
    [MIN_WEIGHT, MAX_WEIGHT] clamps.

    Heuristic: 100% winrate → 1.5x current; 0% winrate → 0.5x current.
    Keeps the adjustment conservative — single bad week doesn't kill a source.
    """
    # Scale factor: 0.5 at 0% winrate, 1.0 at 50%, 1.5 at 100%.
    scale = 0.5 + winrate
    proposed = current_weight * scale
    return max(MIN_WEIGHT, min(MAX_WEIGHT, proposed))


def propose_source_weights(
    engine,
) -> list[WeightProposal]:
    """Read latest DebateLesson, build proposals for each source meeting
    the minimum-trade floor. Returns an empty list when no lesson exists
    or when no sources have enough data.
    """
    from trading_bot.lesson_loop import latest_lesson
    import json as _json

    lesson = latest_lesson(engine)
    if lesson is None:
        return []
    try:
        per_source = _json.loads(lesson.per_source_winrate_json or "{}")
    except Exception:
        return []
    if not isinstance(per_source, dict):
        return []

    proposals: list[WeightProposal] = []
    for source, stats in per_source.items():
        if not isinstance(stats, dict):
            continue
        n = int(stats.get("n", 0))
        if n < MIN_TRADES_FOR_TUNING:
            continue
        winrate = float(stats.get("winrate", 0.0))
        avg_pnl = float(stats.get("avg_pnl_pct", 0.0))
        current = SOURCE_WEIGHTS.get(source, DEFAULT_SOURCE_WEIGHT)
        proposed = _propose_weight_for_source(
            source=source, current_weight=current,
            n_trades=n, winrate=winrate, avg_pnl_pct=avg_pnl,
        )
        delta_pct = (proposed - current) / current if current else 0.0
        rationale = (
            f"{n} trades, winrate={winrate*100:.0f}%, "
            f"avg_pnl={avg_pnl:+.2f}% → scale {scale_str(winrate)} "
            f"({delta_pct*100:+.0f}% from {current:.2f})"
        )
        proposals.append(WeightProposal(
            source=source,
            current_weight=current,
            proposed_weight=round(proposed, 3),
            n_trades=n,
            winrate=winrate,
            avg_pnl_pct=avg_pnl,
            rationale=rationale,
        ))
    return proposals


def scale_str(winrate: float) -> str:
    return f"{0.5 + winrate:.2f}x"


def write_shadow_overrides(
    engine,
    *,
    proposals: list[WeightProposal],
    set_by: str = "adaptive_thresholds",
) -> int:
    """Persist proposals as SHADOW rows in threshold_overrides. Live
    reads ignore shadow rows; analyzer evaluates after the shadow
    window closes. Returns the count written.
    """
    n = 0
    for p in proposals:
        # Skip writes that would no-op (proposed == current within rounding)
        if abs(p.proposed_weight - p.current_weight) < 0.05:
            continue
        try:
            write_override(
                engine,
                knob=_knob_name(p.source),
                value=p.proposed_weight,
                bounds_min=MIN_WEIGHT,
                bounds_max=MAX_WEIGHT,
                set_by=set_by,
                signal_summary={
                    "source": p.source,
                    "current_weight": p.current_weight,
                    "proposed_weight": p.proposed_weight,
                    "n_trades": p.n_trades,
                    "winrate": p.winrate,
                    "avg_pnl_pct": p.avg_pnl_pct,
                    "rationale": p.rationale,
                },
                shadow=True,
            )
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("adaptive_thresholds write_shadow failed for %s: %s", p.source, e)
    return n


def run_tuning_cycle(engine) -> dict:
    """Convenience: propose + persist in one call. Intended for the
    threshold_tuner role (or a Phase E sub-role) to invoke nightly.
    """
    proposals = propose_source_weights(engine)
    n_written = write_shadow_overrides(engine, proposals=proposals)
    return {
        "n_proposals": len(proposals),
        "n_shadow_written": n_written,
        "proposals": [
            {
                "source": p.source,
                "current": p.current_weight,
                "proposed": p.proposed_weight,
                "n_trades": p.n_trades,
                "winrate": p.winrate,
            }
            for p in proposals
        ],
    }
