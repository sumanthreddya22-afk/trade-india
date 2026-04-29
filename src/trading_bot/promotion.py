"""Atomic auto-promote of paper_active.json.

Two gates must clear before a candidate is written:
  1. The promotion gate (alpha_vs_spy_x, sortino, max_dd_pct hard thresholds).
  2. A 10% fitness-delta gate vs the currently active config — prevents
     leaderboard noise from flipping the active config every night.

Atomicity is the same tmp+rename pattern used by state_heartbeat.write_heartbeat.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from trading_bot.fitness import FitnessScore, compute_fitness, promotion_gate_check

MIN_FITNESS_DELTA = 0.10  # candidate must beat current by at least 10%


@dataclass
class PromotionCandidate:
    template: str
    params: dict
    fitness: float
    alpha_vs_spy_x: float
    sortino: float
    max_dd_pct: float

    def to_score(self) -> FitnessScore:
        return compute_fitness(
            alpha_vs_spy_x=self.alpha_vs_spy_x,
            sortino=self.sortino,
            max_dd_pct=self.max_dd_pct,
        )


def should_promote(
    active_path: str | Path, candidate: PromotionCandidate
) -> tuple[bool, dict]:
    """Returns (decision, info_dict). info_dict is logged for audit."""
    p = Path(active_path)
    info: dict = {
        "candidate_fitness": candidate.fitness,
        "candidate_template": candidate.template,
    }
    score = candidate.to_score()
    if not promotion_gate_check(score):
        info["reason"] = "promotion gate failed (alpha/sortino/dd thresholds)"
        info["alpha_vs_spy_x"] = candidate.alpha_vs_spy_x
        info["sortino"] = candidate.sortino
        info["max_dd_pct"] = candidate.max_dd_pct
        return False, info

    if not p.exists():
        info["reason"] = "no active config — first-time promotion"
        info["delta_pct"] = float("inf")
        return True, info

    active = json.loads(p.read_text())
    current_fitness = active.get("fitness_at_promotion")
    info["current_fitness"] = current_fitness

    if current_fitness is None or current_fitness <= 0:
        info["reason"] = "no incumbent fitness — promoting"
        info["delta_pct"] = float("inf")
        return True, info

    delta = (candidate.fitness - current_fitness) / abs(current_fitness)
    info["delta_pct"] = delta * 100.0
    if delta < MIN_FITNESS_DELTA:
        info["reason"] = (
            f"delta {delta * 100:.2f}% < gate {MIN_FITNESS_DELTA * 100:.0f}%"
        )
        return False, info
    info["reason"] = "promotion gate + delta gate cleared"
    return True, info


def promote_atomically(
    active_path: str | Path,
    candidate: PromotionCandidate,
    *,
    notify: bool = False,
) -> None:
    """Rewrite active config with the candidate's template+params+fitness.

    Preserves all other keys (risk_caps, cadence, universe, etc.) unchanged.
    Atomic via tmp+rename so the daemon's mtime watcher never observes a
    partial write.

    `notify` controls the side effects beyond the file write:
      - When True: insert a `lab_promotions` row in production state.db AND
        send a Strategy Promotion email via `send_logged`.
      - When False (default): file-write only — safe for tests and dry runs.
    Production callers (`PromoterRole`, `promote_cli`) opt in with
    `notify=True`. Tests get the safe default.
    """
    p = Path(active_path)
    if p.exists():
        cfg = json.loads(p.read_text())
    else:
        cfg = {}
    cfg["active_template"] = candidate.template
    cfg["params"] = candidate.params
    cfg["fitness_at_promotion"] = candidate.fitness
    cfg["promoted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    cfg["promoted_by"] = "lab-promoter"
    cfg["version"] = (
        f"auto-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    )

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    os.replace(tmp, p)

    if not notify:
        return

    # Record the promotion in the lab_promotions table for first-24h validation.
    from trading_bot.lab_promotions import LabPromotionStore
    import datetime as _dt

    _store = LabPromotionStore()
    _prev = _store.latest()  # capture BEFORE recording so diff is real

    _store.record(
        promoted_at=_dt.datetime.now(_dt.timezone.utc),
        version=cfg["version"],
        template=cfg["active_template"],
        git_sha=cfg.get("git_sha", "unknown"),
        fitness=float(cfg["fitness_at_promotion"]),
        params=cfg.get("params", {}),
        risk_caps=cfg.get("risk_caps", {}),
    )

    # Send strategy promotion email. Failure must not crash the promotion path.
    try:
        from trading_bot.email_promotion import build_promotion_email
        from trading_bot.email_log import send_logged
        from trading_bot.email_sender import EmailSender
        from trading_bot.config import Settings, load_config
        from pathlib import Path as _Path

        _new_promo_dict = {
            "promoted_at": _dt.datetime.now(_dt.timezone.utc),
            "version": cfg["version"],
            "template": cfg["active_template"],
            "git_sha": cfg.get("git_sha", "unknown"),
            "fitness_at_promotion": float(cfg["fitness_at_promotion"]),
            "params": cfg.get("params", {}),
            "risk_caps": cfg.get("risk_caps", {}),
        }
        _email = build_promotion_email(promo=_new_promo_dict, prev=_prev)
        _settings = Settings()
        _cfg = load_config(_Path("strategy/config.yaml"))
        _sender = EmailSender(
            user=_settings.gmail_user,
            app_password=_settings.gmail_app_password,
            to=_cfg.email.to,
        )
        send_logged(
            sender=_sender,
            subject=_email.subject,
            html_body=_email.html_body,
            kind="promotion",
            recipient=_cfg.email.to,
        )
    except Exception as _exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "promotion email failed (promotion still recorded): %s", _exc
        )
