"""Promoter — Tier 5 lab role.

Reads paper_active.json, fetches the leaderboard's top variant, evaluates
the promotion gate + 10% delta gate, and atomically rewrites the config
file when both clear. Otherwise returns a no-op result with the reason.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.leaderboard import current_best
from trading_bot.promotion import (
    PromotionCandidate,
    promote_atomically,
    should_promote,
)
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import EvolutionRun, RoleRun


class PromoterRole(BaseRole):
    name = "promoter"
    tier = 5
    process = "lab"
    job_description = (
        "Atomically rewrite paper_active.json when leaderboard top variant "
        "clears all gates by ≥ 10% delta vs current."
    )
    sla_seconds = 30
    upstream_roles = ["param_optimizer"]
    downstream_roles: list[str] = []

    def __init__(self, *, engine, active_path: str | Path = "data/paper_active.json"):
        super().__init__(engine=engine)
        self.active_path = Path(active_path)

    def _do_work(self, ctx):
        with Session(self.engine) as session:
            best = current_best(session)
        if best is None:
            return {"promoted": False, "reason": "no_candidate_in_leaderboard"}

        params = json.loads(best.params_json)
        candidate = PromotionCandidate(
            template=best.template_name,
            params=params,
            fitness=best.fitness_score,
            alpha_vs_spy_x=best.alpha_vs_spy_x,
            sortino=best.sortino,
            max_dd_pct=best.max_dd_pct,
        )

        ok, info = should_promote(self.active_path, candidate)
        if not ok:
            return {
                "promoted": False,
                "reason": info.get("reason", "no_candidate_above_gate"),
                "candidate_fitness": candidate.fitness,
                "current_fitness": info.get("current_fitness"),
            }

        promote_atomically(self.active_path, candidate)
        with Session(self.engine) as session:
            session.add(
                EvolutionRun(
                    started_at=dt.datetime.now(dt.timezone.utc),
                    finished_at=dt.datetime.now(dt.timezone.utc),
                    template_name=candidate.template,
                    n_trials=0,
                    best_fitness=candidate.fitness,
                    best_params_hash=None,
                    auto_promoted=1,
                    promotion_gate_pass=json.dumps(info),
                )
            )
            session.commit()
        return {
            "promoted": True,
            "from_fitness": info.get("current_fitness"),
            "to_fitness": candidate.fitness,
            "delta_pct": info.get("delta_pct"),
            "template": candidate.template,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            promotions = (
                session.query(EvolutionRun)
                .filter(
                    EvolutionRun.started_at >= cutoff, EvolutionRun.auto_promoted == 1
                )
                .count()
            )
            evaluations = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        rate = (promotions / evaluations * 100) if evaluations else 0.0
        return (
            "promotion_rate_pct",
            rate,
            f"{promotions} promotions / {evaluations} evaluations in last {lookback_days}d",
        )
