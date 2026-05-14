"""Strategy implementations.

Each strategy lives under its own subpackage and is registered in the
strategy_version table. Live-readable from `bot strategy list`.

A strategy must export:
  * ``signal_fn(history, decision_date) -> dict[symbol, float]`` —
    target weights in [0, 1]. Compatible with research.backtest.SignalT.
  * ``DEFAULT_PARAMS`` — frozen dict of operator-tunable knobs.
  * ``UNIVERSE`` — tuple of symbols the strategy operates on.
  * ``STRATEGY_ID`` — must match the registered row in
    ``strategy_version``.

The same ``signal_fn`` is consumed by both the backtest engine and the
live daemon's strategy_runner job, so behaviour is identical between
research and production by construction.
"""
from __future__ import annotations
