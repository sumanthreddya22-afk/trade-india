"""SPY Wheel v1 — cash-secured puts → assignment → covered calls.

The wheel is a defined-outcome options-income strategy:

  state: FLAT
    weekly: sell 1 SPY put ~30 DTE at ~0.30 delta
       on expiration:
         out-of-money  → keep premium, return to FLAT
         in-the-money  → assignment, move to LONG_STOCK

  state: LONG_STOCK
    weekly: sell 1 SPY call ~30 DTE at ~0.30 delta against the shares
       on expiration:
         out-of-money  → keep premium, sell another call next week
         in-the-money  → shares called away, return to FLAT

This is *not* a return-series strategy and can't be backtested through
the standard signal_fn → backtest engine. The wheel runner generates
one decision per week (the SPY-put-sell-event), and the backtest-lite
in ``wheel_backtest.py`` simulates these with Black-Scholes pricing
against a historical IV proxy.

Risk caps:
  * Per ``risk_policy.lock["asset_class"]["options_buying_power_util_max_pct"]``
    (currently 30%): total open option notional ≤ 30% of options BP.
  * One open contract at a time per strike. No stacking.
  * Per-order risk = put strike × 100 (assignment notional). If
    assigned, sleeve is wholly invested in SPY until covered call
    expires worthless or shares are called away.
"""
from __future__ import annotations

from trading_bot.strategies.spy_wheel_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNDERLYING, signal_fn,
)
from trading_bot.strategies.spy_wheel_v1.state_machine import (
    WheelState, advance_state, current_state,
)

__all__ = [
    "DEFAULT_PARAMS", "STRATEGY_ID", "UNDERLYING",
    "WheelState", "advance_state", "current_state", "signal_fn",
]
