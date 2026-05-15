"""ETF Momentum v3 — daily cadence + policy-driven dynamic universe.

The signal logic is unchanged from v2 (signal.py is imported directly).
v3 swaps:
  * cadence from monthly to daily
  * universe from a hardcoded 10-ETF allowlist to
    ``policy/etf_universe_v1.json`` (structured filter)
"""
from trading_bot.strategies.etf_momentum_v3.runner import (
    STRATEGY_ID, evaluate_strategy, should_rebalance_today,
)

__all__ = ["STRATEGY_ID", "evaluate_strategy", "should_rebalance_today"]
