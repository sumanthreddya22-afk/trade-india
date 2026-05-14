#!/usr/bin/env python
"""Run the Tier-1 validation harness for a registered strategy.

Usage:
  python tools/run_tier1.py                        # ETF_MOMENTUM_v1 default
  python tools/run_tier1.py --start 2016-01-01     # custom window
  python tools/run_tier1.py --strategy ETF_MOMENTUM_v1

Requires ``data/historical_bars.db`` to be populated first via
``tools/load_historical_bars.py``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from importlib import import_module
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


STRATEGY_MODULES = {
    "ETF_MOMENTUM_v1": "trading_bot.strategies.etf_momentum_v1",
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", default="ETF_MOMENTUM_v1")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (default: 5y ago)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--starting-equity", type=float, default=100_000.0)
    p.add_argument("--historical-db", default="data/historical_bars.db")
    args = p.parse_args(argv)

    _load_env()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    start = (
        dt.date.fromisoformat(args.start) if args.start
        else end - dt.timedelta(days=5 * 365)
    )

    mod_path = STRATEGY_MODULES.get(args.strategy)
    if not mod_path:
        print(f"unknown strategy: {args.strategy}", file=sys.stderr)
        return 1
    sig_mod = import_module(mod_path)

    # Load lock files
    import json as _json
    cost_lock = _json.loads(Path("policy/cost_model.lock").read_text())
    val_lock = _json.loads(Path("policy/validation_policy.lock").read_text())

    from trading_bot.research.tier1 import run_tier1
    result = run_tier1(
        strategy_id=args.strategy,
        strategy_ver=1,
        signal_module=sig_mod,
        historical_db=Path(args.historical_db),
        cost_model_lock=cost_lock,
        validation_policy_lock=val_lock,
        start=start,
        end=end,
        starting_equity=args.starting_equity,
    )

    print()
    print("=" * 72)
    print(f"  Tier-1 validation: {args.strategy}")
    print("=" * 72)
    print(f"  window:           {start} → {end}")
    print(f"  artifact_id:      {result.artifact_id}")
    print(f"  passed:           {result.passed}")
    if not result.passed:
        print("  failure_reasons:")
        for r in result.failure_reasons:
            print(f"    - {r}")
    print()
    print(f"  Walk-forward folds:    {result.n_walk_forward_folds}")
    print(f"  OOS period (days):     {result.oos_period_days}")
    print(f"  Variants tested:       {result.n_variants}")
    print(f"  Trades (default cfg):  {result.n_trades}")
    print(f"  Observed Sharpe:       {result.observed_sharpe:.3f}")
    print(f"  Deflated SR prob:      {result.dsr_probability:.3f} "
          f"(need ≥ 0.50)")
    print(f"  PBO:                   {result.pbo:.3f} (need ≤ 0.50)")
    print()
    print("  Three-lens summary (chosen variant):")
    for label, r in (
        ("raw", result.raw_result),
        ("broker_paper", result.broker_paper_result),
        ("pessimistic", result.pessimistic_result),
    ):
        total_ret = (r.final_equity / r.starting_equity - 1.0) * 100.0
        print(f"    {label:<14} ${r.starting_equity:,.0f} → ${r.final_equity:,.0f}  "
              f"({total_ret:+.2f}%)  Sharpe={r.sharpe_annualised:.2f}  "
              f"MaxDD={r.max_drawdown_pct:.1f}%  trades={r.n_trades}  "
              f"fees=${r.total_fees:,.2f}")
    print()
    print(f"  Metrics JSON: {json.dumps(result.metrics_dict, indent=2, default=str)}")
    return 0 if result.passed else 2


if __name__ == "__main__":
    sys.exit(main())
