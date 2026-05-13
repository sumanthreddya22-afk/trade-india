"""Validation artifact: one row per backtest run that passes (or fails)
a tier in policy/validation_policy.lock.

Plan v4 §4 + §13. Phase 4 ships the writer + the threshold checker.
Phase 5 ships the research factory that calls these helpers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash
from trading_bot.registry.schema import ensure_registry_tables

TIER_RESEARCH = "research_candidate"
TIER_PAPER = "paper_candidate"
TIER_LIVE = "live_candidate"
TIERS = (TIER_RESEARCH, TIER_PAPER, TIER_LIVE)

GATE_LENS = "pessimistic"
"""Plan §9: only the pessimistic lens may gate promotion."""


@dataclass(frozen=True)
class TierEvaluation:
    """Result of checking a metrics bundle against a tier's thresholds."""

    tier: str
    pass_: bool
    failure_reasons: tuple[str, ...]

    def reasons_json(self) -> Optional[str]:
        if self.pass_:
            return None
        return json.dumps(list(self.failure_reasons),
                          sort_keys=True, separators=(",", ":"))


def evaluate_tier(
    *,
    tier: str,
    metrics: Mapping,
    validation_policy_lock: Mapping,
) -> TierEvaluation:
    """Check ``metrics`` (a dict of validation outputs from a backtest)
    against the thresholds for ``tier`` in
    ``policy/validation_policy.lock``.

    ``metrics`` keys (any subset; missing → treated as failure unless
    flagged optional):

      oos_dsr, pbo, walk_forward_folds, oos_period_days,
      trades_per_regime, paper_obs_days, paper_trade_count_or_rebalances,
      max_drawdown_paper_pct, sharpe_tstat_net,
      excess_over_benchmark_annual_pct, paper_rebalance_events, lens

    The function does NOT load the lock from disk; the caller passes the
    pre-loaded ``policy.validation_policy`` mapping (a PolicyBundle attr).
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    tier_lock = validation_policy_lock["tiers"][tier]
    failures: list[str] = []

    def _floor(metric_name: str, lock_key: str, label: str) -> None:
        if lock_key not in tier_lock:
            return
        threshold = float(tier_lock[lock_key])
        if metric_name not in metrics:
            failures.append(f"{label}: metric missing")
            return
        if float(metrics[metric_name]) < threshold:
            failures.append(
                f"{label}: {float(metrics[metric_name]):.3f} < {threshold:.3f}"
            )

    def _ceiling(metric_name: str, lock_key: str, label: str) -> None:
        if lock_key not in tier_lock:
            return
        threshold = float(tier_lock[lock_key])
        if metric_name not in metrics:
            failures.append(f"{label}: metric missing")
            return
        if float(metrics[metric_name]) > threshold:
            failures.append(
                f"{label}: {float(metrics[metric_name]):.3f} > {threshold:.3f}"
            )

    _floor("oos_dsr", "min_oos_dsr", "DSR")
    _ceiling("pbo", "max_pbo", "PBO")
    _floor("walk_forward_folds", "min_walk_forward_folds", "walk-forward folds")
    _floor("oos_period_days", "min_oos_period_days", "OOS period days")
    _floor("trades_per_regime", "min_trades_per_regime",
           "trades per regime")
    _floor("paper_obs_days", "min_paper_obs_days", "paper observation days")
    _floor("paper_trade_count_or_rebalances",
           "min_paper_trade_count_or_rebalances",
           "paper trades / rebalances")
    _ceiling("max_drawdown_paper_pct", "max_drawdown_paper_pct",
             "max paper drawdown")
    _floor("sharpe_tstat_net", "min_sharpe_tstat_net",
           "t-stat of net Sharpe")
    _floor("excess_over_benchmark_annual_pct",
           "min_excess_over_benchmark_annual_pct",
           "annualised excess over benchmark")
    _floor("paper_rebalance_events", "min_paper_rebalance_events",
           "paper rebalance events")

    # Lens guard — promotion is gated only against the pessimistic lens.
    lens = metrics.get("lens")
    if lens and lens != GATE_LENS:
        failures.append(
            f"lens: artifact built from '{lens}', not '{GATE_LENS}'"
        )

    return TierEvaluation(
        tier=tier,
        pass_=not failures,
        failure_reasons=tuple(failures),
    )


def _canonical_metrics(metrics: Mapping) -> str:
    return json.dumps(dict(metrics), sort_keys=True,
                      separators=(",", ":"), default=str)


def compute_artifact_id(
    *, strategy_id: str, strategy_ver: int, tier: str,
    code_hash: str, config_hash: str, metrics: Mapping,
) -> str:
    """Deterministic artifact_id = sha256 of the immutable identifiers."""
    payload = json.dumps(
        {
            "strategy_id": strategy_id, "strategy_ver": strategy_ver,
            "tier": tier, "code_hash": code_hash, "config_hash": config_hash,
            "metrics": _canonical_metrics(metrics),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_validation_artifact(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    tier: str,
    code_hash: str,
    config_hash: str,
    metrics: Mapping,
    validation_policy_lock: Mapping,
    lens: str = GATE_LENS,
    now: Optional[dt.datetime] = None,
) -> tuple[str, TierEvaluation]:
    """Evaluate the metrics + insert one ``validation_artifact`` row.

    Returns ``(artifact_id, evaluation)``. ``evaluation.pass_`` reflects
    whether the tier thresholds were met; the row is written either way.
    """
    ensure_registry_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    artifact_id = compute_artifact_id(
        strategy_id=strategy_id, strategy_ver=strategy_ver, tier=tier,
        code_hash=code_hash, config_hash=config_hash, metrics=metrics,
    )
    metrics_with_lens = dict(metrics)
    metrics_with_lens.setdefault("lens", lens)
    evaluation = evaluate_tier(
        tier=tier, metrics=metrics_with_lens,
        validation_policy_lock=validation_policy_lock,
    )
    prev = last_hash(conn, "validation_artifact")
    row = {
        "artifact_id": artifact_id, "strategy_id": strategy_id,
        "strategy_ver": strategy_ver, "tier": tier,
        "produced_ts": now.isoformat(),
        "code_hash": code_hash, "config_hash": config_hash,
        "metrics_json": _canonical_metrics(metrics_with_lens),
        "lens": lens, "pass": 1 if evaluation.pass_ else 0,
        "failure_reasons": evaluation.reasons_json(),
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO validation_artifact (
            artifact_id, strategy_id, strategy_ver, tier, produced_ts,
            code_hash, config_hash, metrics_json, lens, pass,
            failure_reasons, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["artifact_id"], row["strategy_id"], row["strategy_ver"],
            row["tier"], row["produced_ts"], row["code_hash"],
            row["config_hash"], row["metrics_json"], row["lens"],
            row["pass"], row["failure_reasons"], prev, this_hash,
        ),
    )
    return artifact_id, evaluation


def find_latest_pass(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    tier: str,
) -> Optional[dict]:
    """Return the most-recent validation_artifact row with pass=1 for
    this strategy + tier, or None."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT artifact_id, strategy_ver, code_hash, config_hash,
                   metrics_json, lens, produced_ts
            FROM validation_artifact
            WHERE strategy_id = ? AND tier = ? AND pass = 1
            ORDER BY produced_ts DESC
            LIMIT 1
            """,
            (strategy_id, tier),
        )
    except sqlite3.OperationalError:
        return None
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "artifact_id": row[0], "strategy_ver": row[1],
        "code_hash": row[2], "config_hash": row[3],
        "metrics": json.loads(row[4]), "lens": row[5],
        "produced_ts": row[6],
    }


__all__ = [
    "GATE_LENS",
    "TIERS",
    "TIER_LIVE",
    "TIER_PAPER",
    "TIER_RESEARCH",
    "TierEvaluation",
    "compute_artifact_id",
    "evaluate_tier",
    "find_latest_pass",
    "record_validation_artifact",
]
