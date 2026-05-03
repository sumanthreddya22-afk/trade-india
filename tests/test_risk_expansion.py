"""W2c — RiskManager expansion: gross/net notional caps + per-strategy halt.

Adds two PDF-prescribed gates without changing existing behavior:
  - gross_notional_cap and net_notional_cap (defaults match current
    paper-only practice, structure exists for live tightening)
  - per-strategy halt: ``RiskState.halted_strategies`` lets the operator
    halt one strategy lane (e.g., wheel) while leaving others running.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.shared.alpaca_client import (
    AccountSnapshot, AssetClass, OrderRequest, OrderSide, Position,
)
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.shared.risk_manager import RiskManager, RiskState


def _config(*, gross_cap_pct: float = 200.0, net_cap_pct: float = 100.0):
    from trading_bot.shared.config import (
        AllocationConfig, AppConfig, EmailConfig, RegimeAllocation,
        RiskConfig, StorageConfig, StrategyConfig,
    )
    risk = RiskConfig(
        daily_loss_limit_pct=2.0, weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0, max_position_pct=10.0,
        max_symbol_concentration_pct=5.0, max_consecutive_losing_days=3,
        gross_cap_pct=gross_cap_pct, net_cap_pct=net_cap_pct,
    )
    return AppConfig(
        risk=risk,
        allocation=AllocationConfig(stocks_max_pct=70.0, crypto_max_pct=30.0,
                                    options_max_pct=20.0, cash_floor_pct=10.0),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
        },
        email=EmailConfig(to="t@x.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
        strategy=StrategyConfig(
            earnings_gate_enabled=False, macro_shock_gate_enabled=False,
            crypto_fear_greed_enabled=False, crypto_reddit_spike_enabled=False,
            crypto_coingecko_enabled=False, insider_cluster_enabled=False,
        ),
    )


def _account(equity: float = 100000.0):
    return AccountSnapshot(
        equity=Decimal(str(equity)), cash=Decimal(str(equity)),
        buying_power=Decimal(str(equity * 2)), portfolio_value=Decimal(str(equity)),
    )


def _state():
    return RiskState(
        daily_pnl_pct=Decimal("0"), weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0, halted=False,
    )


def _order(qty: float = 10.0, price: float = 100.0):
    return OrderRequest(
        symbol="AAPL", qty=Decimal(str(qty)), side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal(str(price)),
        stop_loss_price=Decimal(str(price * 0.95)),
    )


class TestGrossNotional:
    def test_passes_when_below_cap(self):
        rm = RiskManager(_config(gross_cap_pct=200.0))
        rm.check(
            _order(qty=10, price=100),  # 1000 notional
            account=_account(100000),
            positions=[],
            state=_state(), regime="trending_up",
        )

    def test_blocks_when_gross_exceeds_cap(self):
        rm = RiskManager(_config(gross_cap_pct=10.0))  # cap = 10% = $10,000
        existing = [
            Position(symbol="MSFT", qty=Decimal("100"), market_value=Decimal("9500"),
                     avg_entry_price=Decimal("95"), current_price=Decimal("95"),
                     unrealized_pl=Decimal("0"), asset_class="us_equity"),
        ]
        with pytest.raises(RiskRuleViolation, match="gross"):
            rm.check(
                _order(qty=10, price=100),  # 1000 → gross would be 10500 > 10000
                account=_account(100000),
                positions=existing,
                state=_state(), regime="trending_up",
            )


class TestPerStrategyHalt:
    def test_halts_named_strategy(self):
        rm = RiskManager(_config())
        state = RiskState(
            daily_pnl_pct=Decimal("0"), weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0, halted=False,
            halted_strategies=frozenset({"momentum"}),
        )
        with pytest.raises(RiskRuleViolation, match="strategy_halt"):
            rm.check(
                _order(),
                account=_account(),
                positions=[], state=state, regime="trending_up",
                strategy_name="momentum",
            )

    def test_does_not_halt_unrelated_strategy(self):
        rm = RiskManager(_config())
        state = RiskState(
            daily_pnl_pct=Decimal("0"), weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0, halted=False,
            halted_strategies=frozenset({"wheel"}),
        )
        # Should not raise — momentum is not halted, only wheel is
        rm.check(
            _order(),
            account=_account(),
            positions=[], state=state, regime="trending_up",
            strategy_name="momentum",
        )

    def test_legacy_state_without_halted_strategies_still_works(self):
        rm = RiskManager(_config())
        # Legacy RiskState construction (no halted_strategies)
        state = RiskState(
            daily_pnl_pct=Decimal("0"), weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0, halted=False,
        )
        # Default empty — no halt should fire
        rm.check(
            _order(),
            account=_account(),
            positions=[], state=state, regime="trending_up",
        )
