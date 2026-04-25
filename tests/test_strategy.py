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
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.5"))
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    # risk budget = 0.5% of 15000 = $75
    # stop is max(EMA=190, close*0.95=185.25) = 190 (closer to entry)
    # per-share risk = 195 - 190 = 5
    # qty = 75 / 5 = 15
    assert sig.qty == Decimal("15")
    assert sig.stop_loss_price == Decimal("190.00")


def test_momentum_skips_when_qty_zero():
    s = MomentumStrategy(per_trade_risk_pct=Decimal("0.01"))
    ind = _ind(rsi=60, macd=0.5, macd_sig=0.3, ema=190, ret5=0.02, close=195)
    sig = s.evaluate("AAPL", ind, equity=Decimal("15000"))
    assert sig.action == SignalAction.HOLD
