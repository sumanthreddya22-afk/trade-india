# tests/test_wheel_lane.py
import datetime as dt
from unittest.mock import MagicMock

from trading_bot.shared.config import WheelConfig
from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_lane import WheelInputs, WheelLane


def _put(strike, delta, *, dte=35, bid=2.5, oi=200, iv=0.3):
    """Bucket C: bid bumped 2.0 -> 2.5 so the put clears the new
    min_annualized_yield (12%) gate at strike=190 / dte=35."""
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=strike,
        bid=bid, ask=bid + 0.05, last=bid, volume=10, open_interest=oi,
        implied_volatility=iv, delta=delta,
    )


def _inputs(**overrides) -> WheelInputs:
    """Bucket C: shared default inputs. apewisdom field was removed when
    the dead wsb_spike_multiplier gate was deleted from passes_preflight."""
    fin = MagicMock(); fin.has_earnings_in_window.return_value = False
    base = dict(
        symbol="AAPL", regime="trending_up", vix=20.0, sentiment_score=0.1,
        spot=200.0, iv_rank=55.0, finnhub=fin,
        today=dt.date(2026, 4, 28),
        chain=[_put(190, -0.27)], cycle=None, cost_basis=None,
    )
    base.update(overrides)
    return WheelInputs(**base)


def test_wheel_lane_emits_csp_when_all_filters_pass():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    out = WheelLane(cfg).evaluate(_inputs())
    assert out.action == "open_csp"
    assert out.contract is not None and out.contract.strike == 190


def test_wheel_lane_skips_when_iv_rank_low():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    out = WheelLane(cfg).evaluate(_inputs(iv_rank=10.0))
    assert out.action == "skip" and "iv_rank" in out.reason


def test_wheel_lane_skips_when_earnings_present():
    cfg = WheelConfig(enabled=True)
    fin = MagicMock(); fin.has_earnings_in_window.return_value = True
    out = WheelLane(cfg).evaluate(_inputs(finnhub=fin))
    assert out.action == "skip" and "earnings" in out.reason


def test_wheel_lane_skips_when_regime_risk_off():
    cfg = WheelConfig(enabled=True)
    out = WheelLane(cfg).evaluate(_inputs(regime="risk_off", vix=35.0))
    assert out.action == "skip" and "regime" in out.reason


# ---- preflight (cheap gates, no chain) ----


def test_passes_preflight_returns_none_when_all_pass():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    assert WheelLane(cfg).passes_preflight(_inputs(chain=[])) is None


def test_passes_preflight_blocks_when_cycle_already_csp_open():
    cfg = WheelConfig(enabled=True)
    cycle = MagicMock(); cycle.phase = "csp_open"
    reason = WheelLane(cfg).passes_preflight(_inputs(chain=[], cycle=cycle))
    assert reason is not None and "cycle_already_open" in reason


def test_passes_preflight_allows_when_cycle_assigned():
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    cycle = MagicMock(); cycle.phase = "assigned"
    inp = _inputs(chain=[], cycle=cycle, cost_basis=187.5)
    assert WheelLane(cfg).passes_preflight(inp) is None
