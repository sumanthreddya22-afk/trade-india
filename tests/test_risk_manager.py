from decimal import Decimal

import pytest

from trading_bot.alpaca_client import AccountSnapshot, AssetClass, OrderRequest, OrderSide, Position
from trading_bot.config import (
    AllocationConfig,
    AppConfig,
    EmailConfig,
    RegimeAllocation,
    RiskConfig,
    StorageConfig,
)
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.risk_manager import RiskManager, RiskState


def make_config(**overrides) -> AppConfig:
    risk = RiskConfig(
        daily_loss_limit_pct=2.0,
        weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0,
        max_position_pct=10.0,
        max_symbol_concentration_pct=5.0,
        max_consecutive_losing_days=3,
    )
    alloc = AllocationConfig(
        stocks_max_pct=70.0, crypto_max_pct=30.0, options_max_pct=20.0, cash_floor_pct=10.0
    )
    regimes = {
        "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
        "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
        "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
        "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
    }
    cfg = AppConfig(
        risk=risk,
        allocation=alloc,
        regime_allocations=regimes,
        email=EmailConfig(
            to="x@y.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"
        ),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )
    return cfg


@pytest.fixture
def cfg() -> AppConfig:
    return make_config()


@pytest.fixture
def acct() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
    )


@pytest.fixture
def state() -> RiskState:
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )


def test_risk_allows_normal_trade(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("10"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("195.00"),  # $1,950 trade, 1.95% of account
        stop_loss_price=Decimal("191.10"),  # 2% stop, $39 risk = 0.039% of account
    )
    rm.check(req, account=acct, positions=[], state=state, regime="trending_up")  # no raise


def test_risk_rejects_oversized_position(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("100"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("195.00"),  # $19,500 = 19.5% > 10% max
        stop_loss_price=Decimal("191.10"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="trending_up")
    assert e.value.rule == "max_position_pct"


def test_risk_rejects_excessive_per_trade_risk(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("10"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("195.00"),  # entry
        stop_loss_price=Decimal("85.00"),  # huge stop = $1100 risk = 1.1% > 1% limit
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="trending_up")
    assert e.value.rule == "per_trade_risk_pct"


def test_risk_rejects_when_halted(cfg, acct):
    rm = RiskManager(cfg)
    halted = RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=True,
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=halted, regime="trending_up")
    assert e.value.rule == "halted"


def test_risk_rejects_after_daily_loss_breach(cfg, acct):
    rm = RiskManager(cfg)
    breached = RiskState(
        daily_pnl_pct=Decimal("-2.5"),
        weekly_pnl_pct=Decimal("-1"),
        consecutive_losing_days=0,
        halted=False,
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=breached, regime="trending_up")
    assert e.value.rule == "daily_loss_limit"


def test_risk_rejects_after_weekly_loss_breach(cfg, acct):
    rm = RiskManager(cfg)
    breached = RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("-6"),
        consecutive_losing_days=0,
        halted=False,
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=breached, regime="trending_up")
    assert e.value.rule == "weekly_loss_limit"


def test_risk_rejects_concentration_breach(cfg, acct, state):
    rm = RiskManager(cfg)
    existing = Position(
        symbol="AAPL",
        qty=Decimal("20"),
        market_value=Decimal("4500"),  # already 4.5%
        avg_entry_price=Decimal("225"),
        current_price=Decimal("225"),
        unrealized_pl=Decimal("0"),
        asset_class="us_equity",
    )
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("5"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("200"),  # +$1000 → 5.5% > 5% cap
        stop_loss_price=Decimal("198"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[existing], state=state, regime="trending_up")
    assert e.value.rule == "max_symbol_concentration_pct"


def test_risk_rejects_asset_class_cap_in_risk_off(cfg, acct, state):
    rm = RiskManager(cfg)
    # risk_off: crypto cap is 5%
    req = OrderRequest(
        symbol="BTC/USD",
        qty=Decimal("0.5"),
        side=OrderSide.BUY,
        asset_class=AssetClass.CRYPTO,
        limit_price=Decimal("70000"),  # $35k = 35% — way over 5% cap
        stop_loss_price=Decimal("68000"),
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="risk_off")
    assert e.value.rule in {"asset_class_cap", "max_position_pct"}


def test_risk_rejects_inverted_stop_loss(cfg, acct, state):
    rm = RiskManager(cfg)
    req = OrderRequest(
        symbol="AAPL",
        qty=Decimal("1"),
        side=OrderSide.BUY,
        asset_class=AssetClass.STOCK,
        limit_price=Decimal("100"),
        stop_loss_price=Decimal("102"),  # stop ABOVE entry on a buy = inverted
    )
    with pytest.raises(RiskRuleViolation) as e:
        rm.check(req, account=acct, positions=[], state=state, regime="trending_up")
    assert e.value.rule == "stop_loss_direction"


# ---- option_collateral_ok tests (wheel) ----


def test_option_collateral_ok_passes_when_under_caps():
    cfg = make_config()
    rm = RiskManager(cfg)
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("100000"), prospective_collateral=Decimal("5000"),
        existing_options_value=Decimal("0"), per_symbol_collateral=Decimal("5000"),
    )
    assert ok and reason == ""


def test_option_collateral_ok_blocks_when_options_cap_breached():
    cfg = make_config()
    rm = RiskManager(cfg)
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("100000"),
        prospective_collateral=Decimal("3000"),
        existing_options_value=Decimal("18000"),  # already at 18%
        per_symbol_collateral=Decimal("3000"),
    )
    assert ok is False
    assert "options_cap" in reason


def test_option_collateral_ok_blocks_per_symbol_concentration():
    cfg = make_config()
    rm = RiskManager(cfg)
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("100000"), prospective_collateral=Decimal("3000"),
        existing_options_value=Decimal("0"),
        per_symbol_collateral=Decimal("6000"),  # 6% > 5%
    )
    assert ok is False
    assert "symbol_concentration" in reason


def test_option_collateral_ok_zero_equity():
    cfg = make_config()
    rm = RiskManager(cfg)
    ok, reason = rm.option_collateral_ok(
        equity=Decimal("0"), prospective_collateral=Decimal("1"),
        existing_options_value=Decimal("0"), per_symbol_collateral=Decimal("1"),
    )
    assert ok is False
    assert "equity_zero" in reason
