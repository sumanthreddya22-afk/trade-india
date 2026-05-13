"""v4 execution kernel.

Plan v4 §3 + §9. Phase 3 ships:

- cost_model (three reporting lenses)
- order_router (risk-gated, freshness-checked, idempotent submit)
- drift_monitor (20-trade live-vs-model)
- orphan_loop (Phase 1 helper wired into a recurring job shape)
"""
from __future__ import annotations

from trading_bot.execution.cost_model import (
    FillCost, LensT, SideT, apply_lens, crypto_fill, options_fill, stocks_fill,
)
from trading_bot.execution.drift_monitor import (
    DriftReport, ROLLING_WINDOW_DEFAULT, compute_drift,
)
from trading_bot.execution.order_router import (
    BrokerSubmitT, SubmissionResult, submit_order,
)
from trading_bot.execution import orphan_loop  # noqa: F401

__all__ = [
    "BrokerSubmitT",
    "DriftReport",
    "FillCost",
    "LensT",
    "ROLLING_WINDOW_DEFAULT",
    "SideT",
    "SubmissionResult",
    "apply_lens",
    "compute_drift",
    "crypto_fill",
    "options_fill",
    "orphan_loop",
    "stocks_fill",
    "submit_order",
]
