"""Crypto Momentum live runner.

Differences from the equity runners:
  * Asset class is ``crypto`` — used in OrderIntent + lane routing.
  * Position sleeve is capped at CRYPTO_GROSS_MAX_PCT of equity per
    risk_policy.lock["asset_class"]["crypto_gross_max_pct"].
  * Crypto trades 24/7 — no RTH gate.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, load_bars, open_store,
)
from trading_bot.risk import DEFAULT_POLICY_DIR
from trading_bot.strategies.crypto_momentum_v1.signal import (
    CRYPTO_GROSS_MAX_PCT, DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, signal_fn,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict[str, float]
    current_qty: dict[str, float]
    equity: float
    intents: list[dict]


def _crypto_cap_pct() -> float:
    """Read the crypto cap from risk_policy.lock, fall back to constant."""
    try:
        lock = json.loads(
            (DEFAULT_POLICY_DIR / "risk_policy.lock").read_text()
        )
        return float(lock.get("asset_class", {})
                          .get("crypto_gross_max_pct", CRYPTO_GROSS_MAX_PCT))
    except Exception:
        return CRYPTO_GROSS_MAX_PCT


def should_rebalance_today(today: dt.date, last_date: dt.date | None) -> bool:
    if last_date is None:
        return True
    return (today.year, today.month) != (last_date.year, last_date.month)


def evaluate_strategy(
    *,
    historical_db: Path = DEFAULT_HISTORICAL_PATH,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
) -> StrategyDecision:
    decision_date = decision_date or dt.date.today()
    if not historical_db.exists():
        return StrategyDecision(
            decision_date=decision_date, target_weights={},
            current_qty={}, equity=0.0, intents=[],
        )

    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    start = decision_date - dt.timedelta(days=lookback + 30)
    conn = open_store(historical_db)
    try:
        bars = load_bars(conn, symbols=UNIVERSE, start=start, end=decision_date)
    finally:
        conn.close()

    target_weights = signal_fn(bars, decision_date, params=params)

    current_qty: dict[str, float] = {}
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            # Crypto from Alpaca can come as 'BTCUSD' or 'BTC/USD'. Normalise.
            sym = p["symbol"]
            if "/" not in sym and len(sym) >= 6:
                # Try inserting slash before last 3 chars (USD)
                if sym.endswith("USD"):
                    sym = sym[:-3] + "/USD"
            current_qty[sym] = float(p.get("qty", 0))
    equity = 0.0
    if account_fetcher is not None:
        equity = float((account_fetcher() or {}).get("equity", 0.0))

    intents: list[dict] = []
    if not target_weights or equity <= 0:
        return StrategyDecision(
            decision_date=decision_date,
            target_weights=dict(target_weights),
            current_qty=current_qty, equity=equity, intents=[],
        )

    crypto_cap_pct = _crypto_cap_pct()
    crypto_sleeve_value = equity * crypto_cap_pct / 100.0

    close_by_sym: dict[str, float] = {}
    for sym, series in bars.items():
        relevant = [b for b in series if b.bar_date <= decision_date]
        if relevant:
            close_by_sym[sym] = relevant[-1].close

    for sym, w_sleeve in target_weights.items():
        close = close_by_sym.get(sym)
        if not close or close <= 0:
            continue
        target_value = crypto_sleeve_value * w_sleeve
        target_qty = target_value / close
        # Round to 6 decimals for crypto (Alpaca minimum for BTC).
        target_qty = round(target_qty, 6)
        diff = target_qty - current_qty.get(sym, 0.0)
        if abs(diff) < 1e-5:
            continue
        intents.append({
            "strategy_id": STRATEGY_ID, "strategy_ver": 1,
            "symbol": sym, "side": "buy" if diff > 0 else "sell",
            "qty": abs(diff), "intent_price": close,
            "asset_class": "crypto", "lane": "crypto_trend",
            "rationale": f"crypto-momentum: {sym} winner over 90d",
        })

    # Sell any held crypto in universe not in target.
    for sym, qty in current_qty.items():
        if sym in target_weights or qty <= 0 or sym not in UNIVERSE:
            continue
        close = close_by_sym.get(sym, 0.0)
        if close <= 0:
            continue
        intents.append({
            "strategy_id": STRATEGY_ID, "strategy_ver": 1,
            "symbol": sym, "side": "sell", "qty": qty,
            "intent_price": close, "asset_class": "crypto",
            "lane": "crypto_trend",
            "rationale": "crypto-momentum: rotate out",
        })

    return StrategyDecision(
        decision_date=decision_date,
        target_weights=dict(target_weights),
        current_qty=current_qty, equity=equity, intents=intents,
    )


__all__ = ["StrategyDecision", "evaluate_strategy", "should_rebalance_today"]
