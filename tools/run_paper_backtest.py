#!/usr/bin/env python
"""Run paper backtests on the Indian universe and print results.

Usage:
  python tools/run_paper_backtest.py
  python tools/run_paper_backtest.py --strategy etf_momentum
  python tools/run_paper_backtest.py --strategy dual_momentum
  python tools/run_paper_backtest.py --years 2
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _load_cost_model_lock() -> dict:
    """Load the cost model policy lock for three-lens backtest."""
    lock_path = Path("policy/cost_model.lock")
    if lock_path.exists():
        import json
        return json.loads(lock_path.read_text())
    # Fallback: India cost model defaults
    return {
        "stocks": {
            "stt_delivery_sell_pct": 0.1,
            "stt_intraday_sell_pct": 0.025,
            "gst_on_brokerage_pct": 18.0,
            "stamp_duty_buy_pct": 0.015,
            "nse_txn_charge_pct": 0.00297,
            "sebi_turnover_fee_pct": 0.0001,
            "zerodha_delivery_flat_inr": 0,
            "zerodha_intraday_flat_inr": 20,
            "extra_slippage_bps": 10,
        }
    }


def _run_strategy(
    *,
    name: str,
    signal_fn,
    universe: tuple[str, ...],
    start: dt.date,
    end: dt.date,
    equity: float,
    rebalance_freq: str = "monthly",
):
    """Generic backtest runner — wraps signal_fn with the correct universe."""
    from functools import partial
    from trading_bot.research.historical_bars import open_store, load_bars
    from trading_bot.research.backtest import run_three_lens_backtest

    conn = open_store()
    bars = load_bars(conn, symbols=universe, start=start, end=end)
    conn.close()

    # Check we have data
    empty = [s for s, b in bars.items() if len(b) < 20]
    if empty:
        print(f"  WARNING: insufficient data for: {empty}")
    bars = {s: b for s, b in bars.items() if len(b) >= 20}
    if not bars:
        print("  ERROR: no data — run tools/load_historical_bars.py first")
        return None

    print(f"  Universe: {list(bars.keys())}")
    print(f"  Date range: {start} -> {end}")
    print(f"  Starting equity: INR {equity:,.0f}")

    # Wrap signal_fn to pass our universe (overriding the default US one)
    def india_signal(history, decision_date):
        return signal_fn(history, decision_date, universe=universe)

    results = run_three_lens_backtest(
        bars_by_symbol=bars,
        signal_fn=india_signal,
        start=start,
        end=end,
        starting_equity=equity,
        cost_model_lock=_load_cost_model_lock(),
        rebalance_freq=rebalance_freq,
    )
    return results


def run_etf_momentum_backtest(start: dt.date, end: dt.date, equity: float):
    """Backtest ETF momentum on NSE ETFs."""
    from trading_bot.strategies.etf_momentum_v1.signal import signal_fn
    return _run_strategy(
        name="ETF Momentum",
        signal_fn=signal_fn,
        universe=("NIFTYBEES", "JUNIORBEES", "BANKBEES", "GOLDBEES",
                  "LIQUIDBEES", "SETFNIF50"),
        start=start, end=end, equity=equity,
    )


def run_dual_momentum_backtest(start: dt.date, end: dt.date, equity: float):
    """Backtest dual momentum (NIFTYBEES vs LIQUIDBEES)."""
    from trading_bot.strategies.dual_momentum_v1.signal import signal_fn
    return _run_strategy(
        name="Dual Momentum",
        signal_fn=signal_fn,
        universe=("NIFTYBEES", "LIQUIDBEES"),
        start=start, end=end, equity=equity,
    )


def run_crypto_momentum_backtest(start: dt.date, end: dt.date, equity: float):
    """Backtest crypto momentum (BTC/INR + ETH/INR)."""
    from trading_bot.strategies.crypto_momentum_v1.signal import signal_fn
    return _run_strategy(
        name="Crypto Momentum",
        signal_fn=signal_fn,
        universe=("BTC/INR", "ETH/INR"),
        start=start, end=end, equity=equity,
    )


def print_results(name: str, results: dict):
    """Pretty-print three-lens backtest results (dict keyed by lens name)."""
    print(f"\n{'=' * 65}")
    print(f"  {name}")
    print(f"{'=' * 65}")
    print(f"  {'Lens':<14} {'Return':>10} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>7} {'Fees':>12} {'WinRate':>8}")
    print(f"  {'-' * 63}")
    for lens_name, r in results.items():
        d = r.to_dict()
        ret = d['total_return_pct']
        print(
            f"  {d['lens']:<14} {ret:>+9.1f}% {d['sharpe_annualised']:>8.2f} "
            f"{d['max_drawdown_pct']:>7.1f}% {d['n_trades']:>7} "
            f"INR {d['total_fees']:>8,.0f} {d['win_rate']:>7.1%}"
        )
    print(f"{'=' * 65}")

    # Gate check
    pessimistic = results.get("pessimistic")
    if pessimistic:
        p = pessimistic
        d = p.to_dict()
        print(f"\n  Promotion gate (pessimistic lens):")
        sharpe_ok = d['sharpe_annualised'] > 0.5
        dd_ok = d['max_drawdown_pct'] < 25.0
        ret_ok = d['total_return_pct'] > 0
        print(f"    Sharpe > 0.5:      {'PASS' if sharpe_ok else 'FAIL'} ({d['sharpe_annualised']:.2f})")
        print(f"    Max DD < 25%:      {'PASS' if dd_ok else 'FAIL'} ({d['max_drawdown_pct']:.1f}%)")
        print(f"    Positive return:   {'PASS' if ret_ok else 'FAIL'} ({d['total_return_pct']:+.1f}%)")
        if sharpe_ok and dd_ok and ret_ok:
            print(f"    >>> Would PASS Tier-1 gate <<<")
        else:
            print(f"    >>> Would FAIL Tier-1 gate <<<")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", default="all",
                   choices=["all", "etf_momentum", "dual_momentum", "crypto_momentum"],
                   help="Which strategy to backtest (default: all)")
    p.add_argument("--years", type=int, default=2, help="Years of backtest (default: 2)")
    p.add_argument("--equity", type=float, default=1_000_000,
                   help="Starting equity in INR (default: 10,00,000)")
    args = p.parse_args(argv)

    _load_env()

    end = dt.date.today()
    start = end - dt.timedelta(days=365 * args.years)

    print(f"\n{'#' * 65}")
    print(f"  PAPER BACKTEST — Indian Market Strategies")
    print(f"  Period: {start} -> {end} ({args.years} years)")
    print(f"  Starting capital: INR {args.equity:,.0f}")
    print(f"{'#' * 65}")

    strategies = {
        "etf_momentum": ("ETF Momentum (NSE ETFs)", run_etf_momentum_backtest),
        "dual_momentum": ("Dual Momentum (NIFTYBEES / LIQUIDBEES)", run_dual_momentum_backtest),
        "crypto_momentum": ("Crypto Momentum (BTC/INR + ETH/INR)", run_crypto_momentum_backtest),
    }

    to_run = strategies if args.strategy == "all" else {args.strategy: strategies[args.strategy]}

    for key, (name, fn) in to_run.items():
        print(f"\n>>> Running: {name}")
        try:
            results = fn(start, end, args.equity)
            if results:
                print_results(name, results)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\nDone. Dashboard: http://localhost:8765")
    print(f"  Cockpit:   http://localhost:8765/cockpit")
    print(f"  API:       http://localhost:8765/api/status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
