"""Asset-class gross caps: equity ≤80%, crypto ≤15%, options BP ≤30%.

The crypto cap is the same one we unwound to in Phase 0 — Plan v4 §6
keeps it locked in. When breach is detected, new entries in that asset
class are rejected; existing exits are still allowed (handled by the
PDT-style exit allowance in ``precheck``).
"""
from __future__ import annotations

from typing import Sequence

from trading_bot.risk.limits import AssetClassLimits
from trading_bot.risk.types import AccountState, Position, RiskDecision


def _gross_by_class(positions: Sequence[Position]) -> dict[str, float]:
    out: dict[str, float] = {"equity": 0.0, "crypto": 0.0, "option": 0.0}
    for p in positions:
        ac = (p.asset_class or "").lower()
        if ac in out:
            out[ac] += abs(p.market_value)
        elif ac in ("us_equity",):
            out["equity"] += abs(p.market_value)
        elif ac in ("us_option",):
            out["option"] += abs(p.market_value)
    return out


def check_asset_class_caps(
    *,
    intent_asset_class: str,
    intent_notional: float,
    intent_side: str,                 # "buy"|"sell"|"sell_short"|"sell_to_close"|"buy_to_close"
    account: AccountState,
    positions: Sequence[Position],
    limits: AssetClassLimits,
) -> RiskDecision:
    """Single-entry asset-class gate.

    Exits ("sell" on a long, "buy_to_close" on a short, etc.) always
    pass — they REDUCE exposure. Only entries are subject to the cap.
    """
    ac = (intent_asset_class or "").lower()
    if ac in ("us_equity",):
        ac = "equity"
    if ac in ("us_option",):
        ac = "option"

    side = (intent_side or "").lower()
    is_exit = side in ("sell_to_close", "buy_to_close")
    # A plain "sell" of a long position is also an exit — we treat any
    # negative-delta order on a held position as an exit. The kernel's
    # job is to recognise exits; here we rely on the caller passing the
    # canonical side.

    gross = _gross_by_class(positions)
    equity = max(account.equity, 1.0)

    if ac == "equity":
        proj = gross["equity"] + (0.0 if is_exit else intent_notional)
        pct = proj / equity * 100.0
        if pct > limits.equity_gross_max_pct and not is_exit:
            return RiskDecision.halt(
                f"asset_class_cap:equity ({pct:.2f}% > "
                f"{limits.equity_gross_max_pct:.2f}%)"
            )
    elif ac == "crypto":
        proj = gross["crypto"] + (0.0 if is_exit else intent_notional)
        pct = proj / equity * 100.0
        if pct > limits.crypto_gross_max_pct and not is_exit:
            return RiskDecision.halt(
                f"asset_class_cap:crypto ({pct:.2f}% > "
                f"{limits.crypto_gross_max_pct:.2f}%)"
            )
    elif ac == "option":
        proj = gross["option"] + (0.0 if is_exit else intent_notional)
        pct = proj / equity * 100.0
        if pct > limits.options_buying_power_util_max_pct and not is_exit:
            return RiskDecision.halt(
                f"asset_class_cap:options ({pct:.2f}% > "
                f"{limits.options_buying_power_util_max_pct:.2f}%)"
            )

    return RiskDecision.accept()


__all__ = ["check_asset_class_caps"]
