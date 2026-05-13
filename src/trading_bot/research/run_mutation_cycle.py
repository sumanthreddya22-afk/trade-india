"""Monthly mutation cycle driver.

Plan v4 §8 end-to-end:
  1. Propose candidates from the search space (budget-capped per family).
  2. For each candidate, run a backtest (caller supplies the function)
     and record the raw p-value via ``record_outcome``.
  3. Apply BH-FDR across the cycle.
  4. For survivors, hand them to ``run_research.run_cycle`` to emit a
     Tier-1 ``validation_artifact``.

For Phase 6 the backtest callable is injected — real market data
wiring is a separate operator step.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from trading_bot.registry.search_space import SearchSpace
from trading_bot.research.bh_fdr import BHFDRReport, apply as apply_bhfdr
from trading_bot.research.mutation_engine import (
    Candidate, DEFAULT_BUDGET_PER_FAMILY,
    propose_candidates, record_candidate, record_outcome,
)

BacktestT = Callable[[Candidate], tuple[float, Mapping]]
"""Caller supplies a backtest: given a Candidate, returns
``(raw_p_value, sanity_checks_dict)``."""


@dataclass(frozen=True)
class MutationCycleReport:
    cycle_id: str
    n_proposed: int
    n_backtested: int
    n_survivors: int
    bh_fdr: BHFDRReport
    candidates: tuple[Candidate, ...]


def run_cycle(
    conn: sqlite3.Connection,
    *,
    thesis_id: str,
    cycle_id: str,
    search_space: SearchSpace,
    backtest: BacktestT,
    mutation_ids: Optional[Sequence[str]] = None,
    budget_per_family: int = DEFAULT_BUDGET_PER_FAMILY,
    alpha: float = 0.10,
    rationale_lookup: Optional[Mapping[str, str]] = None,
    proposer: str = "mutation_engine",
    now: Optional[dt.datetime] = None,
) -> MutationCycleReport:
    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = propose_candidates(
        thesis_id=thesis_id, cycle_id=cycle_id,
        search_space=search_space, mutation_ids=mutation_ids,
        budget_per_family=budget_per_family, proposer=proposer,
        rationale_lookup=rationale_lookup,
    )
    n_backtested = 0
    for c in candidates:
        record_candidate(conn, c, now=now)
        p, checks = backtest(c)
        record_outcome(
            conn, candidate_id=c.candidate_id,
            raw_p_value=p, sanity_checks=checks, now=now,
        )
        n_backtested += 1

    report = apply_bhfdr(conn, cycle_id=cycle_id, alpha=alpha, now=now)
    return MutationCycleReport(
        cycle_id=cycle_id, n_proposed=len(candidates),
        n_backtested=n_backtested, n_survivors=report.n_survivors,
        bh_fdr=report, candidates=tuple(candidates),
    )


__all__ = ["BacktestT", "MutationCycleReport", "run_cycle"]
