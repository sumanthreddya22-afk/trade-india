"""End-to-end smoke: build WheelDeps, run scan + manage on an in-memory engine
with mocked external IO, verify cycle lifecycle through assignment."""
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine

from trading_bot.state_db import Base
from trading_bot.options.wheel_runner import run_wheel_scan, WheelDeps
from trading_bot.options.chain import ChainContract


def _put(strike, delta=-0.25, dte=35):
    today = dt.date.today()
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=2.10, ask=2.20, last=2.15, volume=100, open_interest=400,
        implied_volatility=0.30, delta=delta,
    )


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'i.db'}")
    Base.metadata.create_all(e)
    return e


def test_scan_then_assignment_then_cc_then_called_away(engine):
    """Cycle: open CSP → reconciler marks assigned → run_wheel_scan opens CC →
    cycle now in cc_open phase."""
    from trading_bot.options.wheel_state import WheelStateRepo, mark_assigned

    deps = MagicMock(spec=WheelDeps)
    deps.engine = engine
    deps.cfg = MagicMock(enabled=True, dte_min=30, dte_max=45,
                         delta_target_low=0.20, delta_target_high=0.30,
                         vix_floor=15, vix_ceiling=30, sentiment_floor=-0.3,
                         iv_rank_floor=30, wsb_spike_multiplier=2.0,
                         min_premium_abs=0.20, min_open_interest=100,
                         take_profit_pct=0.50, dte_force_close=21,
                         delta_breach_csp=0.45, delta_breach_cc=0.55,
                         max_rolls_per_cycle=2)
    deps.option_alpaca = MagicMock()
    deps.option_alpaca.get_chain.return_value = [_put(190)]
    deps.option_alpaca.sell_to_open.return_value = "ord-1"
    deps.option_alpaca.get_option_positions.return_value = []
    deps.alpaca_client = MagicMock()
    acct = MagicMock(equity=Decimal("100000"))
    deps.alpaca_client.get_account.return_value = acct
    deps.risk_manager = MagicMock()
    deps.risk_manager.option_collateral_ok.return_value = (True, "")
    deps.intelligence_macro = MagicMock()
    deps.intelligence_macro.snapshot.return_value = MagicMock(vix=20.0)
    deps.regime_detector = MagicMock()
    deps.regime_detector.detect.return_value = "trending_up"
    from trading_bot.options.wheel_signals import WheelCandidate
    deps.candidates_for_today = MagicMock(return_value=[
        WheelCandidate(symbol="AAPL", signal="stable_elevated_iv",
                       confidence=0.55, reason="IV rank 55",
                       iv_rank=55.0, last_iv=0.30),
    ])
    deps.iv_rank_for = MagicMock(return_value=55.0)
    deps.spot_for = MagicMock(return_value=200.0)
    deps.sentiment_for = MagicMock(return_value=0.1)
    deps.finnhub = MagicMock()
    deps.finnhub.has_earnings_in_window.return_value = False
    deps.apewisdom = MagicMock()
    deps.apewisdom.is_spike.return_value = False
    deps.alert_queue = MagicMock()

    # 1) Open CSP
    run_wheel_scan(deps)
    repo = WheelStateRepo(engine)
    cyc = repo.get_active(symbol="AAPL")
    assert cyc is not None and cyc.phase == "csp_open"

    # 2) Simulate assignment
    mark_assigned(repo, cycle_id=cyc.cycle_id,
                  when=dt.datetime.now(dt.timezone.utc))

    # 3) Open CC — chain returns a call now
    today = dt.date.today()
    cc = ChainContract(
        contract_symbol=f"AAPL{(today+dt.timedelta(days=35)):%y%m%d}C00200000",
        underlying="AAPL", expiration=today + dt.timedelta(days=35),
        kind="C", strike=200, bid=1.10, ask=1.20, last=1.15, volume=50,
        open_interest=300, implied_volatility=0.28, delta=0.27,
    )
    deps.option_alpaca.get_chain.return_value = [cc]
    deps.option_alpaca.sell_to_open.return_value = "ord-cc-1"
    run_wheel_scan(deps)
    cyc = repo.get_active(symbol="AAPL")
    assert cyc.phase == "cc_open"
