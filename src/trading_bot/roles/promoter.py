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
from trading_bot.state_db import EvolutionRun, PromoterHalt, RoleRun


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

    def __init__(self, *, engine, active_path: str | Path = "data/paper_active.json",
                 notify: bool = False):
        super().__init__(engine=engine)
        self.active_path = Path(active_path)
        # When True, emit a Strategy Promotion email + record a lab_promotions
        # row on every successful promotion. Production lab process should
        # construct with notify=True; tests and dry-runs default to False so
        # they never write to production state.db or send real emails.
        self._notify = notify

    def _active_halt(self) -> dict | None:
        """Return halt info dict if any active halt window covers now, else None."""
        now = dt.datetime.now(dt.timezone.utc)
        with Session(self.engine) as session:
            row = (
                session.query(PromoterHalt)
                .filter(PromoterHalt.halted_until > now)
                .order_by(PromoterHalt.set_at.desc())
                .first()
            )
            if row is None:
                return None
            return {
                "halted_until": row.halted_until,
                "reason": row.reason,
                "set_by": row.set_by,
            }

    def _do_work(self, ctx):
        # Phase 3.5: respect Calibrator's halt window.
        halt = self._active_halt()
        if halt is not None:
            return {
                "promoted": False,
                "reason": "halted_by_calibrator",
                "halted_until": halt["halted_until"].isoformat(),
                "halt_reason": halt["reason"],
            }

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

        promote_atomically(self.active_path, candidate, notify=self._notify)
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
