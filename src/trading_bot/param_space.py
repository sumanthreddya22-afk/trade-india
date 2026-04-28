"""Param search spaces per strategy template.

Tuple shape: (low, high, kind) where kind is "int" or "float". Optuna's
TPE sampler consumes this via suggest_int / suggest_float.

Phase 5's Strategy Architect role will populate this dict for new templates.
"""
from __future__ import annotations

PARAM_SPACE: dict[str, dict[str, tuple]] = {
    "momentum": {
        "rsi_lower": (50.0, 60.0, "float"),
        "rsi_upper": (65.0, 75.0, "float"),
        "ema_period": (15, 30, "int"),
        "stop_pct": (3.0, 7.0, "float"),
        "sentiment_floor": (-1.0, 0.0, "float"),
    },
}
