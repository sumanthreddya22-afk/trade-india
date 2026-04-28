"""Leaderboard read/write helpers. Same shape as state_hwm.py."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.fitness import compute_fitness
from trading_bot.state_db import Leaderboard


def params_hash(params: dict[str, Any]) -> str:
    """Stable sha1 of canonical-JSON-encoded params (sorted keys)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def record_run(
    session: Session,
    *,
    template: str,
    params: dict[str, Any],
    alpha: float,
    sortino: float,
    dd: float,
    folds_passed: int,
    folds_total: int,
) -> None:
    score = compute_fitness(alpha_vs_spy_x=alpha, sortino=sortino, max_dd_pct=dd)
    session.add(
        Leaderboard(
            template_name=template,
            params_hash=params_hash(params),
            params_json=json.dumps(params, sort_keys=True),
            alpha_vs_spy_x=alpha,
            sortino=sortino,
            max_dd_pct=dd,
            folds_passed=folds_passed,
            folds_total=folds_total,
            fitness_score=score.fitness_score,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.commit()


def top_n(session: Session, *, n: int = 10) -> list[Leaderboard]:
    return list(
        session.query(Leaderboard)
        .order_by(desc(Leaderboard.fitness_score))
        .limit(n)
        .all()
    )


def current_best(session: Session) -> Leaderboard | None:
    return (
        session.query(Leaderboard)
        .order_by(desc(Leaderboard.fitness_score))
        .first()
    )
