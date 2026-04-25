from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import Enum

from trading_bot.market_data import Indicators


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: SignalAction
    qty: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    reason: str


class MomentumStrategy:
    """Phase 1 momentum entry rule. Long-only, integer share quantities."""

    def __init__(
        self,
        rsi_lower: float = 55.0,
        rsi_upper: float = 70.0,
        per_trade_risk_pct: Decimal = Decimal("0.5"),
        stop_pct: Decimal = Decimal("0.05"),
        max_concentration_pct: Decimal = Decimal("4.5"),
    ) -> None:
        self._rsi_lower = rsi_lower
        self._rsi_upper = rsi_upper
        self._risk_pct = per_trade_risk_pct
        self._stop_pct = stop_pct
        self._max_concentration_pct = max_concentration_pct

    def evaluate(self, symbol: str, ind: Indicators, equity: Decimal) -> Signal:
        if not (self._rsi_lower <= ind.rsi_14 <= self._rsi_upper):
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"rsi {ind.rsi_14:.1f} outside [{self._rsi_lower}, {self._rsi_upper}]")
        if ind.macd <= ind.macd_signal:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"macd {ind.macd:.3f} not above signal {ind.macd_signal:.3f}")
        if ind.last_close <= ind.ema_20:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"close {ind.last_close:.2f} not above EMA20 {ind.ema_20:.2f}")
        if ind.return_5d <= 0:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"5d return {ind.return_5d:.4f} not positive")

        entry = Decimal(str(ind.last_close))
        ema_stop = Decimal(str(ind.ema_20))
        pct_stop = entry * (Decimal("1") - self._stop_pct)
        stop = max(ema_stop, pct_stop)
        per_share_risk = entry - stop
        if per_share_risk <= 0:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          "stop not below entry — anomaly")

        risk_budget = (equity * self._risk_pct / Decimal("100")).quantize(Decimal("0.01"))
        risk_qty = risk_budget / per_share_risk
        # Also cap by concentration: max position notional / entry price
        concentration_budget = (equity * self._max_concentration_pct / Decimal("100"))
        concentration_qty = concentration_budget / entry
        # Use whichever is smaller — both constraints must be respected
        raw_qty = min(risk_qty, concentration_qty)
        qty = raw_qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if qty < 1:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"calculated qty {raw_qty:.4f} < 1 share")

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            qty=qty,
            entry_price=entry,
            stop_loss_price=stop.quantize(Decimal("0.01")),
            reason=f"rsi={ind.rsi_14:.1f} macd>{ind.macd_signal:.3f} close>EMA20",
        )


class MeanReversionStrategy:
    """Phase 1 mean-reversion entry. Buys oversold names that have already
    started bouncing (RSI rising from below 30 toward 35). Active in
    sideways and risk_off regimes.
    """

    def __init__(
        self,
        rsi_lower: float = 25.0,
        rsi_upper: float = 35.0,
        per_trade_risk_pct: Decimal = Decimal("0.5"),
        stop_pct: Decimal = Decimal("0.04"),
        max_concentration_pct: Decimal = Decimal("4.5"),
    ) -> None:
        self._rsi_lower = rsi_lower
        self._rsi_upper = rsi_upper
        self._risk_pct = per_trade_risk_pct
        self._stop_pct = stop_pct
        self._max_concentration_pct = max_concentration_pct

    def evaluate(self, symbol: str, ind: Indicators, equity: Decimal) -> Signal:
        if not (self._rsi_lower <= ind.rsi_14 <= self._rsi_upper):
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"rsi {ind.rsi_14:.1f} outside MR window [{self._rsi_lower}, {self._rsi_upper}]")
        # Require price near or below EMA20 (oversold) but recovering
        if ind.last_close > ind.ema_20 * 1.01:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"close {ind.last_close:.2f} > 1% above EMA20 {ind.ema_20:.2f} — not oversold")
        # 5d return should be turning positive (just starting to bounce)
        if ind.return_5d < -0.05:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"5d return {ind.return_5d:.4f} still falling — wait for bounce")

        entry = Decimal(str(ind.last_close))
        stop = entry * (Decimal("1") - self._stop_pct)
        per_share_risk = entry - stop
        if per_share_risk <= 0:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          "stop math invalid")

        risk_budget = (equity * self._risk_pct / Decimal("100")).quantize(Decimal("0.01"))
        risk_qty = risk_budget / per_share_risk
        concentration_budget = (equity * self._max_concentration_pct / Decimal("100"))
        concentration_qty = concentration_budget / entry
        raw_qty = min(risk_qty, concentration_qty)
        qty = raw_qty.quantize(Decimal("1"), rounding=ROUND_DOWN)
        if qty < 1:
            return Signal(symbol, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"),
                          f"calculated qty {raw_qty:.4f} < 1 share")

        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            qty=qty,
            entry_price=entry,
            stop_loss_price=stop.quantize(Decimal("0.01")),
            reason=f"MR: rsi={ind.rsi_14:.1f} (oversold), close~EMA20",
        )


def strategy_for_regime(regime: str):
    """Strategy router: pick the right strategy for the current market regime."""
    if regime == "trending_up":
        return MomentumStrategy()
    if regime == "trending_down":
        # Don't trade aggressively in downtrends. Mean reversion only on deep oversold.
        return MeanReversionStrategy(rsi_lower=20.0, rsi_upper=30.0)
    if regime == "sideways":
        return MeanReversionStrategy()
    # risk_off
    return MeanReversionStrategy(rsi_lower=20.0, rsi_upper=28.0,
                                  per_trade_risk_pct=Decimal("0.25"))
