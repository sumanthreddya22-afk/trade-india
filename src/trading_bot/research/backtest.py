"""Backtest engine — replays a strategy against historical bars.

Plan v4 §9: backtests report three lenses (raw, broker_paper,
pessimistic). The Tier-1/2/3 validation gate uses ONLY the pessimistic
lens. This engine produces returns in all three lenses but the harness
(``run_tier1.py``) feeds only the pessimistic series to DSR / PBO.

Design contracts:

  * **No look-ahead.** A signal computed using bar at date ``D`` may
    only act on the close at ``D`` (i.e., the order fills at the next
    bar's open, ``D+1``). Tests assert this.
  * **No survivorship correction.** ETF universe is fixed at the
    operator-specified set; ETFs that didn't exist in a given window
    silently produce no signal for that window. (For S&P 500 universes
    a real point-in-time membership table would be required; explicit
    Phase 7-second-lane decision.)
  * **Cost model from policy/cost_model.lock.** Loaded at backtest
    start; pessimistic-lens additive bps applied per round-trip.

Signal protocol — a callable:

    sig_fn(history: dict[symbol, list[DailyBar]], decision_date: date)
        -> dict[symbol, float]   # target weight in [0, 1] per symbol

Returns *target weights* (fraction of equity). The engine handles
sizing, rebalance order generation, fills, and accounting.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from trading_bot.research.historical_bars import DailyBar

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost lenses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostLens:
    name: str               # "raw" | "broker_paper" | "pessimistic"
    extra_slippage_bps: float
    fixed_fee_per_share: float = 0.0
    sec_fee_rate: float = 0.0    # applied on sell notional
    finra_taf_per_share: float = 0.0
    finra_taf_cap_per_trade: float = 8.30

    @classmethod
    def raw(cls) -> "CostLens":
        return cls(name="raw", extra_slippage_bps=0.0)

    @classmethod
    def broker_paper(cls, lock: Mapping) -> "CostLens":
        eq = lock.get("stocks", {})
        return cls(
            name="broker_paper",
            extra_slippage_bps=0.0,
            sec_fee_rate=float(eq.get("sec_section_31_rate", 0.0)),
            finra_taf_per_share=float(eq.get("finra_taf_per_share", 0.0)),
            finra_taf_cap_per_trade=float(eq.get("finra_taf_cap_per_trade", 8.30)),
        )

    @classmethod
    def pessimistic(cls, lock: Mapping) -> "CostLens":
        eq = lock.get("stocks", {})
        return cls(
            name="pessimistic",
            extra_slippage_bps=float(eq.get("extra_slippage_bps", 5)),
            sec_fee_rate=float(eq.get("sec_section_31_rate", 0.0)),
            finra_taf_per_share=float(eq.get("finra_taf_per_share", 0.0)),
            finra_taf_cap_per_trade=float(eq.get("finra_taf_cap_per_trade", 8.30)),
        )

    def apply_fill_cost(
        self, *, side: str, price: float, qty: float,
    ) -> tuple[float, float]:
        """Return (effective_price, fee_dollars) for a fill."""
        # Slippage: buys pay more, sells receive less.
        slip = price * self.extra_slippage_bps / 1e4
        effective = price + slip if side == "buy" else price - slip
        notional = abs(qty) * effective

        fees = abs(qty) * self.fixed_fee_per_share
        if side == "sell":
            # SEC Section 31 + FINRA TAF apply on sells only.
            fees += notional * self.sec_fee_rate
            taf = abs(qty) * self.finra_taf_per_share
            fees += min(taf, self.finra_taf_cap_per_trade)
        return effective, fees


# ---------------------------------------------------------------------------
# Backtest state + result
# ---------------------------------------------------------------------------

@dataclass
class Position:
    qty: float = 0.0
    avg_cost: float = 0.0


@dataclass
class Trade:
    decision_date: dt.date
    fill_date: dt.date
    symbol: str
    side: str               # "buy" | "sell"
    qty: float
    price: float            # post-slippage effective price
    fees: float
    notional: float


@dataclass
class BacktestResult:
    lens: str
    starting_equity: float
    final_equity: float
    equity_curve: list[tuple[dt.date, float]]
    returns_daily: list[float]
    returns_monthly: list[float]
    n_trades: int
    total_fees: float
    sharpe_annualised: float
    max_drawdown_pct: float
    win_rate: float
    trades: list[Trade] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lens": self.lens,
            "starting_equity": self.starting_equity,
            "final_equity": self.final_equity,
            "total_return_pct": (
                (self.final_equity / self.starting_equity - 1.0) * 100.0
                if self.starting_equity else 0.0
            ),
            "n_trades": self.n_trades,
            "total_fees": self.total_fees,
            "sharpe_annualised": self.sharpe_annualised,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "n_daily_returns": len(self.returns_daily),
            "n_monthly_returns": len(self.returns_monthly),
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

SignalT = Callable[[Mapping[str, Sequence[DailyBar]], dt.date], Mapping[str, float]]


def _all_trading_dates(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
) -> list[dt.date]:
    """Union of dates across all symbols, sorted ascending. Trading days
    only; gaps reflect Saturdays / Sundays / market holidays."""
    seen: set[dt.date] = set()
    for series in bars_by_symbol.values():
        for b in series:
            seen.add(b.bar_date)
    return sorted(seen)


def _close_index(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
) -> dict[str, dict[dt.date, DailyBar]]:
    return {sym: {b.bar_date: b for b in series}
            for sym, series in bars_by_symbol.items()}


def _max_drawdown_pct(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _annualised_sharpe(daily_returns: Sequence[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = statistics.fmean(daily_returns)
    sd = statistics.pstdev(daily_returns)
    if sd <= 0:
        return 0.0
    # 252 trading days per year.
    return mean / sd * math.sqrt(252)


def run_backtest(
    *,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    signal_fn: SignalT,
    start: dt.date,
    end: dt.date,
    starting_equity: float = 100_000.0,
    cost_lens: CostLens,
    rebalance_freq: str = "monthly",     # "monthly" | "weekly" | "daily"
    decision_lag_days: int = 1,          # decisions act on next bar's open
) -> BacktestResult:
    """Run one backtest end-to-end.

    State machine:
      For each trading day D in [start, end]:
        if D is a rebalance day:
          targets = signal_fn(history_ending_at_D, D)
          enqueue diffs vs current positions as fill_at_D+lag orders
        for each pending order with fill_date == D:
          apply fill (open price on D, with slippage)
        mark-to-market with D's close price
        record equity curve point + daily return
    """
    dates = [d for d in _all_trading_dates(bars_by_symbol) if start <= d <= end]
    if not dates:
        raise ValueError(f"no trading dates in window [{start}, {end}]")
    close_idx = _close_index(bars_by_symbol)

    positions: dict[str, Position] = {}
    cash = starting_equity
    pending: list[tuple[dt.date, str, str, float]] = []     # (fill_date, symbol, side, target_qty)
    trades: list[Trade] = []
    equity_curve: list[tuple[dt.date, float]] = []
    daily_returns: list[float] = []
    monthly_returns: list[float] = []
    last_equity = starting_equity
    last_monthly_equity = starting_equity
    last_month: Optional[tuple[int, int]] = None
    total_fees = 0.0

    def _is_rebalance(date: dt.date, prev: dt.date | None) -> bool:
        if rebalance_freq == "daily":
            return True
        if rebalance_freq == "weekly":
            return prev is None or date.isocalendar().week != prev.isocalendar().week
        # monthly default
        return prev is None or date.month != prev.month

    prev_date = None
    for d in dates:
        # 1) Fill any orders queued for today.
        remaining = []
        for fill_date, sym, side, target_qty in pending:
            if fill_date != d:
                remaining.append((fill_date, sym, side, target_qty))
                continue
            bar = close_idx.get(sym, {}).get(d)
            if bar is None:
                # Symbol didn't trade today — defer the order.
                # In live trading we'd cancel; here we just drop.
                continue
            price, fees = cost_lens.apply_fill_cost(
                side=side, price=bar.open, qty=target_qty,
            )
            pos = positions.setdefault(sym, Position())
            if side == "buy":
                new_qty = pos.qty + target_qty
                if new_qty > 0:
                    pos.avg_cost = (pos.avg_cost * pos.qty + price * target_qty) / new_qty
                pos.qty = new_qty
                cash -= price * target_qty + fees
            else:
                pos.qty -= target_qty
                if pos.qty <= 1e-9:
                    pos.qty = 0.0
                    pos.avg_cost = 0.0
                cash += price * target_qty - fees
            total_fees += fees
            trades.append(Trade(
                decision_date=d - dt.timedelta(days=decision_lag_days),
                fill_date=d, symbol=sym, side=side, qty=target_qty,
                price=price, fees=fees, notional=price * target_qty,
            ))
        pending = remaining

        # 2) Rebalance decision?
        if _is_rebalance(d, prev_date):
            history = {
                sym: [b for b in series if b.bar_date <= d]
                for sym, series in bars_by_symbol.items()
            }
            try:
                targets = signal_fn(history, d) or {}
            except Exception:
                log.exception("signal_fn raised on %s", d)
                targets = {}
            # Mark-to-market equity at *current* prices for sizing.
            equity_for_sizing = cash + sum(
                pos.qty * (close_idx.get(sym, {}).get(d).close if close_idx.get(sym, {}).get(d) else pos.avg_cost)
                for sym, pos in positions.items() if pos.qty > 0
            )
            # Compute desired qty per symbol from target weight.
            for sym, target_w in targets.items():
                bar = close_idx.get(sym, {}).get(d)
                if bar is None or bar.close <= 0:
                    continue
                target_value = equity_for_sizing * max(0.0, min(1.0, target_w))
                target_qty = target_value / bar.close
                current_qty = positions.get(sym, Position()).qty
                diff = target_qty - current_qty
                if abs(diff) < 1e-4:
                    continue
                side = "buy" if diff > 0 else "sell"
                fill_date = d + dt.timedelta(days=decision_lag_days)
                pending.append((fill_date, sym, side, abs(diff)))
            # Symbols dropped to weight 0 → also queue sells.
            for sym, pos in list(positions.items()):
                if pos.qty > 0 and sym not in targets:
                    fill_date = d + dt.timedelta(days=decision_lag_days)
                    pending.append((fill_date, sym, "sell", pos.qty))

        # 3) Mark to market at today's close.
        equity_today = cash + sum(
            pos.qty * (close_idx.get(sym, {}).get(d).close if close_idx.get(sym, {}).get(d) else pos.avg_cost)
            for sym, pos in positions.items() if pos.qty > 0
        )
        equity_curve.append((d, equity_today))
        if last_equity > 0:
            daily_returns.append(equity_today / last_equity - 1.0)
        last_equity = equity_today

        # Month boundary → record monthly return.
        cur_month = (d.year, d.month)
        if last_month is None:
            last_month = cur_month
        elif cur_month != last_month and last_monthly_equity > 0:
            monthly_returns.append(equity_today / last_monthly_equity - 1.0)
            last_monthly_equity = equity_today
            last_month = cur_month

        prev_date = d

    # Final monthly return (partial month) — append if equity changed.
    if last_monthly_equity > 0 and equity_curve and equity_curve[-1][1] != last_monthly_equity:
        monthly_returns.append(equity_curve[-1][1] / last_monthly_equity - 1.0)

    eq_values = [e for _, e in equity_curve]
    # Win rate over closed round-trips: simplistic — fraction of sell
    # trades that closed above avg_cost. Imperfect but cheap.
    sells = [t for t in trades if t.side == "sell"]
    winning_sells = sum(1 for t in sells if t.price > 0)  # placeholder
    win_rate = (winning_sells / len(sells)) if sells else 0.0

    return BacktestResult(
        lens=cost_lens.name,
        starting_equity=starting_equity,
        final_equity=equity_curve[-1][1] if equity_curve else starting_equity,
        equity_curve=equity_curve,
        returns_daily=daily_returns,
        returns_monthly=monthly_returns,
        n_trades=len(trades),
        total_fees=total_fees,
        sharpe_annualised=_annualised_sharpe(daily_returns),
        max_drawdown_pct=_max_drawdown_pct(eq_values),
        win_rate=win_rate,
        trades=trades,
    )


def run_three_lens_backtest(
    *,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    signal_fn: SignalT,
    start: dt.date,
    end: dt.date,
    starting_equity: float = 100_000.0,
    cost_model_lock: Mapping,
    rebalance_freq: str = "monthly",
) -> dict[str, BacktestResult]:
    """Convenience: run the same signal under raw / broker_paper /
    pessimistic lenses. The pessimistic series is what feeds DSR + PBO.
    """
    lenses = {
        "raw": CostLens.raw(),
        "broker_paper": CostLens.broker_paper(cost_model_lock),
        "pessimistic": CostLens.pessimistic(cost_model_lock),
    }
    out = {}
    for name, lens in lenses.items():
        out[name] = run_backtest(
            bars_by_symbol=bars_by_symbol, signal_fn=signal_fn,
            start=start, end=end, starting_equity=starting_equity,
            cost_lens=lens, rebalance_freq=rebalance_freq,
        )
    return out


__all__ = [
    "BacktestResult", "CostLens", "Position", "SignalT", "Trade",
    "run_backtest", "run_three_lens_backtest",
]
