"""Param Optimizer — Tier 5 lab role.

Runs an optuna TPE search over PARAM_SPACE[template] for `n_trials`. Each
trial calls BacktestEngineerRole, computes fitness, records the variant
in `leaderboard`, and returns the fitness score as the optuna objective.

Records a single `evolution_runs` summary row at the end with best params.
"""
from __future__ import annotations

import datetime as dt
import json

import optuna
from sqlalchemy.orm import Session

from trading_bot.fitness import compute_fitness
from trading_bot.leaderboard import params_hash, record_run
from trading_bot.param_space import PARAM_SPACE
from trading_bot.roles.backtest_engineer import BacktestEngineerRole
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import EvolutionRun, RoleRun

# Quiet optuna's per-trial logging — we use structured logs at the call site.
optuna.logging.set_verbosity(optuna.logging.WARNING)


class ParamOptimizerRole(BaseRole):
    name = "param_optimizer"
    tier = 5
    process = "lab"
    job_description = (
        "Bayesian search via optuna over template parameter space. "
        "Records each variant in leaderboard. Default 100 trials."
    )
    sla_seconds = 4 * 60 * 60  # 4h budget
    upstream_roles = ["backtest_engineer"]
    downstream_roles = ["promoter"]

    def _do_work(self, ctx):
        template = ctx.get("template", "momentum")
        n_trials = ctx.get("n_trials", 100)
        start = ctx.get("start", dt.date(2024, 1, 1))
        end = ctx.get("end", dt.date(2026, 7, 1))
        n_folds = ctx.get("n_folds", 6)

        space = PARAM_SPACE.get(template, {})
        if not space:
            return {"n_trials": 0, "error": f"unknown template: {template}"}

        engineer = BacktestEngineerRole(engine=self.engine)
        run_started = dt.datetime.now(dt.timezone.utc)

        best_fitness: float | None = None
        best_params: dict | None = None
        successful_trials = 0

        def _objective(trial: optuna.Trial) -> float:
            params = _sample_params(trial, space)
            result = engineer.safe_run(
                ctx={
                    "template": template,
                    "params": params,
                    "start": start,
                    "end": end,
                    "n_folds": n_folds,
                }
            )
            if result.status != RoleStatus.OK:
                # Tell optuna to discard this trial
                raise optuna.TrialPruned()
            outputs = result.outputs
            score = compute_fitness(
                alpha_vs_spy_x=outputs["alpha_vs_spy_x"],
                sortino=outputs["sortino"],
                max_dd_pct=outputs["max_dd_pct"],
            )
            with Session(self.engine) as session:
                record_run(
                    session,
                    template=template,
                    params=params,
                    alpha=outputs["alpha_vs_spy_x"],
                    sortino=outputs["sortino"],
                    dd=outputs["max_dd_pct"],
                    folds_passed=outputs["folds_passed"],
                    folds_total=outputs["folds_total"],
                )
            nonlocal best_fitness, best_params, successful_trials
            successful_trials += 1
            if best_fitness is None or score.fitness_score > best_fitness:
                best_fitness = score.fitness_score
                best_params = params
            return score.fitness_score

        study = optuna.create_study(
            direction="maximize", sampler=optuna.samplers.TPESampler()
        )
        study.optimize(_objective, n_trials=n_trials, catch=())

        finished = dt.datetime.now(dt.timezone.utc)
        with Session(self.engine) as session:
            session.add(
                EvolutionRun(
                    started_at=run_started,
                    finished_at=finished,
                    template_name=template,
                    n_trials=successful_trials,
                    best_fitness=best_fitness,
                    best_params_hash=(
                        params_hash(best_params) if best_params else None
                    ),
                    auto_promoted=0,
                    promotion_gate_pass=(
                        json.dumps({"best_params": best_params})
                        if best_params
                        else None
                    ),
                )
            )
            session.commit()

        return {
            "template": template,
            "n_trials": successful_trials,
            "best_fitness": best_fitness,
            "best_params": best_params,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "search_runs",
            float(count),
            f"{count} optuna search runs in last {lookback_days}d",
        )


def _sample_params(trial: optuna.Trial, space: dict[str, tuple]) -> dict:
    params: dict = {}
    for name, spec in space.items():
        low, high, kind = spec
        if kind == "int":
            params[name] = trial.suggest_int(name, int(low), int(high))
        else:
            params[name] = trial.suggest_float(name, float(low), float(high))
    return params
