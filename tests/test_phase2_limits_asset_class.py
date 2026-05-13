"""Phase 2 — asset-class caps (equity 80%, crypto 15%, options 30%)."""
from __future__ import annotations

from trading_bot.risk.asset_class_caps import check_asset_class_caps
from trading_bot.risk.limits import AssetClassLimits
from trading_bot.risk.types import AccountState, Position


L = AssetClassLimits(
    equity_gross_max_pct=80.0,
    crypto_gross_max_pct=15.0,
    options_buying_power_util_max_pct=30.0,
)

ACCT = AccountState(equity=10_000, cash=5000,
                    equity_at_session_start=10_000, day_trade_count=0)


def _pos(symbol, ac, mv):
    return Position(symbol=symbol, asset_class=ac, qty=1.0,
                    market_value=mv, classification="bot")


def test_crypto_entry_at_cap_blocked() -> None:
    # Already at 15% cap.
    d = check_asset_class_caps(
        intent_asset_class="crypto", intent_notional=200, intent_side="buy",
        account=ACCT,
        positions=[_pos("BTCUSD", "crypto", 1500)],
        limits=L,
    )
    assert d.verdict == "halt"
    assert "crypto" in d.reason


def test_crypto_entry_under_cap_passes() -> None:
    d = check_asset_class_caps(
        intent_asset_class="crypto", intent_notional=100, intent_side="buy",
        account=ACCT,
        positions=[_pos("BTCUSD", "crypto", 1000)],
        limits=L,
    )
    assert d.verdict == "accept"


def test_crypto_exit_always_passes() -> None:
    d = check_asset_class_caps(
        intent_asset_class="crypto", intent_notional=500, intent_side="sell_to_close",
        account=ACCT,
        positions=[_pos("BTCUSD", "crypto", 5000)],   # over cap
        limits=L,
    )
    assert d.verdict == "accept"


def test_equity_entry_at_cap_blocked() -> None:
    d = check_asset_class_caps(
        intent_asset_class="equity", intent_notional=1000, intent_side="buy",
        account=ACCT,
        positions=[_pos("SPY", "equity", 8000)],
        limits=L,
    )
    assert d.verdict == "halt"


def test_options_buying_power_blocked() -> None:
    d = check_asset_class_caps(
        intent_asset_class="option", intent_notional=500, intent_side="buy",
        account=ACCT,
        positions=[_pos("SPY_C", "option", 2800)],
        limits=L,
    )
    assert d.verdict == "halt"
    assert "options" in d.reason
