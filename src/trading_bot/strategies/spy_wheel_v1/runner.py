"""Wheel runner — picks today's option to sell.

State-aware decision:
  * **FLAT**            → sell a 30-DTE 0.30-delta SPY put (open new wheel)
  * **SHORT_PUT_OPEN**  → wait until expiry; no new orders
  * **LONG_STOCK**      → sell a 30-DTE 0.30-delta SPY call (covered)
  * **SHORT_CALL_OPEN** → wait until expiry

Cadence: weekly. Daemon job ticks daily; ``should_rebalance_today``
returns True only on Mondays (US-Eastern) so we have a clean Friday
expiry candidate.

The runner uses ``yfinance`` (via ``data_router.fetch_option_chain``)
to pull the live chain, ``black_scholes.bs_delta`` to find the target
strike, and returns one ``OptionOrderIntent`` per tick.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from trading_bot.ingest.data_router import (
    fetch_option_chain, list_option_expirations,
)
from trading_bot.ingest.yfinance_adapter import find_contract_by_delta
from trading_bot.strategies.spy_wheel_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNDERLYING,
    WheelSignal, occ_ticker, pick_expiry,
)
from trading_bot.strategies.spy_wheel_v1.state_machine import (
    WheelState, current_state, snapshot_positions,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WheelDecision:
    decision_date: dt.date
    state: WheelState
    signal: WheelSignal
    equity: float
    intents: list[dict]    # OrderIntent-shaped


def should_rebalance_today(
    today: dt.date, last_decision_date: Optional[dt.date],
) -> bool:
    """Weekly cadence — only act on Mondays in operator's tz.

    If today is Monday and we haven't acted this week, sell. The
    daemon ticks daily; this guard prevents stacking trades.
    """
    if today.weekday() != 0:    # 0 = Monday
        return False
    if last_decision_date is None:
        return True
    # Same Monday already actioned? — bail.
    return today != last_decision_date


def _options_buying_power(account_fetcher: Callable[[], dict]) -> float:
    try:
        acct = account_fetcher() or {}
    except Exception:
        return 0.0
    return float(acct.get("options_buying_power", 0.0) or 0.0)


def evaluate_strategy(
    *,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
) -> WheelDecision:
    """Produce the wheel's decision for ``decision_date``.

    Pure-ish: reads broker state but never submits. The dispatch loop
    submits the resulting intents.
    """
    decision_date = decision_date or dt.date.today()
    positions = (positions_fetcher() or []) if positions_fetcher else []
    state = current_state(positions)
    snap = snapshot_positions(positions)

    options_bp = (
        _options_buying_power(account_fetcher) if account_fetcher else 0.0
    )
    equity = 0.0
    if account_fetcher is not None:
        try:
            equity = float((account_fetcher() or {}).get("equity", 0.0))
        except Exception:
            equity = 0.0

    # Default: no action.
    null_signal = WheelSignal(
        decision_date=decision_date, state=state.value,
        underlying=UNDERLYING, underlying_price=0.0,
        side="none", action="wait", contract_symbol=None,
        strike=None, expiry=None, delta_estimate=None,
        mid_price=None, contracts=0,
        rationale=f"state={state.value}; waiting",
    )

    side = (
        "put" if state == WheelState.FLAT
        else "call" if state == WheelState.LONG_STOCK
        else None
    )
    if side is None:
        return WheelDecision(
            decision_date=decision_date, state=state, signal=null_signal,
            equity=equity, intents=[],
        )

    # Pick the expiry chain
    expiries = list_option_expirations(UNDERLYING)
    expiry = pick_expiry(
        expiries, today=decision_date,
        target_days=int(params["dte_target_days"]),
        min_days=int(params["dte_min_days"]),
        max_days=int(params["dte_max_days"]),
    )
    if expiry is None:
        return WheelDecision(
            decision_date=decision_date, state=state,
            signal=null_signal._replace(rationale="no expiry in DTE window")
            if hasattr(null_signal, "_replace") else null_signal,
            equity=equity, intents=[],
        )

    chain = fetch_option_chain(UNDERLYING, expiry)
    if chain is None or chain.underlying_price <= 0:
        return WheelDecision(
            decision_date=decision_date, state=state, signal=null_signal,
            equity=equity, intents=[],
        )

    target_delta = float(params["target_delta"])
    contract = find_contract_by_delta(
        chain, side=side, target_delta=target_delta,
        risk_free_rate=float(params["risk_free_rate"]),
    )
    if contract is None:
        return WheelDecision(
            decision_date=decision_date, state=state, signal=null_signal,
            equity=equity, intents=[],
        )

    # Contract qty
    if side == "call":
        # We cover existing shares — at most floor(shares/100) contracts.
        max_qty = int(snap.spy_shares // 100)
    else:
        # Cash-secured: notional per contract = strike × 100.
        notional_per = contract.strike * 100.0
        max_qty = int(options_bp // notional_per) if notional_per > 0 else 0
    qty = max(0, min(max_qty, int(params["max_contracts_per_week"])))

    if qty <= 0:
        return WheelDecision(
            decision_date=decision_date, state=state,
            signal=WheelSignal(
                decision_date=decision_date, state=state.value,
                underlying=UNDERLYING, underlying_price=chain.underlying_price,
                side=side, action="wait",
                contract_symbol=occ_ticker(UNDERLYING, expiry, side, contract.strike),
                strike=contract.strike, expiry=expiry,
                delta_estimate=target_delta, mid_price=contract.mid,
                contracts=0,
                rationale=f"qty=0 (options_bp=${options_bp:.0f}, "
                          f"strike={contract.strike}); skip this week",
            ),
            equity=equity, intents=[],
        )

    occ = occ_ticker(UNDERLYING, expiry, side, contract.strike)
    sig = WheelSignal(
        decision_date=decision_date, state=state.value,
        underlying=UNDERLYING, underlying_price=chain.underlying_price,
        side=side, action="sell_to_open",
        contract_symbol=occ, strike=contract.strike, expiry=expiry,
        delta_estimate=target_delta, mid_price=contract.mid,
        contracts=qty,
        rationale=(
            f"state={state.value}: sell {qty} {side} "
            f"@ {contract.strike:.0f} exp {expiry.isoformat()} "
            f"(target Δ={target_delta:.2f}, mid=${contract.mid:.2f})"
        ),
    )

    # OrderIntent-shaped dict for the dispatch loop
    intent = {
        "strategy_id": STRATEGY_ID, "strategy_ver": 1,
        "symbol": occ,
        # "sell" + asset_class=option_us → Alpaca treats as sell-to-open
        # because we don't currently hold a long position in this contract.
        "side": "sell",
        "qty": float(qty),
        "intent_price": contract.mid if contract.mid > 0 else contract.last_price,
        "asset_class": "us_option",
        "lane": "options_income_wheel",
        "rationale": sig.rationale,
        # Wheel-specific metadata
        "_wheel_state": state.value,
        "_wheel_strike": contract.strike,
        "_wheel_expiry": expiry.isoformat(),
    }
    return WheelDecision(
        decision_date=decision_date, state=state, signal=sig,
        equity=equity, intents=[intent],
    )


__all__ = [
    "WheelDecision", "evaluate_strategy", "should_rebalance_today",
]
