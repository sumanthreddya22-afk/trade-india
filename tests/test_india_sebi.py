"""India / SEBI risk checks — circuit breakers, ban list, price bands, margin.

Covers the four functions in ``trading_bot.risk.india_sebi``. The
precheck integration is exercised indirectly: a separate test wires
``IndiaSebiContext`` through ``precheck.evaluate`` and confirms the
sub-checks halt the order.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from trading_bot.risk.india_sebi import (
    IndiaSebiContext,
    check_fno_ban_list,
    check_fno_margin_available,
    check_nifty_circuit_breaker,
    check_per_stock_circuit,
)

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# 1. Index circuit breaker
# ---------------------------------------------------------------------------


def _ist(h: int, m: int) -> dt.datetime:
    """Build a 2026-06-03 IST datetime at (h, m)."""
    return dt.datetime(2026, 6, 3, h, m, tzinfo=IST)


def test_no_breaker_when_market_is_up() -> None:
    d = check_nifty_circuit_breaker(drop_pct=+5.0, now_ist=_ist(10, 0))
    assert d.verdict == "accept"


def test_no_breaker_below_10pct_threshold() -> None:
    d = check_nifty_circuit_breaker(drop_pct=-9.99, now_ist=_ist(10, 0))
    assert d.verdict == "accept"


def test_10pct_drop_before_13_halts_45min() -> None:
    d = check_nifty_circuit_breaker(drop_pct=-10.5, now_ist=_ist(11, 30))
    assert d.verdict == "halt"
    assert "tier_10pct" in d.reason
    assert "halt_45min" in d.reason


def test_10pct_drop_between_13_and_1430_halts_15min() -> None:
    d = check_nifty_circuit_breaker(drop_pct=-10.5, now_ist=_ist(13, 30))
    assert d.verdict == "halt"
    assert "halt_15min" in d.reason


def test_10pct_drop_after_1430_does_not_halt() -> None:
    """SEBI: a 10% breach after 14:30 IST does not stop trading
    (insufficient time remaining for a meaningful halt)."""
    d = check_nifty_circuit_breaker(drop_pct=-10.5, now_ist=_ist(14, 45))
    assert d.verdict == "accept"


def test_15pct_drop_before_13_halts_105min() -> None:
    d = check_nifty_circuit_breaker(drop_pct=-15.5, now_ist=_ist(11, 0))
    assert d.verdict == "halt"
    assert "tier_15pct" in d.reason
    assert "halt_105min" in d.reason


def test_15pct_drop_between_13_and_14_halts_45min() -> None:
    d = check_nifty_circuit_breaker(drop_pct=-15.0, now_ist=_ist(13, 30))
    assert d.verdict == "halt"
    assert "tier_15pct" in d.reason
    assert "halt_45min" in d.reason


def test_15pct_drop_after_14_closes_session() -> None:
    d = check_nifty_circuit_breaker(drop_pct=-15.5, now_ist=_ist(14, 30))
    assert d.verdict == "halt"
    assert "session_close" in d.reason


def test_20pct_drop_any_time_closes_session() -> None:
    for hour in (9, 12, 15):
        d = check_nifty_circuit_breaker(
            drop_pct=-20.0, now_ist=_ist(hour, 16),
        )
        assert d.verdict == "halt"
        assert "tier_20pct" in d.reason
        assert "session_close" in d.reason


# ---------------------------------------------------------------------------
# 2. F&O ban list
# ---------------------------------------------------------------------------


def test_no_ban_when_list_empty() -> None:
    d = check_fno_ban_list(
        intent_symbol="TATASTEEL", intent_side="buy",
        ban_list=frozenset(),
    )
    assert d.verdict == "accept"


def test_ban_blocks_opening_buy() -> None:
    d = check_fno_ban_list(
        intent_symbol="TATASTEEL", intent_side="buy",
        ban_list=frozenset({"TATASTEEL"}),
    )
    assert d.verdict == "halt"
    assert "TATASTEEL" in d.reason
    assert "opening_blocked" in d.reason


def test_ban_blocks_opening_short() -> None:
    d = check_fno_ban_list(
        intent_symbol="TATASTEEL", intent_side="sell_short",
        ban_list=frozenset({"TATASTEEL"}),
    )
    assert d.verdict == "halt"


def test_ban_permits_closing_trade() -> None:
    """Closing trades reduce MWPL pressure and must remain allowed."""
    for closing_side in ("sell_to_close", "buy_to_close"):
        d = check_fno_ban_list(
            intent_symbol="TATASTEEL", intent_side=closing_side,
            ban_list=frozenset({"TATASTEEL"}),
        )
        assert d.verdict == "accept", f"side={closing_side!r} should pass"


def test_ban_strips_option_suffix_from_underlying() -> None:
    """An option symbol like ``TATASTEEL26JUN1000CE`` must trip the
    ban on the ``TATASTEEL`` underlying."""
    d = check_fno_ban_list(
        intent_symbol="TATASTEEL26JUN1000CE", intent_side="buy",
        ban_list=frozenset({"TATASTEEL"}),
    )
    assert d.verdict == "halt"


def test_ban_does_not_match_unrelated_symbol() -> None:
    d = check_fno_ban_list(
        intent_symbol="RELIANCE", intent_side="buy",
        ban_list=frozenset({"TATASTEEL"}),
    )
    assert d.verdict == "accept"


# ---------------------------------------------------------------------------
# 3. Per-stock circuit (price band)
# ---------------------------------------------------------------------------


def _band_lookup_for(symbol: str, lower: float, upper: float):
    """Return a band_lookup callback that yields ``(lower, upper)`` for
    ``symbol`` and None for everything else."""
    def _lookup(sym):
        return (lower, upper) if sym == symbol else None
    return _lookup


def test_circuit_no_band_for_symbol_passes() -> None:
    d = check_per_stock_circuit(
        intent_symbol="NIFTYBEES", intent_price=200.0, intent_side="buy",
        band_lookup=lambda _s: None,
    )
    assert d.verdict == "accept"


def test_buy_below_upper_passes() -> None:
    d = check_per_stock_circuit(
        intent_symbol="RELIANCE", intent_price=2900.0, intent_side="buy",
        band_lookup=_band_lookup_for("RELIANCE", 2700.0, 3000.0),
    )
    assert d.verdict == "accept"


def test_buy_at_upper_halts() -> None:
    d = check_per_stock_circuit(
        intent_symbol="RELIANCE", intent_price=3000.0, intent_side="buy",
        band_lookup=_band_lookup_for("RELIANCE", 2700.0, 3000.0),
    )
    assert d.verdict == "halt"
    assert "upper" in d.reason


def test_buy_above_upper_halts() -> None:
    d = check_per_stock_circuit(
        intent_symbol="RELIANCE", intent_price=3050.0, intent_side="buy",
        band_lookup=_band_lookup_for("RELIANCE", 2700.0, 3000.0),
    )
    assert d.verdict == "halt"


def test_sell_at_lower_halts() -> None:
    d = check_per_stock_circuit(
        intent_symbol="RELIANCE", intent_price=2700.0, intent_side="sell",
        band_lookup=_band_lookup_for("RELIANCE", 2700.0, 3000.0),
    )
    assert d.verdict == "halt"
    assert "lower" in d.reason


def test_sell_above_lower_passes() -> None:
    d = check_per_stock_circuit(
        intent_symbol="RELIANCE", intent_price=2750.0, intent_side="sell",
        band_lookup=_band_lookup_for("RELIANCE", 2700.0, 3000.0),
    )
    assert d.verdict == "accept"


def test_sell_at_upper_passes() -> None:
    """A sell at the upper band relieves pressure on the upside
    circuit — must not be blocked."""
    d = check_per_stock_circuit(
        intent_symbol="RELIANCE", intent_price=3000.0, intent_side="sell",
        band_lookup=_band_lookup_for("RELIANCE", 2700.0, 3000.0),
    )
    assert d.verdict == "accept"


# ---------------------------------------------------------------------------
# 4. F&O margin guard
# ---------------------------------------------------------------------------


def test_margin_sufficient_for_one_nifty_lot() -> None:
    # NIFTY 1-lot CE @ premium ₹100, lot_size 50, contracts 1.
    # required = 100 * 50 * 1 * 1.10 = 5500
    d = check_fno_margin_available(
        premium_inr=100.0, lot_size=50, contracts=1,
        available_margin_inr=10_000.0,
    )
    assert d.verdict == "accept"


def test_margin_blocks_when_insufficient() -> None:
    # 100 * 50 * 1 * 1.10 = 5500 required, only 5000 available.
    d = check_fno_margin_available(
        premium_inr=100.0, lot_size=50, contracts=1,
        available_margin_inr=5_000.0,
    )
    assert d.verdict == "halt"
    assert "fno_margin" in d.reason
    assert "required" in d.reason


def test_margin_buffer_applied_correctly() -> None:
    """Without the 10% buffer 100*50=5000 would just pass; with the
    buffer it bumps to 5500 and trips the 5200 budget."""
    d = check_fno_margin_available(
        premium_inr=100.0, lot_size=50, contracts=1,
        available_margin_inr=5_200.0, buffer_multiplier=1.10,
    )
    assert d.verdict == "halt"

    d_no_buffer = check_fno_margin_available(
        premium_inr=100.0, lot_size=50, contracts=1,
        available_margin_inr=5_200.0, buffer_multiplier=1.0,
    )
    assert d_no_buffer.verdict == "accept"


def test_margin_zero_premium_short_circuits_to_accept() -> None:
    """An option sold for ₹0 premium has no buyer-side margin cost;
    upstream short-margin SPAN handles writes."""
    d = check_fno_margin_available(
        premium_inr=0.0, lot_size=50, contracts=1,
        available_margin_inr=10.0,
    )
    assert d.verdict == "accept"


def test_margin_zero_contracts_short_circuits_to_accept() -> None:
    d = check_fno_margin_available(
        premium_inr=100.0, lot_size=50, contracts=0,
        available_margin_inr=10.0,
    )
    assert d.verdict == "accept"


# ---------------------------------------------------------------------------
# 5. IndiaSebiContext defaults are all "skip"
# ---------------------------------------------------------------------------


def test_default_context_skips_every_check() -> None:
    ctx = IndiaSebiContext()
    assert ctx.nifty_drop_pct is None
    assert ctx.fno_ban_list == frozenset()
    assert ctx.price_band_lookup is None
    assert ctx.available_margin_inr is None
    assert ctx.fno_margin_buffer == 1.10
