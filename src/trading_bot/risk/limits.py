"""Typed projections of the ``risk_policy.lock`` numerics.

The lock file is a generic JSON document. Risk-check modules want
typed access (``policy.account.daily_drawdown_pct_of_equity``) rather
than ``policy["account"]["daily_drawdown_pct_of_equity"]``. This module
provides the conversion + a couple of derived helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class AccountLimits:
    daily_drawdown_pct: float
    trailing_drawdown_pct: float
    trailing_drawdown_window_days: int
    intraday_pnl_floor_pct: float


@dataclass(frozen=True)
class AssetClassLimits:
    equity_gross_max_pct: float
    crypto_gross_max_pct: float
    options_buying_power_util_max_pct: float


@dataclass(frozen=True)
class LaneLimits:
    per_lane_allocation_max_pct: float
    per_lane_daily_loss_max_pct: float


@dataclass(frozen=True)
class StrategyLimits:
    realized_plus_unrealized_loss_30d_max_pct: float


@dataclass(frozen=True)
class SymbolLimits:
    per_symbol_gross_max_pct: float


@dataclass(frozen=True)
class OrderLimits:
    per_order_at_risk_max_pct: float
    stop_coverage_required_within_seconds: int


@dataclass(frozen=True)
class KillSwitchLimits:
    broker_api_error_rate_max_pct: float
    broker_api_error_rate_window_minutes: int
    unknown_position_max_age_minutes: int
    wall_clock_skew_max_seconds: int


@dataclass(frozen=True)
class RiskLimits:
    account: AccountLimits
    asset_class: AssetClassLimits
    lane: LaneLimits
    strategy: StrategyLimits
    symbol: SymbolLimits
    order: OrderLimits
    kill_switches: KillSwitchLimits


def parse_risk_policy(payload: Mapping) -> RiskLimits:
    a = payload["account"]
    ac = payload["asset_class"]
    ln = payload["lane"]
    st = payload["strategy"]
    sy = payload["symbol"]
    od = payload["order"]
    ks = payload["kill_switches"]
    return RiskLimits(
        account=AccountLimits(
            daily_drawdown_pct=float(a["daily_drawdown_pct_of_equity"]),
            trailing_drawdown_pct=float(a["trailing_drawdown_pct_of_equity"]),
            trailing_drawdown_window_days=int(a["trailing_drawdown_window_days"]),
            intraday_pnl_floor_pct=float(a["intraday_pnl_floor_pct_of_equity"]),
        ),
        asset_class=AssetClassLimits(
            equity_gross_max_pct=float(ac["equity_gross_max_pct"]),
            crypto_gross_max_pct=float(ac["crypto_gross_max_pct"]),
            options_buying_power_util_max_pct=float(ac["options_buying_power_util_max_pct"]),
        ),
        lane=LaneLimits(
            per_lane_allocation_max_pct=float(ln["per_lane_allocation_max_pct"]),
            per_lane_daily_loss_max_pct=float(ln["per_lane_daily_loss_max_pct"]),
        ),
        strategy=StrategyLimits(
            realized_plus_unrealized_loss_30d_max_pct=float(
                st["realized_plus_unrealized_loss_30d_max_pct"]
            ),
        ),
        symbol=SymbolLimits(
            per_symbol_gross_max_pct=float(sy["per_symbol_gross_max_pct"]),
        ),
        order=OrderLimits(
            per_order_at_risk_max_pct=float(od["per_order_at_risk_max_pct"]),
            stop_coverage_required_within_seconds=int(od["stop_coverage_required_within_seconds"]),
        ),
        kill_switches=KillSwitchLimits(
            broker_api_error_rate_max_pct=float(ks["broker_api_error_rate_max_pct"]),
            broker_api_error_rate_window_minutes=int(ks["broker_api_error_rate_window_minutes"]),
            unknown_position_max_age_minutes=int(ks["unknown_position_max_age_minutes"]),
            wall_clock_skew_max_seconds=int(ks["wall_clock_skew_max_seconds"]),
        ),
    )


__all__ = [
    "AccountLimits",
    "AssetClassLimits",
    "KillSwitchLimits",
    "LaneLimits",
    "OrderLimits",
    "RiskLimits",
    "StrategyLimits",
    "SymbolLimits",
    "parse_risk_policy",
]
