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

from trading_bot.decision_lessons import recent_lessons_text
from trading_bot.leaderboard import current_best, top_n
from trading_bot.promotion import (
    PromotionCandidate,
    promote_atomically,
    should_promote,
)
from trading_bot.promotion_debate import run_promotion_debate
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
                 notify: bool = False, debate_enabled: bool = True):
        super().__init__(engine=engine)
        self.active_path = Path(active_path)
        # When True, emit a Strategy Promotion email + record a lab_promotions
        # row on every successful promotion. Production lab process should
        # construct with notify=True; tests and dry-runs default to False so
        # they never write to production state.db or send real emails.
        self._notify = notify
        # Bull/bear/judge debate gate (3 LLM calls per candidate that has
        # already cleared the fitness + delta gates). Fail-open: missing
        # creds / budget halt / SDK errors all skip the debate so promotion
        # falls back to the prior behaviour.
        self._debate_enabled = debate_enabled

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

        # Debate gate (additive; fails open). Run only after the existing
        # fitness + delta gates have already cleared so cost is bounded.
        debate_outcome: dict | None = None
        if self._debate_enabled:
            verdict = self._run_debate(candidate)
            if verdict is not None:
                debate_outcome = {
                    "recommendation": verdict.recommendation,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                }
                if verdict.recommendation == "block" and verdict.confidence in (
                    "high",
                    "medium",
                ):
                    return {
                        "promoted": False,
                        "reason": "blocked_by_debate",
                        "candidate_fitness": candidate.fitness,
                        "current_fitness": info.get("current_fitness"),
                        "debate": debate_outcome,
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
        out = {
            "promoted": True,
            "from_fitness": info.get("current_fitness"),
            "to_fitness": candidate.fitness,
            "delta_pct": info.get("delta_pct"),
            "template": candidate.template,
        }
        if debate_outcome is not None:
            out["debate"] = debate_outcome
        return out

    def _run_debate(self, candidate: PromotionCandidate):
        """Build leaderboard + lessons context, then run the 3-call debate."""
        with Session(self.engine) as session:
            top = top_n(session, n=5)
        leaderboard_lines = [
            f"  {r.template_name:24s} fitness={r.fitness_score:.3f} "
            f"alpha={r.alpha_vs_spy_x:.2f}x sortino={r.sortino:.2f} "
            f"dd={r.max_dd_pct:.1f}%"
            for r in top
        ]
        leaderboard_context = "\n".join(leaderboard_lines)
        try:
            lessons_block = recent_lessons_text(
                self.engine, strategy=candidate.template, n_focused=4, n_cross=4
            )
        except Exception:
            lessons_block = ""
        return run_promotion_debate(
            self.engine,
            candidate,
            leaderboard_context=leaderboard_context,
            lessons_block=lessons_block,
            role_name=self.name,
        )

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
