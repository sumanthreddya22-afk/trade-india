# src/trading_bot/risk_manager.py
from dataclasses import dataclass
from decimal import Decimal

from trading_bot.alpaca_client import (
    AccountSnapshot,
    AssetClass,
    OrderRequest,
    OrderSide,
    Position,
)
from trading_bot.config import AppConfig
from trading_bot.exceptions import RiskRuleViolation


@dataclass(frozen=True)
class RiskState:
    """Daily/weekly P&L state + halt flags. Reconciled before each check."""

    daily_pnl_pct: Decimal
    weekly_pnl_pct: Decimal
    consecutive_losing_days: int
    halted: bool


class RiskManager:
    """Gates EVERY trade. No bypass."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config

    def check(
        self,
        order: OrderRequest,
        *,
        account: AccountSnapshot,
        positions: list[Position],
        state: RiskState,
        regime: str,
    ) -> None:
        """Raise RiskRuleViolation if any rule is breached. Returns None on success."""
        if state.halted:
            raise RiskRuleViolation(
                rule="halted",
                detail="trading is halted by circuit-breaker; manual reset required",
            )
        self._check_per_trade_risk(order, account)
        self._check_max_position(order, account)
        self._check_concentration(order, positions, account)
        self._check_asset_class_caps(order, positions, account, regime)
        self._check_daily_weekly_limits(state)

    # ---- individual rule helpers ----

    def _check_per_trade_risk(self, o: OrderRequest, a: AccountSnapshot) -> None:
        # risk = (entry - stop) * qty for buy, (stop - entry) * qty for sell
        if o.side == OrderSide.BUY:
            per_share_risk = o.limit_price - o.stop_loss_price
        else:
            per_share_risk = o.stop_loss_price - o.limit_price
        if per_share_risk <= 0:
            raise RiskRuleViolation(
                rule="stop_loss_direction",
                detail=f"stop {o.stop_loss_price} on wrong side of entry {o.limit_price}",
            )
        risk_dollars = per_share_risk * o.qty
        risk_pct = (risk_dollars / a.equity) * Decimal("100")
        limit = Decimal(str(self._cfg.risk.per_trade_risk_pct))
        if risk_pct > limit:
            raise RiskRuleViolation(
                rule="per_trade_risk_pct",
                detail=f"risk {risk_pct:.2f}% exceeds limit {limit}%",
            )

    def _check_max_position(self, o: OrderRequest, a: AccountSnapshot) -> None:
        notional = o.limit_price * o.qty
        pct = (notional / a.equity) * Decimal("100")
        limit = Decimal(str(self._cfg.risk.max_position_pct))
        if pct > limit:
            raise RiskRuleViolation(
                rule="max_position_pct",
                detail=f"position {pct:.2f}% exceeds limit {limit}%",
            )

    def _check_concentration(
        self, o: OrderRequest, positions: list[Position], a: AccountSnapshot
    ) -> None:
        existing = next((p for p in positions if p.symbol == o.symbol), None)
        existing_notional = existing.market_value if existing else Decimal("0")
        new_notional = existing_notional + (o.limit_price * o.qty if o.side == OrderSide.BUY else 0)
        pct = (new_notional / a.equity) * Decimal("100")
        limit = Decimal(str(self._cfg.risk.max_symbol_concentration_pct))
        if pct > limit:
            raise RiskRuleViolation(
                rule="max_symbol_concentration_pct",
                detail=f"{o.symbol} concentration {pct:.2f}% exceeds limit {limit}%",
            )

    def _check_asset_class_caps(
        self,
        o: OrderRequest,
        positions: list[Position],
        a: AccountSnapshot,
        regime: str,
    ) -> None:
        existing_by_class = {"stock": Decimal("0"), "crypto": Decimal("0"), "option": Decimal("0")}
        for p in positions:
            ac = p.asset_class.replace("us_equity", "stock").replace("us_option", "option")
            if ac in existing_by_class:
                existing_by_class[ac] += p.market_value
        new_class = o.asset_class.value
        new_notional = (o.limit_price * o.qty) if o.side == OrderSide.BUY else Decimal("0")
        proposed = existing_by_class.get(new_class, Decimal("0")) + new_notional
        proposed_pct = (proposed / a.equity) * Decimal("100")

        regime_caps = self._cfg.regime_allocations.get(regime)
        if regime_caps is None:
            raise RiskRuleViolation(
                rule="regime_unknown", detail=f"regime '{regime}' not in config"
            )
        cap_map = {
            "stock": Decimal(str(regime_caps.stocks)),
            "crypto": Decimal(str(regime_caps.crypto)),
            "option": Decimal(str(regime_caps.options)),
        }
        cap = cap_map[new_class]
        if proposed_pct > cap:
            raise RiskRuleViolation(
                rule="asset_class_cap",
                detail=f"{new_class} {proposed_pct:.2f}% exceeds regime cap {cap}%",
            )

    def _check_daily_weekly_limits(self, s: RiskState) -> None:
        d_limit = Decimal(str(self._cfg.risk.daily_loss_limit_pct))
        w_limit = Decimal(str(self._cfg.risk.weekly_loss_limit_pct))
        if s.daily_pnl_pct <= -d_limit:
            raise RiskRuleViolation(
                rule="daily_loss_limit",
                detail=f"daily P&L {s.daily_pnl_pct}% breaches -{d_limit}%",
            )
        if s.weekly_pnl_pct <= -w_limit:
            raise RiskRuleViolation(
                rule="weekly_loss_limit",
                detail=f"weekly P&L {s.weekly_pnl_pct}% breaches -{w_limit}%",
            )

    # ---- options-specific gate (wheel collateral) ----

    def option_collateral_ok(
        self, *,
        equity: Decimal,
        prospective_collateral: Decimal,
        existing_options_value: Decimal,
        per_symbol_collateral: Decimal,
    ) -> tuple[bool, str]:
        """Check options-allocation cap + per-symbol concentration.

        Returns (ok, reason). reason="" when ok.
        """
        if equity <= 0:
            return False, "equity_zero"
        options_max = Decimal(str(self._cfg.allocation.options_max_pct))
        sym_max = Decimal(str(self._cfg.risk.max_symbol_concentration_pct))
        options_pct = (existing_options_value + prospective_collateral) / equity * Decimal("100")
        if options_pct > options_max:
            return False, f"options_cap ({options_pct:.1f}% > {options_max}%)"
        sym_pct = per_symbol_collateral / equity * Decimal("100")
        if sym_pct > sym_max:
            return False, f"symbol_concentration ({sym_pct:.1f}% > {sym_max}%)"
        return True, ""
