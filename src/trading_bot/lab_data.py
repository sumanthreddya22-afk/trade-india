"""Centralized read-only views into the lab + supervision state for the
dashboard and email digest. All queries go against state.db.

Single source of truth so dashboard and email show identical numbers.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.state_db import (
    AnthropicCostLog,
    CalibrationRun,
    CostHalt,
    EvolutionRun,
    FallbackFlag,
    HoldSpyTransitionState,
    Leaderboard,
    PromoterHalt,
    RoleKpi,
    RoleRun,
    TemplateProposal,
)


# ============================================================================
# Strategy Mode
# ============================================================================


@dataclass
class StrategyModeView:
    is_fallback: bool
    set_at: dt.datetime
    set_by: str
    reason: str | None
    days_in_state: int

    @property
    def label(self) -> str:
        return "FALLBACK" if self.is_fallback else "ACTIVE"

    @property
    def color(self) -> str:
        return "amber" if self.is_fallback else "green"


def strategy_mode(session: Session) -> StrategyModeView | None:
    row = (
        session.query(FallbackFlag).order_by(desc(FallbackFlag.set_at)).first()
    )
    if row is None:
        return None
    set_at = row.set_at if row.set_at.tzinfo else row.set_at.replace(tzinfo=dt.timezone.utc)
    days = max(0, (dt.datetime.now(dt.timezone.utc) - set_at).days)
    return StrategyModeView(
        is_fallback=bool(row.fallback_active),
        set_at=set_at,
        set_by=row.set_by,
        reason=row.reason,
        days_in_state=days,
    )


def hold_spy_transition(session: Session) -> dict | None:
    """If a Hold-SPY transition is in progress, return its state."""
    row = (
        session.query(HoldSpyTransitionState)
        .order_by(desc(HoldSpyTransitionState.id))
        .first()
    )
    if row is None or row.day_index >= 5:
        return None
    return {
        "phase": row.phase,
        "day_index": row.day_index,
        "days_remaining": 5 - row.day_index,
        "last_action_at": row.last_action_at,
    }


# ============================================================================
# Halts
# ============================================================================


@dataclass
class HaltView:
    kind: str  # "promoter" | "cost"
    halted_until: dt.datetime
    reason: str
    set_by: str | None
    hours_remaining: float


def active_halts(session: Session) -> list[HaltView]:
    now = dt.datetime.now(dt.timezone.utc)
    out: list[HaltView] = []
    for row in (
        session.query(PromoterHalt)
        .filter(PromoterHalt.halted_until > now)
        .all()
    ):
        until = row.halted_until if row.halted_until.tzinfo else row.halted_until.replace(tzinfo=dt.timezone.utc)
        out.append(
            HaltView(
                kind="promoter",
                halted_until=until,
                reason=row.reason,
                set_by=row.set_by,
                hours_remaining=max(0.0, (until - now).total_seconds() / 3600),
            )
        )
    for row in (
        session.query(CostHalt).filter(CostHalt.halted_until > now).all()
    ):
        until = row.halted_until if row.halted_until.tzinfo else row.halted_until.replace(tzinfo=dt.timezone.utc)
        out.append(
            HaltView(
                kind="cost",
                halted_until=until,
                reason=row.reason,
                set_by=None,
                hours_remaining=max(0.0, (until - now).total_seconds() / 3600),
            )
        )
    return out


# ============================================================================
# Lab Evolution
# ============================================================================


@dataclass
class LabEvolutionView:
    # Most-recent attempt — may be a 0-trial no-op when nothing changed.
    last_run_started_at: dt.datetime | None
    last_run_finished_at: dt.datetime | None
    last_run_n_trials: int
    last_run_best_fitness: float | None
    last_run_template: str | None
    last_run_promoted: bool
    # Most-recent run with n_trials > 0 — what the operator usually wants to
    # see ("the last time this thing actually did work"). Equal to last_run_*
    # when the latest attempt was productive; differs when today's run was a
    # no-op (e.g., params hadn't drifted; nothing new to search). Without this
    # split, the card showed "0 trials / —" on no-op days even though the
    # leaderboard still had real numbers from yesterday.
    last_productive_run_started_at: dt.datetime | None
    last_productive_run_n_trials: int
    last_productive_run_best_fitness: float | None
    last_productive_run_template: str | None
    last_productive_run_promoted: bool
    top_leaderboard: list[dict]


def lab_evolution(session: Session) -> LabEvolutionView:
    last_run = (
        session.query(EvolutionRun).order_by(desc(EvolutionRun.started_at)).first()
    )
    last_productive = (
        session.query(EvolutionRun)
        .filter(EvolutionRun.n_trials > 0)
        .order_by(desc(EvolutionRun.started_at))
        .first()
    )
    top = (
        session.query(Leaderboard)
        .order_by(desc(Leaderboard.fitness_score))
        .limit(5)
        .all()
    )
    leaderboard = [
        {
            "template": r.template_name,
            "alpha_vs_spy_x": r.alpha_vs_spy_x,
            "sortino": r.sortino,
            "max_dd_pct": r.max_dd_pct,
            "fitness_score": r.fitness_score,
            "folds": f"{r.folds_passed}/{r.folds_total}",
            "recorded_at": r.recorded_at,
        }
        for r in top
    ]
    if last_run is None:
        return LabEvolutionView(
            last_run_started_at=None, last_run_finished_at=None,
            last_run_n_trials=0, last_run_best_fitness=None,
            last_run_template=None, last_run_promoted=False,
            last_productive_run_started_at=None,
            last_productive_run_n_trials=0,
            last_productive_run_best_fitness=None,
            last_productive_run_template=None,
            last_productive_run_promoted=False,
            top_leaderboard=leaderboard,
        )
    return LabEvolutionView(
        last_run_started_at=last_run.started_at,
        last_run_finished_at=last_run.finished_at,
        last_run_n_trials=last_run.n_trials,
        last_run_best_fitness=last_run.best_fitness,
        last_run_template=last_run.template_name,
        last_run_promoted=bool(last_run.auto_promoted),
        last_productive_run_started_at=(
            last_productive.started_at if last_productive else None
        ),
        last_productive_run_n_trials=(
            last_productive.n_trials if last_productive else 0
        ),
        last_productive_run_best_fitness=(
            last_productive.best_fitness if last_productive else None
        ),
        last_productive_run_template=(
            last_productive.template_name if last_productive else None
        ),
        last_productive_run_promoted=(
            bool(last_productive.auto_promoted) if last_productive else False
        ),
        top_leaderboard=leaderboard,
    )


# ============================================================================
# Calibrator
# ============================================================================


@dataclass
class CalibratorView:
    latest_corr: float | None
    latest_severity: str
    latest_n: int
    latest_at: dt.datetime | None
    history: list[dict]


def calibrator(session: Session, *, history_days: int = 30) -> CalibratorView:
    latest = (
        session.query(CalibrationRun)
        .order_by(desc(CalibrationRun.recorded_at))
        .first()
    )
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=history_days)
    history_rows = (
        session.query(CalibrationRun)
        .filter(CalibrationRun.recorded_at >= cutoff)
        .order_by(CalibrationRun.recorded_at)
        .all()
    )
    if latest is None:
        return CalibratorView(
            latest_corr=None,
            latest_severity="never_run",
            latest_n=0,
            latest_at=None,
            history=[],
        )
    return CalibratorView(
        latest_corr=latest.spearman_corr,
        latest_severity=latest.severity,
        latest_n=latest.n_trades,
        latest_at=latest.recorded_at,
        history=[
            {
                "recorded_at": r.recorded_at,
                "corr": r.spearman_corr,
                "severity": r.severity,
            }
            for r in history_rows
        ],
    )


# ============================================================================
# LLM Spend (Anthropic)
# ============================================================================


@dataclass
class LlmSpendView:
    month_to_date_usd: float
    monthly_cap_usd: float
    pct_used: float
    n_calls_mtd: int
    n_calls_30d: int
    last_call_at: dt.datetime | None
    most_used_model: str | None


def llm_spend(session: Session) -> LlmSpendView:
    from trading_bot.cost_tracker import monthly_cap_usd, monthly_spend

    now = dt.datetime.now(dt.timezone.utc)
    mtd = monthly_spend(session)
    cap = monthly_cap_usd()
    month_start = dt.datetime(now.year, now.month, 1, tzinfo=dt.timezone.utc)
    n_mtd = (
        session.query(AnthropicCostLog)
        .filter(AnthropicCostLog.called_at >= month_start)
        .count()
    )
    cutoff_30 = now - dt.timedelta(days=30)
    n_30d = (
        session.query(AnthropicCostLog)
        .filter(AnthropicCostLog.called_at >= cutoff_30)
        .count()
    )
    last_row = (
        session.query(AnthropicCostLog)
        .order_by(desc(AnthropicCostLog.called_at))
        .first()
    )
    # Most-used model month-to-date
    rows_by_model = (
        session.query(AnthropicCostLog)
        .filter(AnthropicCostLog.called_at >= month_start)
        .all()
    )
    counts: dict[str, int] = {}
    for r in rows_by_model:
        counts[r.model] = counts.get(r.model, 0) + 1
    top_model = max(counts.items(), key=lambda kv: kv[1])[0] if counts else None

    return LlmSpendView(
        month_to_date_usd=mtd,
        monthly_cap_usd=cap,
        pct_used=(mtd / cap * 100) if cap > 0 else 0.0,
        n_calls_mtd=n_mtd,
        n_calls_30d=n_30d,
        last_call_at=last_row.called_at if last_row else None,
        most_used_model=top_model,
    )


# ============================================================================
# Role health
# ============================================================================


@dataclass
class RoleHealthRow:
    role_name: str
    runs_today: int
    runs_30d: int
    success_rate_pct: float
    last_run_at: dt.datetime | None
    last_status: str
    last_error: str | None


def role_health(session: Session) -> list[RoleHealthRow]:
    """One row per role, last 30 days.

    "Today" is computed against America/New_York midnight (the operator's
    local day) — not UTC. A role that fires at 15:30 ET shows up under
    today even though its UTC timestamp may already be the next calendar
    day. The previous implementation used UTC midnight and silently
    showed today=0 for any post-20:00 ET activity.
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = dt.datetime.now(dt.timezone.utc)
    now_et = now.astimezone(et)
    today_start_et = dt.datetime.combine(now_et.date(), dt.time.min, tzinfo=et)
    today_start_utc = today_start_et.astimezone(dt.timezone.utc)
    cutoff_30 = now - dt.timedelta(days=30)

    # Distinct role names that have any rows in 30d
    rows = (
        session.query(RoleRun)
        .filter(RoleRun.started_at >= cutoff_30)
        .order_by(desc(RoleRun.started_at))
        .all()
    )
    by_role: dict[str, list[RoleRun]] = {}
    for r in rows:
        by_role.setdefault(r.role_name, []).append(r)

    out: list[RoleHealthRow] = []
    for name, runs in by_role.items():
        n_30 = len(runs)
        n_today = 0
        for r in runs:
            ts = r.started_at
            # SQLite round-trips DateTime(timezone=True) as naive — assume UTC.
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if ts >= today_start_utc:
                n_today += 1
        n_ok = sum(1 for r in runs if r.status == "ok")
        latest = runs[0]
        out.append(
            RoleHealthRow(
                role_name=name,
                runs_today=n_today,
                runs_30d=n_30,
                success_rate_pct=(n_ok / n_30 * 100) if n_30 else 0.0,
                last_run_at=latest.started_at,
                last_status=latest.status,
                last_error=(
                    (latest.error_text or "")[:200]
                    if latest.status != "ok"
                    else None
                ),
            )
        )
    out.sort(key=lambda r: r.role_name)
    return out


# ============================================================================
# Template Proposals
# ============================================================================


@dataclass
class ProposalRow:
    name: str
    expected_regime: str
    proposed_at: dt.datetime
    review_status: str
    rationale_short: str
    accepted_at: dt.datetime | None


def recent_proposals(session: Session, *, limit: int = 5) -> list[ProposalRow]:
    rows = (
        session.query(TemplateProposal)
        .order_by(desc(TemplateProposal.proposed_at))
        .limit(limit)
        .all()
    )
    return [
        ProposalRow(
            name=r.name,
            expected_regime=r.expected_regime,
            proposed_at=r.proposed_at,
            review_status=r.review_status,
            rationale_short=(r.rationale or "")[:120],
            accepted_at=r.accepted_at,
        )
        for r in rows
    ]
