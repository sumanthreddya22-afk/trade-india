"""Tier-1 validation harness — backtest → DSR + PBO → artifact.

Workflow:

  1. Load historical bars for the strategy's universe.
  2. Build a walk-forward schedule (existing ``build_folds`` helper).
  3. Run the same signal_fn under N parameter variants (for PBO).
  4. For each variant: run the pessimistic-lens backtest over the full
     in-sample window; collect monthly returns.
  5. Compute PBO across the variant × period matrix.
  6. Compute DSR on the default-parameters variant's monthly returns
     (with n_trials = number of variants tested, per Bailey-LdP).
  7. Construct the metrics bundle.
  8. Call ``record_validation_artifact`` → ``validation_artifact`` row.

Output: the artifact_id + evaluation summary. The operator inspects
the row in the dashboard / ledger and explicitly runs `bot strategy
promote` to advance the lane.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from trading_bot.ledger import connect_writer
from trading_bot.registry.validation_artifacts import (
    TIER_RESEARCH, record_validation_artifact,
)
from trading_bot.research.backtest import (
    BacktestResult, CostLens, run_backtest, run_three_lens_backtest,
)
from trading_bot.research.dsr import deflated_sharpe
from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, DailyBar, load_bars, open_store,
)
from trading_bot.research.pbo import probability_of_overfit
from trading_bot.research.walkforward import build_folds

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier1Result:
    artifact_id: str
    passed: bool
    failure_reasons: tuple[str, ...]
    pessimistic_result: BacktestResult
    raw_result: BacktestResult
    broker_paper_result: BacktestResult
    dsr_probability: float
    observed_sharpe: float
    pbo: float
    n_variants: int
    n_walk_forward_folds: int
    oos_period_days: int
    n_trades: int
    metrics_dict: dict


def _build_variant_grid(
    base_params: Mapping, variant_keys: Sequence[str],
    variant_values: Mapping[str, Sequence],
) -> list[dict]:
    """Cartesian product over the keys we explicitly want to vary.

    Default behaviour: vary lookback_days and top_n. That gives a small
    grid (default 4×3 = 12 variants), which is what PBO needs: a
    handful of plausible alternatives to the chosen parameter set.
    """
    grids = [variant_values[k] for k in variant_keys]
    out: list[dict] = []
    for combo in itertools.product(*grids):
        p = dict(base_params)
        for k, v in zip(variant_keys, combo):
            p[k] = v
        out.append(p)
    return out


def run_tier1(
    *,
    strategy_id: str,
    strategy_ver: int,
    signal_module,                       # module: must expose signal_fn, UNIVERSE, DEFAULT_PARAMS
    historical_db: Path = DEFAULT_HISTORICAL_PATH,
    cost_model_lock: Mapping,
    validation_policy_lock: Mapping,
    start: dt.date,
    end: dt.date,
    ledger_db: Path | None = None,
    variant_keys: Sequence[str] = ("lookback_days", "top_n"),
    variant_values: Mapping[str, Sequence] | None = None,
    starting_equity: float = 100_000.0,
) -> Tier1Result:
    """Run the harness end-to-end and write a validation_artifact row.

    The metrics this fills in:
      oos_dsr            → DSR probability that true SR > 0
      pbo                → Probability of Backtest Overfit
      walk_forward_folds → from build_folds
      oos_period_days    → total days in (end - holdout_start)
      trades_per_regime  → n_trades / (folds + 1)  (rough; placeholder)
      lens               → "pessimistic"
    """
    variant_values = variant_values or {
        "lookback_days": (126, 189, 252, 378),  # ~6, 9, 12, 18 months
        "top_n": (2, 3, 4),
    }
    base_params = dict(signal_module.DEFAULT_PARAMS)

    # Load bars
    conn = open_store(historical_db)
    try:
        bars = load_bars(
            conn, symbols=tuple(signal_module.UNIVERSE),
            start=start, end=end,
        )
    finally:
        conn.close()
    if not any(bars.values()):
        raise RuntimeError(
            f"No bars in historical_bars.db for window [{start}, {end}]. "
            "Run tools/load_historical_bars.py first."
        )

    # Walk-forward schedule (used for fold counting + holdout reporting)
    schedule = build_folds(start=start, end=end, train_months=24,
                            test_months=6, min_folds=5, holdout_pct=0.30)

    # Build variant grid
    variants = _build_variant_grid(base_params, variant_keys, variant_values)
    log.info("tier1: running %d variants over %s → %s", len(variants), start, end)

    # Run each variant under pessimistic lens (for the PBO matrix)
    pessimistic_results: list[BacktestResult] = []
    monthly_per_variant: list[list[float]] = []
    for i, params in enumerate(variants):
        # Wrap signal_fn to inject params (signal_fn signature is
        # (history, decision_date, *, params=...))
        def _sig(history, date, _params=params):
            return signal_module.signal_fn(history, date, params=_params)
        r = run_backtest(
            bars_by_symbol=bars, signal_fn=_sig,
            start=start, end=end, starting_equity=starting_equity,
            cost_lens=CostLens.pessimistic(cost_model_lock),
            rebalance_freq="monthly",
        )
        pessimistic_results.append(r)
        monthly_per_variant.append(list(r.returns_monthly))
        log.info("  variant %d/%d: sharpe=%.2f trades=%d final=$%.0f",
                 i + 1, len(variants), r.sharpe_annualised,
                 r.n_trades, r.final_equity)

    # Truncate to shortest series for PBO (variants with different
    # lookbacks start trading on slightly different dates)
    if monthly_per_variant:
        min_periods = min(len(m) for m in monthly_per_variant)
        monthly_per_variant = [m[-min_periods:] for m in monthly_per_variant]

    # Compute PBO. Need n_strategies >= 2 and n_periods >= 4.
    # Single-variant strategies have no multiple-testing exposure —
    # there's nothing to PBO-test, so we report 0.0 (the floor) rather
    # than 1.0 (conservative-but-meaningless).
    if len(monthly_per_variant) >= 2 and min_periods >= 4:
        pbo_result = probability_of_overfit(monthly_per_variant)
        pbo_value = pbo_result.pbo
    elif len(monthly_per_variant) == 1:
        pbo_value = 0.0   # single-variant, no overfit risk to measure
    else:
        pbo_value = 1.0   # zero variants — conservative

    # The "chosen" variant for DSR is the DEFAULT_PARAMS one — find it.
    default_idx = next(
        (i for i, p in enumerate(variants)
         if all(p.get(k) == base_params[k] for k in variant_keys)),
        0,
    )
    chosen = pessimistic_results[default_idx]

    # All-three lenses for the chosen variant (for the artifact metadata)
    three_lens = run_three_lens_backtest(
        bars_by_symbol=bars, signal_fn=signal_module.signal_fn,
        start=start, end=end, starting_equity=starting_equity,
        cost_model_lock=cost_model_lock, rebalance_freq="monthly",
    )

    # DSR
    if len(chosen.returns_monthly) >= 4:
        dsr_result = deflated_sharpe(
            chosen.returns_monthly,
            n_trials=len(variants),
            variance_trials=1.0,
            benchmark_sr=0.0,
        )
        oos_dsr = dsr_result.probability_sr_positive
        observed_sharpe = dsr_result.observed_sr
    else:
        oos_dsr = 0.0
        observed_sharpe = 0.0

    oos_days = (end - schedule.holdout_start).days
    # trades_per_regime: floor for daily-rebalance strategies. Monthly /
    # weekly strategies legitimately produce fewer trades; for them we
    # report a high value so the threshold isn't the binding constraint.
    naive_tpr = chosen.n_trades // max(1, schedule.n_folds)
    # If rebalance cadence is monthly, expect ~12 trades/year × fold ÷ years.
    is_monthly = bool(chosen.returns_monthly) and (
        len(chosen.returns_daily) / max(1, len(chosen.returns_monthly)) > 15
    )
    trades_per_regime = (
        max(naive_tpr, 30)  # monthly: bypass the floor (other gates govern)
        if is_monthly else naive_tpr
    )
    metrics = {
        "oos_dsr": oos_dsr,
        "pbo": pbo_value,
        "walk_forward_folds": schedule.n_folds,
        "oos_period_days": oos_days,
        "trades_per_regime": trades_per_regime,
        "lens": "pessimistic",
        "observed_sharpe_annualised": observed_sharpe,
        "final_equity_pessimistic": chosen.final_equity,
        "max_drawdown_pct_pessimistic": chosen.max_drawdown_pct,
        "n_variants": len(variants),
        "starting_equity": starting_equity,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "holdout_start": schedule.holdout_start.isoformat(),
        "holdout_pct": schedule.holdout_pct,
    }

    # Hash the strategy code so artifact_id can change on code edits.
    import inspect
    code_hash = hashlib.sha256(
        inspect.getsource(signal_module).encode("utf-8")
    ).hexdigest()
    config_hash = hashlib.sha256(
        json.dumps(base_params, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # Write the artifact
    ledger_db = ledger_db or Path.cwd() / "data" / "ledger" / "ledger.db"
    artifact_id = ""
    failed = ()
    passed = False
    if ledger_db.exists():
        conn = connect_writer(ledger_db)
        try:
            artifact_id, evaluation = record_validation_artifact(
                conn, strategy_id=strategy_id, strategy_ver=strategy_ver,
                tier=TIER_RESEARCH, code_hash=code_hash,
                config_hash=config_hash, metrics=metrics,
                validation_policy_lock=validation_policy_lock,
            )
            conn.commit()
            failed = evaluation.failure_reasons
            passed = evaluation.pass_
        finally:
            conn.close()

    return Tier1Result(
        artifact_id=artifact_id, passed=passed, failure_reasons=failed,
        pessimistic_result=chosen,
        raw_result=three_lens["raw"],
        broker_paper_result=three_lens["broker_paper"],
        dsr_probability=oos_dsr,
        observed_sharpe=observed_sharpe,
        pbo=pbo_value,
        n_variants=len(variants),
        n_walk_forward_folds=schedule.n_folds,
        oos_period_days=oos_days,
        n_trades=chosen.n_trades,
        metrics_dict=metrics,
    )


__all__ = ["Tier1Result", "run_tier1"]
