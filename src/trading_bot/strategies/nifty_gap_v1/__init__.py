"""NIFTY Overnight Gap Fade v1 — India-specific intraday hypothesis.

**Mechanism.** On NSE, the only liquidity-discovery event between the
prior close (15:30 IST) and today's open (09:15 IST) is the pre-open
call auction (09:00-09:15). The auction price often overshoots vs the
day's eventual fair value because it aggregates overnight news from a
relatively thin participant set. The hypothesis: when NIFTYBEES gaps
by ≥ ``gap_threshold_pct`` from yesterday's close, the gap tends to
fade — i.e. today's close is closer to yesterday's close than today's
open is. Trade the fade: sell at open after a gap-up, buy after a
gap-down, exit at close.

**Why this might work in India specifically.**
  * NSE has no extended-hours trading, so overnight news (RBI/Fed
    statements, US close moves, Asia futures) accumulates and lands
    fully on the 09:00 auction. NYSE has 8h of after-hours trading
    that absorbs much of this.
  * Pre-open call auction is uncrossed with limited price-discovery —
    a small order book imbalance can produce a meaningful auction
    print.
  * Retail momentum-chasers (DIY app traders) are typically active
    around the open and quiet by midday, so the immediate post-open
    flow is plausibly mean-reverting.

**Why this might NOT work / known failure modes.**
  * Trend days — strong macro catalyst (budget, RBI surprise, US CPI)
    can produce sustained one-way moves that do not fade.
  * Expiry days — heavy F&O positioning (especially weekly BankNifty
    Thursday) distorts the cash NIFTY behaviour.
  * Microstructure noise — at low gap thresholds, the "fade" is
    smaller than the round-trip cost (STT both sides + stamp duty +
    GST + slippage). Need ≥ ~0.4% gap to clear costs.

**Status.** ``research_only``. Registered against a separate hypothesis
ID. Promotion to ``shadow`` requires a passing Tier-1 validation
artifact (walk-forward + deflated Sharpe + PBO + plateau coverage)
on at least 5 years of NSE daily OHLC. None of that is in this repo
yet — historical_bars.db is empty; Tier-1 cannot be run until a
data-ingest job populates it. This module exists to lock the
hypothesis in code and prevent silent drift.
"""
from __future__ import annotations

from trading_bot.strategies.nifty_gap_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, compute_gap_signal,
)

__all__ = [
    "DEFAULT_PARAMS", "STRATEGY_ID", "UNIVERSE", "compute_gap_signal",
]
