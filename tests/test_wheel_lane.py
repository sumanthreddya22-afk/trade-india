# tests/test_wheel_lane.py
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock
from trading_bot.options.wheel_lane import WheelLane, WheelDecision, WheelInputs
from trading_bot.options.chain import ChainContract
from trading_bot.config import WheelConfig
from trading_bot.intelligence_apewisdom import MentionRow


def _put(strike, delta, *, dte=35, bid=2.0, oi=200, iv=0.3):
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=bid, ask=bid + 0.05, last=bid, volume=10, open_interest=oi,
        implied_volatility=iv, delta=delta,
    )


def test_wheel_lane_emits_csp_when_all_filters_pass():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    ape = MagicMock(); ape.is_spike.return_value = False
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "open_csp"
    assert out.contract is not None and out.contract.strike == 190


def test_wheel_lane_skips_when_iv_rank_low():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    ape = MagicMock(); ape.is_spike.return_value = False
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=10.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "iv_rank" in out.reason


def test_wheel_lane_skips_when_earnings_present():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = True
    ape = MagicMock(); ape.is_spike.return_value = False
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "earnings" in out.reason


def test_wheel_lane_skips_when_regime_risk_off():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); ape = MagicMock()
    inp = WheelInputs(
        symbol="AAPL", regime="risk_off", vix=35.0, sentiment_score=0.0,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "regime" in out.reason


def test_wheel_lane_skips_when_wsb_spike():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    ape = MagicMock(); ape.is_spike.return_value = True
    inp = WheelInputs(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin, apewisdom=ape, today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    out = WheelLane(cfg).evaluate(inp)
    assert out.action == "skip" and "wsb" in out.reason
