import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_runner import run_wheel_scan, run_wheel_manage, WheelDeps
from trading_bot.options.wheel_lane import WheelDecision
from trading_bot.options.wheel_state import WheelStateRepo
from trading_bot.state_db import Base, OptionFill, WheelCycle


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'r.db'}")
    Base.metadata.create_all(e)
    return e


def _put(strike, delta=-0.25):
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=35)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=2.10, ask=2.20, last=2.15, volume=100, open_interest=400,
        implied_volatility=0.30, delta=delta,
    )


def _deps(engine):
    d = MagicMock(spec=WheelDeps)
    d.engine = engine
    d.option_alpaca = MagicMock()
    d.option_alpaca.get_chain.return_value = [_put(190)]
    d.option_alpaca.sell_to_open.return_value = "ord-csp-1"
    d.option_alpaca.buy_to_close.return_value = "ord-bto-1"
    d.option_alpaca.get_option_positions.return_value = []
    d.alpaca_client = MagicMock()
    acct = MagicMock(); acct.equity = Decimal("100000"); acct.cash = Decimal("50000")
    acct.buying_power = Decimal("100000"); acct.portfolio_value = Decimal("100000")
    d.alpaca_client.get_account.return_value = acct
    d.alpaca_client.get_positions.return_value = []
    d.risk_manager = MagicMock()
    d.risk_manager.option_collateral_ok.return_value = (True, "")
    d.intelligence_macro = MagicMock(); d.intelligence_macro.snapshot.return_value = MagicMock(vix=20.0)
    d.regime_detector = MagicMock(); d.regime_detector.detect.return_value = "trending_up"
    d.eligible_for_today = MagicMock(return_value={"AAPL"})
    d.iv_rank_for = MagicMock(return_value=55.0)
    d.spot_for = MagicMock(return_value=200.0)
    d.sentiment_for = MagicMock(return_value=0.1)
    d.finnhub = MagicMock(); d.finnhub.has_earnings_in_window.return_value = False
    d.apewisdom = MagicMock(); d.apewisdom.is_spike.return_value = False
    d.alert_queue = MagicMock()
    d.cfg = MagicMock()
    d.cfg.enabled = True
    d.cfg.delta_target_low = 0.20
    d.cfg.delta_target_high = 0.30
    d.cfg.dte_min = 30
    d.cfg.dte_max = 45
    d.cfg.vix_floor = 15
    d.cfg.vix_ceiling = 30
    d.cfg.sentiment_floor = -0.3
    d.cfg.iv_rank_floor = 30
    d.cfg.wsb_spike_multiplier = 2.0
    d.cfg.min_premium_abs = 0.20
    d.cfg.min_open_interest = 100
    d.cfg.take_profit_pct = 0.50
    d.cfg.dte_force_close = 21
    d.cfg.delta_breach_csp = 0.45
    d.cfg.delta_breach_cc = 0.55
    d.cfg.max_rolls_per_cycle = 2
    return d


def test_wheel_scan_opens_csp_and_writes_journal_and_alert(engine):
    d = _deps(engine)
    run_wheel_scan(d)
    d.option_alpaca.sell_to_open.assert_called_once()
    with Session(engine) as s:
        cyc = s.query(WheelCycle).one()
        assert cyc.symbol == "AAPL" and cyc.phase == "csp_open"
        fill = s.query(OptionFill).one()
        assert fill.option_type == "CSP" and fill.side == "SELL"
    assert any("wheel_csp_opened" in str(c) for c in d.alert_queue.mock_calls)


def test_wheel_scan_skips_when_risk_blocks(engine):
    d = _deps(engine)
    d.risk_manager.option_collateral_ok.return_value = (False, "options_cap")
    run_wheel_scan(d)
    d.option_alpaca.sell_to_open.assert_not_called()
    assert any("wheel_allocation_cap" in str(c) for c in d.alert_queue.mock_calls)


def test_wheel_manage_buys_to_close_at_50pct_profit(engine):
    d = _deps(engine)
    repo = WheelStateRepo(engine)
    with Session(engine) as s:
        s.add(WheelCycle(cycle_id="c1", symbol="AAPL", phase="csp_open",
                         opened_at=dt.datetime.now(dt.timezone.utc),
                         csp_contract="AAPL250603P00190000",
                         csp_strike=Decimal("190"),
                         csp_expiration=dt.date(2025, 6, 3),
                         csp_credit=Decimal("2.10")))
        s.commit()
    pos = MagicMock()
    pos.symbol = "AAPL250603P00190000"
    pos.qty = "-1"
    pos.cost_basis = "-210"
    snap = MagicMock()
    snap.contract_symbol = "AAPL250603P00190000"
    snap.bid = 1.00; snap.ask = 1.05  # mid = 1.025 ≤ 50% of 2.10
    snap.delta = -0.20
    snap.expiration = dt.date(2025, 6, 3)
    d.option_alpaca.get_option_positions.return_value = [pos]
    d.option_alpaca.snapshot_for_contract = MagicMock(return_value=snap)
    run_wheel_manage(d)
    d.option_alpaca.buy_to_close.assert_called_once()
    assert any("wheel_take_profit" in str(c) for c in d.alert_queue.mock_calls)
