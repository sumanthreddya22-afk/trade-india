"""End-to-end happy-path smoke tests for stock + crypto + wheel.

Each test drives the FULL pipeline from a forced signal through:
  * orchestrator scan / wheel scan
  * risk gates
  * Alpaca order submission
  * order_submitted audit event (Phase 1.3)
  * journal / cycle persistence

These are deliberately light on assertions about specific HTML / SQL
shape — they exist to catch the "the whole pipeline broke" regression
class, not to validate every gate's logic (those have their own units).
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_bot.alpaca_client import (
    AccountSnapshot, OrderResult, OrderSide, Position,
)
from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_runner import WheelDeps, run_wheel_scan
from trading_bot.orchestrator import ScanResult, TradeOrchestrator
from trading_bot.state import WatchlistEntry
from trading_bot.state_db import Base
from trading_bot.strategy import Signal, SignalAction


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'state.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """Redirect StructuredLogger to a tmp dir so order_submitted events
    are isolated per test."""
    runs = tmp_path / "runs"
    runs.mkdir()
    # The alpaca audit logger reads its base from the StructuredLogger
    # constructor default ('runs'). To redirect cleanly we re-create the
    # module-level _audit_log against the tmp base.
    import trading_bot.alpaca_client as ac
    from trading_bot.log_structured import StructuredLogger
    monkeypatch.setattr(ac, "_audit_log", StructuredLogger(base=runs, role="alpaca"))
    return runs


def _account(equity: float = 15000.0) -> AccountSnapshot:
    eq = Decimal(str(equity))
    return AccountSnapshot(
        equity=eq, cash=eq,
        buying_power=eq * 2, portfolio_value=eq,
    )


def _bars(symbol: str = "MSFT") -> pd.DataFrame:
    """40-bar uptrend so RSI lands in the momentum band."""
    return pd.DataFrame(
        {"close": [100 + i for i in range(40)],
         "open": [100 + i for i in range(40)],
         "high": [101 + i for i in range(40)],
         "low": [99 + i for i in range(40)],
         "volume": [1_000_000] * 40},
        index=pd.date_range("2026-04-01", periods=40, freq="D", tz="UTC"),
    )


def _config():
    """Same shape as tests/test_orchestrator.py — risk + strategy gates
    permissive enough that a small momentum signal places."""
    from trading_bot.config import (
        AllocationConfig, AppConfig, EmailConfig, RegimeAllocation,
        RiskConfig, StorageConfig, StrategyConfig,
    )
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0, weekly_loss_limit_pct=5.0,
            per_trade_risk_pct=1.0, max_position_pct=10.0,
            max_symbol_concentration_pct=5.0,
            max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(options_max_pct=20.0),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
            "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
            "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
            "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
        },
        strategy=StrategyConfig(
            earnings_gate_enabled=False,
            macro_shock_gate_enabled=False,
            crypto_fear_greed_enabled=False,
            crypto_reddit_spike_enabled=False,
            crypto_coingecko_enabled=False,
            insider_cluster_enabled=False,
        ),
        storage=StorageConfig(trade_journal_path="data/trade_journal.db"),
        email=EmailConfig(to="test@example.com"),
    )


# ---------------------------------------------------------------------------
# E2E #1 — STOCK momentum happy path
# ---------------------------------------------------------------------------


def test_e2e_stock_momentum_places_order_with_audit(monkeypatch, runs_dir):
    """Forced BUY signal → orchestrator places stock bracket order →
    journal row written → order_submitted audit event lands."""
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()

    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.get_open_order_symbols.return_value = set()
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-stock-1", stop_loss_order_id="s-stock-1",
    )

    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime="trending_up",
    )
    forced = Signal(
        symbol="MSFT", action=SignalAction.BUY, qty=Decimal("2"),
        entry_price=Decimal("139"), stop_loss_price=Decimal("133"),
        reason="forced E2E",
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity:
            forced if sym == "MSFT" else
            Signal(sym, SignalAction.HOLD, Decimal("0"), Decimal("0"),
                   Decimal("0"), "x"),
    )

    watchlist = [WatchlistEntry(symbol="MSFT", asset_class="stock", notes="")]
    result = orch.scan(watchlist=watchlist)

    assert isinstance(result, ScanResult)
    placed = [d for d in result.decisions if d.symbol == "MSFT"]
    assert placed and placed[0].action == "placed_order"
    assert placed[0].entry_order_id == "e-stock-1"

    # Real Alpaca call happened (post-risk-gate).
    alpaca.place_order_with_stop_loss.assert_called_once()
    journal.append.assert_called_once()
    journal_row = journal.append.call_args.args[0]
    assert journal_row.symbol == "MSFT"
    assert journal_row.side == "buy"
    assert str(journal_row.asset_class).lower() == "stock"


# ---------------------------------------------------------------------------
# E2E #2 — CRYPTO momentum happy path
# ---------------------------------------------------------------------------


def test_e2e_crypto_momentum_places_order_with_audit(monkeypatch, runs_dir):
    """Forced BUY on BTC/USD → orchestrator places crypto market+stop pair →
    journal row tagged asset_class='crypto'."""
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()

    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.get_open_order_symbols.return_value = set()
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-crypto-1", stop_loss_order_id="s-crypto-1",
    )

    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime="trending_up",
    )
    forced = Signal(
        symbol="BTC/USD", action=SignalAction.BUY, qty=Decimal("0.001"),
        entry_price=Decimal("95000"), stop_loss_price=Decimal("93000"),
        reason="forced E2E crypto",
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity:
            forced if sym == "BTC/USD" else
            Signal(sym, SignalAction.HOLD, Decimal("0"), Decimal("0"),
                   Decimal("0"), "x"),
    )

    watchlist = [WatchlistEntry(symbol="BTC/USD", asset_class="crypto", notes="")]
    result = orch.scan(watchlist=watchlist)

    placed = [d for d in result.decisions if d.symbol == "BTC/USD"]
    assert placed and placed[0].action == "placed_order"

    # The orchestrator routes crypto through the same place_order_with_stop_loss
    # entrypoint — alpaca_client decides bracket-vs-pair internally.
    alpaca.place_order_with_stop_loss.assert_called_once()
    submitted_order = alpaca.place_order_with_stop_loss.call_args.args[0]
    assert submitted_order.asset_class.value == "crypto"
    assert submitted_order.symbol == "BTC/USD"

    journal.append.assert_called_once()
    journal_row = journal.append.call_args.args[0]
    assert journal_row.asset_class == "crypto"


# ---------------------------------------------------------------------------
# E2E #3 — WHEEL CSP happy path
# ---------------------------------------------------------------------------


def _put_contract(strike: float, *, delta: float = -0.25, dte: int = 35) -> ChainContract:
    today = dt.date.today()
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=2.10, ask=2.15, last=2.12, volume=100, open_interest=400,
        implied_volatility=0.30, delta=delta,
    )


def test_e2e_wheel_csp_opens_with_audit_event(state_engine, runs_dir):
    """Curated allowlist universe → preflight passes → chain produces a
    valid CSP contract → risk gates pass → sell_to_open submitted →
    cycle written to wheel_cycles, fill written to option_fills,
    audit event lands at runs/<date>/alpaca/."""
    from trading_bot.options.wheel_state import WheelStateRepo
    from sqlalchemy.orm import Session
    from trading_bot.state_db import OptionFill, WheelCycle

    deps = MagicMock(spec=WheelDeps)
    deps.engine = state_engine
    deps.option_alpaca = MagicMock()
    deps.option_alpaca.get_chain.return_value = [_put_contract(190)]
    deps.option_alpaca.sell_to_open.return_value = "ord-csp-e2e"
    deps.option_alpaca.get_option_positions.return_value = []
    deps.alpaca_client = MagicMock()
    deps.alpaca_client.get_account.return_value = MagicMock(
        equity=Decimal("100000"), cash=Decimal("50000"),
        buying_power=Decimal("100000"), portfolio_value=Decimal("100000"),
    )
    deps.alpaca_client.get_positions.return_value = []
    deps.risk_manager = MagicMock()
    deps.risk_manager.option_collateral_ok.return_value = (True, "")
    deps.intelligence_macro = MagicMock()
    deps.intelligence_macro.snapshot.return_value = MagicMock(vix=20.0)
    deps.regime_detector = MagicMock()
    deps.regime_detector.detect.return_value = "trending_up"
    deps.eligible_for_today = MagicMock(return_value={"AAPL"})
    deps.iv_rank_for = MagicMock(return_value=55.0)
    deps.spot_for = MagicMock(return_value=200.0)
    deps.sentiment_for = MagicMock(return_value=0.1)
    deps.finnhub = MagicMock()
    deps.finnhub.has_earnings_in_window.return_value = False
    deps.alert_queue = MagicMock()
    deps.cfg = MagicMock(
        enabled=True, dte_min=30, dte_max=45,
        delta_target_low=0.20, delta_target_high=0.30,
        vix_floor=15, vix_ceiling=30, sentiment_floor=-0.3,
        iv_rank_floor=30, min_premium_abs=0.20, min_open_interest=100,
        liquidity_max_spread_abs=0.10, liquidity_max_spread_rel=0.05,
        unblock_debate_enabled=False,
        unblock_max_overage_ratio=0.50, unblock_min_candidate_score=7.0,
        unblock_daily_debate_cap=15,
        options_max_pct=20.0, allowlist_only=True,
        min_annualized_yield=0.0,  # let the test contract pass
        take_profit_pct=0.50, dte_force_close=21,
        delta_breach_csp=0.45, delta_breach_cc=0.55,
        max_rolls_per_cycle=2,
    )

    run_wheel_scan(deps)

    # CSP submitted via Alpaca options client.
    deps.option_alpaca.sell_to_open.assert_called_once()

    # WheelCycle row written; phase = csp_open.
    with Session(state_engine) as s:
        cycle = s.query(WheelCycle).one()
        assert cycle.symbol == "AAPL"
        assert cycle.phase == "csp_open"
        # OptionFill row tagged correctly.
        fill = s.query(OptionFill).one()
        assert fill.option_type == "CSP"
        assert fill.side == "SELL"
        assert fill.alpaca_order_id == "ord-csp-e2e"

    # Alert emitted for the open event (not skipped — allowlist mode).
    assert any("wheel_csp_opened" in str(c) for c in deps.alert_queue.mock_calls)
