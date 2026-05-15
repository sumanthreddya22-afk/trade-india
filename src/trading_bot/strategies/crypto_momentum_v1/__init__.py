"""Crypto Momentum v1 — BTC/ETH top-1 by 90-day return.

Mechanism: at each monthly rebalance, compare the 90-day total return
of BTC/USD and ETH/USD; hold the winner at 100% of the crypto sleeve.
Capped at 15% of total equity per the risk_policy.lock crypto cap.

Why monthly + 90-day: shorter lookbacks chase noise; longer lookbacks
miss regime changes. 90d / monthly is the smallest decision cadence
that has empirical evidence of working without overtrading.
"""
from __future__ import annotations

from trading_bot.strategies.crypto_momentum_v1.signal import (
    CRYPTO_GROSS_MAX_PCT, DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, signal_fn,
)

__all__ = [
    "CRYPTO_GROSS_MAX_PCT", "DEFAULT_PARAMS",
    "STRATEGY_ID", "UNIVERSE", "signal_fn",
]
