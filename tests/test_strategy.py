from decimal import Decimal

from trading_bot.market_data import Indicators
from trading_bot.strategy import MomentumStrategy, SignalAction


def _ind(rsi: float, macd: float, macd_sig: float, ema: float, ret5: float, close: float) -> Indicators:
    return Indicators(
        last_close=close,
        rsi_14=rsi,
        macd=macd,
        macd_signal=macd_sig,
        ema_20=ema,
        return_5d=ret5,
    )


def test_momentum_emits_buy_when_all_rules_pass():
    s = MomentumStrategy()
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.BUY
    assert sig.symbol == "AAPL"
    assert sig.entry_price == Decimal("195")
    assert sig.stop_loss_price < sig.entry_price


def test_momentum_holds_when_rsi_too_high():
    s = MomentumStrategy()
    ind = _ind(rsi=75, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD


def test_momentum_holds_when_macd_bearish():
    s = MomentumStrategy()
    ind = _ind(rsi=60, macd=0.1, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD


def test_momentum_holds_when_below_ema():
    s = MomentumStrategy()
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=200, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD


def test_momentum_position_size_respects_risk_budget():
    """qty is min(risk-budget qty, concentration-cap qty)."""
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.5"))
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    # risk budget = 0.5% of 15000 = $75; per-share risk = $5; risk_qty = 15
    # concentration cap = 4.5% of 15000 = $675; conc_qty = 675/195 ≈ 3.46
    # min(15, 3.46) = 3.46 → floor = 3
    assert sig.qty == Decimal("3")
    assert sig.stop_loss_price == Decimal("190.00")


def test_momentum_position_size_when_risk_smaller_than_concentration():
    """When per-trade risk binds before concentration, risk wins."""
    # tiny stop distance => risk_qty very small, while conc_qty stays big
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.5"))
    # stop = max(190, 185.25) = 190 → distance = 5 → risk_qty = 75/5 = 15
    # but if we set higher concentration cap (50%), conc_qty = 7500/195 ≈ 38.46
    # so risk binds at 15
    s2 = MomentumStrategy(per_trade_risk_pct=Decimal("0.5"), max_concentration_pct=Decimal("50"))
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s2.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.qty == Decimal("15")


def test_momentum_skips_when_qty_zero():
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.01"))
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD
