"""India / SEBI-specific risk checks.

Four checks defined by Indian market structure that the generic risk
kernel cannot express:

1. **Index circuit breakers** — SEBI mandates market-wide halts when
   NIFTY 50 or BSE Sensex moves ±10/15/20% from the previous close.
   The halt duration depends on the magnitude AND the time of day.
   Source: SEBI circular SEBI/HO/MRD/DP/CIR/P/2020/95 (and updates).

2. **F&O ban-list** — NSE publishes a daily list of scrips that have
   crossed 95% of the market-wide position limit (MWPL). On those
   scrips, ONLY position-reduction trades (sell-to-close, buy-to-close)
   are allowed; opening new positions is barred until the script is
   removed from the ban list.

3. **Per-stock circuit limits** — every NSE scrip carries an upper +
   lower circuit price band (typically ±2%, ±5%, ±10%, or ±20% of
   previous close). Orders at or beyond the band are rejected by NSE;
   the kernel should reject them upstream to avoid an api_error_rate
   spike.

4. **F&O exposure-margin check** — this is NOT a SPAN calculator. It is
   a conservative pre-trade guard: rejects an F&O order if the premium
   notional × lot × contracts × buffer exceeds available cash margin.
   Treat as a lower bound; the real SPAN+exposure margin computed by
   Zerodha on order receipt may be tighter.

All four functions are pure: they take their inputs and return a
``RiskDecision``. State (ledger, daily ban list, price bands) is
fetched by callers and injected. The functions stay testable.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional

from trading_bot.risk.types import RiskDecision

# NSE F&O symbol format: <UNDERLYING><YY><MMM><STRIKE><CE|PE> for
# options, <UNDERLYING><YY><MMM>FUT for futures. Examples:
#   TATASTEEL26JUN1000CE → TATASTEEL
#   NIFTY26JUN24000PE    → NIFTY
#   RELIANCE26JULFUT     → RELIANCE
# The regex captures the alpha prefix up to the year-month-suffix.
_FNO_UNDERLYING_RE = re.compile(
    r"^([A-Z&-]+?)\d{2}[A-Z]{3}",
)

# ---------------------------------------------------------------------------
# Constants — SEBI index circuit-breaker tiers
# ---------------------------------------------------------------------------

# SEBI tiers: (drop_pct_floor, time_cutoff_ist, halt_minutes).
# A drop ≥ floor triggers the longest halt whose time_cutoff has not
# yet passed; "session_close" means the market shuts for the day.
#
# Reference: SEBI 2020 circular (current as of 2026-06).
_SESSION_CLOSE = -1

_NIFTY_BREAKER_RULES: tuple[tuple[float, dt.time, int], ...] = (
    # 20% at any time = close session
    (20.0, dt.time(15, 30), _SESSION_CLOSE),
    # 15% before 13:00 → 1h 45m; before 14:00 → 45m; after → close
    (15.0, dt.time(13,  0), 105),
    (15.0, dt.time(14,  0),  45),
    (15.0, dt.time(15, 30), _SESSION_CLOSE),
    # 10% before 13:00 → 45m; before 14:30 → 15m; after → no halt
    (10.0, dt.time(13,  0),  45),
    (10.0, dt.time(14, 30),  15),
    (10.0, dt.time(15, 30),   0),
)


@dataclass(frozen=True)
class IndiaSebiContext:
    """Optional bundle of India/SEBI inputs for the risk precheck.

    All fields default to None / empty → the corresponding check is
    skipped. Callers populate only the fields they have data for.
    """
    nifty_drop_pct: Optional[float] = None
    """Current NIFTY 50 move from previous close, in percent. Negative
    = market down. Source: live tick or last index print."""

    now_ist: Optional[dt.datetime] = None
    """Wall-clock time in IST (Asia/Kolkata). Used to compute halt
    durations and to evaluate whether the post-cutoff "no halt" rule
    applies. Defaults to the current wall clock when None."""

    fno_ban_list: FrozenSet[str] = frozenset()
    """Set of scrip symbols currently on NSE's daily F&O ban list.
    Empty = no scrips banned (or list not loaded → check skipped)."""

    price_band_lookup: Optional[Callable[[str], Optional[tuple[float, float]]]] = None
    """Callback ``symbol -> (lower_band, upper_band)`` in INR. Return
    None for symbols without bands (ETFs, F&O, crypto). When the
    callback itself is None, the per-stock circuit check is skipped."""

    available_margin_inr: Optional[float] = None
    """Cash margin available for new F&O positions, in INR. When None,
    the F&O margin check is skipped (caller doesn't have margin data
    yet — e.g. dry-run / backtest)."""

    fno_margin_buffer: float = 1.10
    """Multiplier applied to premium notional to estimate the required
    margin. 1.10 = +10% cushion above raw premium. NOT a SPAN
    calculation; conservative pre-trade guard only."""


# ---------------------------------------------------------------------------
# 1. Index circuit breaker
# ---------------------------------------------------------------------------

def check_nifty_circuit_breaker(
    *, drop_pct: float, now_ist: dt.datetime,
) -> RiskDecision:
    """Apply SEBI's tiered NIFTY 50 circuit-breaker rule.

    ``drop_pct`` is the absolute magnitude of the move from previous
    close (negative input is treated as a drop; positive is treated as
    no breaker since SEBI's tiered rule is triggered on declines).

    Returns ``halt`` when an active halt window covers ``now_ist``;
    ``accept`` otherwise. The function is stateless — callers that
    need to record the halt should also fire a kill_switch event.
    """
    mag = abs(drop_pct) if drop_pct < 0 else 0.0
    if mag <= 0:
        return RiskDecision.accept()

    t = now_ist.time()
    for floor, cutoff, halt_minutes in _NIFTY_BREAKER_RULES:
        if mag >= floor and t < cutoff:
            if halt_minutes == _SESSION_CLOSE:
                return RiskDecision.halt(
                    f"nifty_circuit:tier_{int(floor)}pct:session_close"
                )
            if halt_minutes == 0:
                return RiskDecision.accept()
            return RiskDecision.halt(
                f"nifty_circuit:tier_{int(floor)}pct:halt_{halt_minutes}min"
            )
    return RiskDecision.accept()


# ---------------------------------------------------------------------------
# 2. F&O ban-list
# ---------------------------------------------------------------------------

# Sides that OPEN new exposure (vs. close existing). Only opening sides
# are blocked when a scrip is on the ban list; closing trades are
# explicitly permitted because they reduce the bank-wide MWPL pressure
# the ban list is trying to relieve.
_OPENING_SIDES = frozenset({"buy", "sell", "sell_short"})


def check_fno_ban_list(
    *, intent_symbol: str, intent_side: str, ban_list: FrozenSet[str],
) -> RiskDecision:
    """Reject opening positions on scrips in NSE's daily F&O ban list.

    Closing trades (``sell_to_close``, ``buy_to_close``) pass through —
    the ban list exists to STOP MWPL growth, not to trap holders."""
    if not ban_list:
        return RiskDecision.accept()
    sym_root = (intent_symbol or "").upper().split(":")[-1]
    # Strip option/future suffix → ban applies to the underlying.
    m = _FNO_UNDERLYING_RE.match(sym_root)
    underlying = m.group(1) if m else sym_root
    if underlying not in ban_list:
        return RiskDecision.accept()
    if (intent_side or "").lower() in _OPENING_SIDES:
        return RiskDecision.halt(
            f"fno_ban_list:{underlying}:opening_blocked "
            f"(closing trades still permitted)"
        )
    return RiskDecision.accept()


# ---------------------------------------------------------------------------
# 3. Per-stock circuit (upper / lower price band)
# ---------------------------------------------------------------------------

def check_per_stock_circuit(
    *,
    intent_symbol: str,
    intent_price: float,
    intent_side: str,
    band_lookup: Callable[[str], Optional[tuple[float, float]]],
) -> RiskDecision:
    """Reject orders at or beyond the per-scrip circuit price band.

    ``band_lookup(symbol) -> (lower, upper)`` returns None for symbols
    without bands (ETFs that track no underlying, F&O, crypto). Buys
    at or above the upper band are blocked; sells at or below the
    lower band are blocked. The opposite direction is permitted since
    a contra trade helps the circuit relax."""
    bands = band_lookup(intent_symbol)
    if bands is None:
        return RiskDecision.accept()
    lower, upper = bands
    side = (intent_side or "").lower()
    is_buy_like = side in ("buy", "buy_to_close")
    if is_buy_like and intent_price >= upper:
        return RiskDecision.halt(
            f"per_stock_circuit:upper:{intent_symbol}:"
            f"price={intent_price:.2f}>=upper={upper:.2f}"
        )
    if (not is_buy_like) and intent_price <= lower:
        return RiskDecision.halt(
            f"per_stock_circuit:lower:{intent_symbol}:"
            f"price={intent_price:.2f}<=lower={lower:.2f}"
        )
    return RiskDecision.accept()


# ---------------------------------------------------------------------------
# 4. F&O margin guard (conservative — not SPAN)
# ---------------------------------------------------------------------------

def check_fno_margin_available(
    *,
    premium_inr: float,
    lot_size: int,
    contracts: int,
    available_margin_inr: float,
    buffer_multiplier: float = 1.10,
) -> RiskDecision:
    """Reject F&O orders whose conservative margin estimate exceeds
    ``available_margin_inr``.

    Estimate = premium × lot_size × contracts × buffer. This is NOT
    SPAN — Zerodha computes real SPAN + exposure margin at the order
    gateway. The buffer (default +10%) gives a cushion so we don't
    submit orders that will be insta-rejected on margin.

    Only meaningful for option BUYS (where premium is paid up front).
    For shorts / writes, SPAN dominates and a separate adapter call
    is required; this function returns accept() so the kernel doesn't
    falsely block writes. The caller decides whether to skip."""
    if premium_inr <= 0 or lot_size <= 0 or contracts <= 0:
        return RiskDecision.accept()
    required = premium_inr * lot_size * contracts * buffer_multiplier
    if required > available_margin_inr:
        return RiskDecision.halt(
            f"fno_margin:required_inr={required:.2f}>"
            f"available_inr={available_margin_inr:.2f}"
            f" (buffer={buffer_multiplier:.2f}x)"
        )
    return RiskDecision.accept()


__all__ = [
    "IndiaSebiContext",
    "check_fno_ban_list",
    "check_fno_margin_available",
    "check_nifty_circuit_breaker",
    "check_per_stock_circuit",
]
