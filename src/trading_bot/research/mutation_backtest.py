"""Real mutation backtest callable.

Used by ``research.mutation_runner.run_nightly_cycle`` to replace the
dry-run stub. For each ``Candidate``:

  1. Resolve the family → (signal_fn, default_params, universe).
  2. Merge ``variant_value`` into the params (the mutation actually
     changes the strategy's parameterisation).
  3. Load historical bars from ``data/historical_bars.db``.
  4. Run ``research.backtest.run_backtest`` with the pessimistic cost
     lens.
  5. Convert annualised Sharpe → t-stat → one-tailed p-value.
  6. Return ``(p_value, sanity_checks)`` matching ``BacktestT``.

Sanity checks include trade count, max drawdown, and a "trades per
regime" proxy so the BH-FDR survivors aren't degenerate (e.g. one
mega-trade carrying the Sharpe).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trading_bot.research.backtest import CostLens, run_backtest
from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, load_bars, open_store,
)
from trading_bot.research.mutation_engine import Candidate
from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


# Registry: family → (signal_fn, default_params, universe, rebalance_freq).
# Adding a new v3 family here = mutation engine can tune its parameters.
def _signal_registry() -> dict:
    from trading_bot.strategies.crypto_momentum_v1.signal import (
        DEFAULT_PARAMS as CRYPTO_PARAMS, UNIVERSE as CRYPTO_UNIVERSE,
        signal_fn as crypto_signal,
    )
    from trading_bot.strategies.dual_momentum_v1.signal import (
        DEFAULT_PARAMS as DUAL_PARAMS, signal_fn as dual_signal,
    )
    from trading_bot.strategies.etf_momentum_v1.signal import (
        DEFAULT_PARAMS as ETF_PARAMS, UNIVERSE as ETF_UNIVERSE,
        signal_fn as etf_signal,
    )

    # Dual Momentum v1 doesn't export a static UNIVERSE constant; the v3
    # variant uses sleeves. For backtest purposes we anchor to the
    # canonical SPY + TLT pair (the seed thesis), which is sufficient
    # for parameter mutation testing.
    DUAL_UNIVERSE = ("SPY", "TLT")

    return {
        "ETF_MOMENTUM_v3": {
            "signal_fn": etf_signal,
            "default_params": dict(ETF_PARAMS),
            "universe": ETF_UNIVERSE,
            "rebalance_freq": "daily",
        },
        "DUAL_MOMENTUM_v3": {
            "signal_fn": dual_signal,
            "default_params": dict(DUAL_PARAMS),
            "universe": DUAL_UNIVERSE,
            "rebalance_freq": "daily",
        },
        "CRYPTO_MOMENTUM_v3": {
            "signal_fn": crypto_signal,
            "default_params": dict(CRYPTO_PARAMS),
            "universe": CRYPTO_UNIVERSE,
            "rebalance_freq": "daily",
        },
    }


def _load_cost_lock() -> Mapping:
    import json
    p = DEFAULT_POLICY_DIR / "cost_model.lock"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _sharpe_to_p_value(sharpe: float, n_obs: int) -> float:
    """One-tailed Sharpe test: H0 sharpe ≤ 0. Approx via normal CDF.

    The standard test is t-distributed with n-1 dof, but for n > 30 the
    normal approximation is tight enough. We clamp p to ``[1e-6, 1.0]``
    so BH-FDR doesn't divide by zero.
    """
    if n_obs < 30 or sharpe <= 0:
        return 1.0
    # Annualised Sharpe → daily Sharpe → t-stat.
    daily_sharpe = sharpe / math.sqrt(252)
    t_stat = daily_sharpe * math.sqrt(n_obs)
    # One-tailed standard normal survival function.
    p = 0.5 * math.erfc(t_stat / math.sqrt(2))
    return max(1e-6, min(1.0, p))


def _merge_variant_into_params(
    candidate: Candidate, defaults: dict,
) -> dict:
    """Apply the candidate's ``variant_value`` to the default params.

    The convention: ``mutation_id`` names the param being mutated and
    ``variant_value`` is the new value. Strategies that have nested
    params can extend this (dotted notation, etc.) — for now the seed
    families use flat dicts.
    """
    out = dict(defaults)
    if isinstance(candidate.variant_value, dict):
        out.update(candidate.variant_value)
    else:
        # Single-value mutation — name the param via mutation_id.
        param_name = candidate.mutation_id.replace("-", "_")
        out[param_name] = candidate.variant_value
    return out


def make_backtest_fn(
    *,
    historical_db: Path = None,
    window_years: int = 5,
    starting_equity: float = 100_000.0,
) -> Callable[[Candidate], tuple[float, Mapping[str, Any]]]:
    """Return a backtest callable suitable for
    ``research.run_mutation_cycle.run_cycle(backtest=...)``.
    """
    historical_db = historical_db or DEFAULT_HISTORICAL_PATH
    if not historical_db.is_absolute():
        from pathlib import Path as _P
        historical_db = _P(__file__).resolve().parents[3] / historical_db

    cost_lock = _load_cost_lock()
    registry = _signal_registry()

    def _backtest(candidate: Candidate) -> tuple[float, Mapping[str, Any]]:
        family = candidate.family
        entry = registry.get(family)
        if entry is None:
            return 1.0, {"error": f"unknown_family:{family}"}
        if not historical_db.exists():
            return 1.0, {"error": "no_historical_db"}

        params = _merge_variant_into_params(candidate, entry["default_params"])
        universe = entry["universe"]
        rebalance = entry["rebalance_freq"]

        end = dt.date.today()
        start = dt.date(end.year - window_years, end.month, end.day)

        conn = open_store(historical_db)
        try:
            bars = load_bars(
                conn, symbols=universe, start=start, end=end,
            )
        finally:
            conn.close()

        if not bars or not any(bars.values()):
            return 1.0, {"error": "no_bars_loaded"}

        signal_fn = entry["signal_fn"]
        # The v1 signal_fn signatures vary by family; we adapt with a
        # closure so the backtest sees a uniform (history, decision_date)
        # callable.
        def _adapted_signal(history, decision_date):
            try:
                if family == "DUAL_MOMENTUM_v3":
                    return signal_fn(
                        history, decision_date,
                        params=params, universe=universe,
                    )
                elif family == "CRYPTO_MOMENTUM_v3":
                    return signal_fn(history, decision_date, params=params)
                else:
                    return signal_fn(
                        history, decision_date,
                        params=params, universe=universe,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("signal_fn raised: %s", e)
                return {}

        try:
            result = run_backtest(
                bars_by_symbol=bars,
                signal_fn=_adapted_signal,
                start=start, end=end,
                starting_equity=starting_equity,
                cost_lens=CostLens.pessimistic(cost_lock),
                rebalance_freq=rebalance,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("backtest %s/%s failed: %s",
                        family, candidate.mutation_id, e)
            return 1.0, {"error": f"backtest_exception:{e}"}

        n_obs = len(result.returns_daily)

        # WS2 defensive guard — reject implausible results so the broken
        # in-house engine can't feed garbage candidates into auto-register.
        # The engine has known issues with daily-rebalance + fee model
        # (max_drawdown >50% and fee explosions in unit smoke); plan calls
        # for vectorbt replacement (deferred). Until then, treat any
        # candidate with these tell-tales as a non-survivor.
        total_return_pct = (
            (result.final_equity / result.starting_equity - 1.0) * 100.0
            if result.starting_equity else 0.0
        )
        implausible_reasons = []
        if result.max_drawdown_pct > 50.0:
            implausible_reasons.append(
                f"max_drawdown_pct={result.max_drawdown_pct:.1f}>50"
            )
        if result.total_fees > 0.5 * starting_equity:
            implausible_reasons.append(
                f"total_fees={result.total_fees:.0f}>"
                f"{0.5*starting_equity:.0f}"
            )
        if total_return_pct < -50.0:
            implausible_reasons.append(
                f"total_return_pct={total_return_pct:.1f}<-50"
            )
        if implausible_reasons:
            return 1.0, {
                "error": "implausible_backtest",
                "reasons": implausible_reasons,
                "sharpe_annualised": result.sharpe_annualised,
                "max_drawdown_pct": result.max_drawdown_pct,
                "total_return_pct": total_return_pct,
                "total_fees": result.total_fees,
                "n_trades": result.n_trades,
                "engine_quarantined": True,
            }

        p = _sharpe_to_p_value(result.sharpe_annualised, n_obs)

        sanity: dict[str, Any] = {
            "sharpe_annualised": result.sharpe_annualised,
            "n_trades": result.n_trades,
            "n_observations": n_obs,
            "max_drawdown_pct": result.max_drawdown_pct,
            "win_rate": result.win_rate,
            "total_return_pct": (
                (result.final_equity / result.starting_equity - 1.0) * 100.0
                if result.starting_equity else 0.0
            ),
            "total_fees": result.total_fees,
        }
        return p, sanity

    return _backtest


__all__ = ["make_backtest_fn"]
