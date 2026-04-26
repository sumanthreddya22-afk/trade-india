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
