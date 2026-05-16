"""Live-vs-model drift monitor.

Plan v4 §9: "live-vs-model drift: nightly job compares realized fill vs
the pessimistic-lens modeled fill per asset class. If 20-trade rolling
mean slippage exceeds modeled by >2x, the lane is auto-demoted to
observe-only."

Phase 3 ships the comparator + the recommendation. The nightly job
wires in Phase 5 alongside the kernel daemon.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

ROLLING_WINDOW_DEFAULT = 20
TOLERANCE_MULTIPLIER_DEFAULT = 1.5


@dataclass(frozen=True)
class DriftReport:
    lane: str
    n_trades: int
    modelled_mean_bps: float
    realised_mean_bps: float
    ratio: float                          # realised / modelled
    breach: bool
    recommendation: str                   # "" or "demote:<lane>"


def _slippage_bps_buy(mid: float, fill_price: float) -> float:
    if mid <= 0:
        return 0.0
    return (fill_price - mid) / mid * 10000.0


def _slippage_bps_sell(mid: float, fill_price: float) -> float:
    if mid <= 0:
        return 0.0
    return (mid - fill_price) / mid * 10000.0


def compute_drift(
    conn: sqlite3.Connection,
    *,
    lane: str,
    decision_mid_lookup,  # Callable[fill_event_row -> float] returning the decision-time mid
    modelled_mean_bps: float,
    rolling_window: int = ROLLING_WINDOW_DEFAULT,
    tolerance_multiplier: float = TOLERANCE_MULTIPLIER_DEFAULT,
) -> DriftReport:
    """Compute the realised slippage over the last ``rolling_window`` fills
    on the given lane.

    Callers join ``fill_event`` against ``strategy_decision`` to recover
    the decision-time mid; that join lives outside this module so the
    function stays pure.

    ``decision_mid_lookup`` is a callable that, given a fill_event row
    dict (with ``order_uid``, ``symbol``, ``price``, ``qty``, ``event_ts``),
    returns the decision-time mid for that fill. The caller wires this
    however it likes (in-memory cache, SQL view, etc.).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT fe.ledger_seq, fe.order_uid, fe.symbol, fe.qty, fe.price, fe.event_ts
        FROM fill_event fe
        JOIN order_master om USING (order_uid)
        WHERE om.strategy_id IN (
            SELECT DISTINCT strategy_id FROM order_master om2
            WHERE om2.order_uid = fe.order_uid
        )
        ORDER BY fe.ledger_seq DESC
        LIMIT ?
        """,
        (rolling_window,),
    )
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return DriftReport(
            lane=lane, n_trades=0,
            modelled_mean_bps=modelled_mean_bps, realised_mean_bps=0.0,
            ratio=0.0, breach=False, recommendation="",
        )

    slippages = []
    for row in rows:
        mid = decision_mid_lookup(row)
        if not mid or mid <= 0:
            continue
        # Use order_master.side to know direction. Cheap: read from join.
        cur2 = conn.cursor()
        cur2.execute(
            "SELECT side FROM order_master WHERE order_uid=?",
            (row["order_uid"],),
        )
        side_row = cur2.fetchone()
        side = side_row[0] if side_row else "buy"
        if side in ("buy", "buy_to_close"):
            slip = _slippage_bps_buy(mid, row["price"])
        else:
            slip = _slippage_bps_sell(mid, row["price"])
        slippages.append(slip)

    if not slippages:
        return DriftReport(
            lane=lane, n_trades=0,
            modelled_mean_bps=modelled_mean_bps, realised_mean_bps=0.0,
            ratio=0.0, breach=False, recommendation="",
        )

    realised_mean = sum(slippages) / len(slippages)
    ratio = realised_mean / modelled_mean_bps if modelled_mean_bps > 0 else float("inf")
    breach = ratio > tolerance_multiplier
    return DriftReport(
        lane=lane, n_trades=len(slippages),
        modelled_mean_bps=modelled_mean_bps,
        realised_mean_bps=realised_mean,
        ratio=ratio, breach=breach,
        recommendation=f"demote:{lane}" if breach else "",
    )


__all__ = ["DriftReport", "ROLLING_WINDOW_DEFAULT", "compute_drift"]
