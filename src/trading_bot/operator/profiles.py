"""Risk profiles — three preset overlays for ``policy/risk_policy.lock``.

A profile is a *partial* mapping: only the fields the profile changes
are listed. Applying a profile reads the current lock, deep-merges the
overlay, writes a new dated lock version, and regenerates
``policy/HASHES``.

The lock-version tag follows the existing convention:
``YYYY-MM-DD.profile-{name}``. The lock_version is read back at load
time so the operator can see "current profile" without spelunking JSON.

Loosening fields trigger the 7-day cooldown (Plan v4 §4 asymmetric
cooldown). Tightening is immediate.
"""
from __future__ import annotations

from typing import Mapping

# Sentinel "current values match the existing v4 phase-2 lock".
NEUTRAL_OVERLAY: Mapping = {
    "account": {
        "daily_drawdown_pct_of_equity": 1.0,
        "intraday_pnl_floor_pct_of_equity": -1.5,
    },
    "asset_class": {
        "equity_gross_max_pct": 80.0,
        "crypto_gross_max_pct": 15.0,
        "options_buying_power_util_max_pct": 30.0,
    },
    "order": {
        "per_order_at_risk_max_pct": 2.0,
    },
    "symbol": {
        "per_symbol_gross_max_pct": 5.0,
    },
}

# Tightening across the board — entering safe mode never triggers the
# loosen cooldown. Suitable for: vacation, post-incident, account
# concentration episodes, regime uncertainty.
SAFE_OVERLAY: Mapping = {
    "account": {
        "daily_drawdown_pct_of_equity": 0.5,
        "intraday_pnl_floor_pct_of_equity": -1.0,
    },
    "asset_class": {
        "equity_gross_max_pct": 60.0,
        "crypto_gross_max_pct": 10.0,
        "options_buying_power_util_max_pct": 15.0,
    },
    "order": {
        "per_order_at_risk_max_pct": 1.0,
    },
    "symbol": {
        "per_symbol_gross_max_pct": 3.0,
    },
}

# Loosening — triggers 7-day cooldown for any field that exceeds the
# current value. Suitable only after MVP-OP passes AND operator decides
# to accept additional risk.
AGGRESSIVE_OVERLAY: Mapping = {
    "account": {
        "daily_drawdown_pct_of_equity": 1.5,
        "intraday_pnl_floor_pct_of_equity": -2.0,
    },
    "asset_class": {
        "equity_gross_max_pct": 90.0,
        "crypto_gross_max_pct": 20.0,
        "options_buying_power_util_max_pct": 40.0,
    },
    "order": {
        "per_order_at_risk_max_pct": 3.0,
    },
    "symbol": {
        "per_symbol_gross_max_pct": 7.0,
    },
}

PROFILES: Mapping[str, Mapping] = {
    "safe": SAFE_OVERLAY,
    "neutral": NEUTRAL_OVERLAY,
    "aggressive": AGGRESSIVE_OVERLAY,
}


def is_loosening(field_path: str, old_value: float, new_value: float) -> bool:
    """Direction-of-change classifier.

    For caps (max_pct fields), higher = looser. For floors
    (intraday_pnl_floor_pct_of_equity), more-negative = looser. The
    helper centralises the asymmetry so callers don't get it wrong.
    """
    if "floor" in field_path:
        # Floor is negative; lower (more negative) = more permissive.
        return new_value < old_value
    return new_value > old_value


def diff_profile(
    current_lock: Mapping, target_overlay: Mapping,
) -> list[dict]:
    """Return a list of ``{path, old, new, direction}`` rows for the
    operator to preview before applying.
    """
    diffs: list[dict] = []

    def _walk(prefix: str, cur: Mapping, tgt: Mapping) -> None:
        for k, v in tgt.items():
            if k.startswith("_"):  # skip _comment / _note keys
                continue
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _walk(path, cur.get(k, {}) if isinstance(cur, Mapping) else {}, v)
            else:
                old = cur.get(k) if isinstance(cur, Mapping) else None
                if old is None or float(old) != float(v):
                    direction = (
                        "loosen" if (old is not None and is_loosening(path, float(old), float(v)))
                        else "tighten" if old is not None
                        else "set"
                    )
                    diffs.append({"path": path, "old": old, "new": v, "direction": direction})
    _walk("", current_lock, target_overlay)
    return diffs


__all__ = [
    "AGGRESSIVE_OVERLAY", "NEUTRAL_OVERLAY", "PROFILES", "SAFE_OVERLAY",
    "diff_profile", "is_loosening",
]
