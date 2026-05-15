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
from trading_bot.ingest.universe import (
    AssetFetcher, DiscoveryUnavailable, TopByVolume,
    UniverseResolution, enrich_with_volume, resolve_universe,
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


# Wheel is structurally single-underlying — the state machine tracks
# share lots, short puts/calls against ONE underlying. The discovery
# rule still resolves the underlying through the standard pipeline so
# the feature_snapshot captures it as the reproducibility anchor.
# Expanding this allowlist requires refactoring the state machine
# (it currently keys on ``spy_shares``) and is gated by a new
# strategy_version + validation packet.
_WHEEL_UNDERLYING_ALLOWLIST: tuple[str, ...] = (UNDERLYING,)

DISCOVERY_RULE = TopByVolume(
    asset_class="us_equity",
    top_n=1,
    required_attributes=("ETF",),
    symbol_allowlist=_WHEEL_UNDERLYING_ALLOWLIST,
    name="spy_wheel_v1.underlying",
)


@dataclass(frozen=True)
class WheelDecision:
    decision_date: dt.date
    state: WheelState
    signal: WheelSignal
    equity: float
    intents: list[dict]    # OrderIntent-shaped
    universe: tuple[str, ...] = ()
    universe_payload: dict = None


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


def _resolve_underlying_with_fallback(
    *,
    asset_fetcher: Optional[AssetFetcher],
    decision_date: dt.date,
    volume_provider: Optional[Callable[[str], float | None]] = None,
) -> tuple[str, dict]:
    """Return ``(underlying_symbol, universe_payload)``.

    The wheel's allowlist is a single symbol today; discovery is still
    invoked so the snapshot anchor stays consistent across strategies.
    Falls back to the static UNDERLYING when no fetcher is wired
    (unit tests / backtest replay).
    """
    static_payload = {
        "rule_name": DISCOVERY_RULE.name,
        "rule_hash": "fallback:static",
        "decision_date": decision_date.isoformat(),
        "symbols": list(_WHEEL_UNDERLYING_ALLOWLIST),
    }
    if asset_fetcher is None:
        return UNDERLYING, {
            **static_payload,
            "_fallback_reason": "no asset_fetcher injected",
        }
    fetcher: AssetFetcher = asset_fetcher
    if volume_provider is not None:
        base_fetcher = asset_fetcher

        def _enriched(asset_class: str):
            return enrich_with_volume(base_fetcher(asset_class), volume_provider)

        fetcher = _enriched
    try:
        res: UniverseResolution = resolve_universe(
            DISCOVERY_RULE, asset_fetcher=fetcher,
            decision_date=decision_date, asset_classes=("us_equity",),
        )
    except DiscoveryUnavailable as e:
        log.warning(
            "spy_wheel_v1: discovery unavailable (%s); falling back "
            "to static underlying %s", e, UNDERLYING,
        )
        return UNDERLYING, {
            **static_payload,
            "rule_hash": "fallback:discovery_unavailable",
            "_fallback_reason": str(e),
        }
    chosen = res.symbols[0] if res.symbols else UNDERLYING
    return chosen, res.payload


def evaluate_strategy(
    *,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
    asset_fetcher: Optional[AssetFetcher] = None,
    volume_provider: Optional[Callable[[str], float | None]] = None,
) -> WheelDecision:
    """Produce the wheel's decision for ``decision_date``.

    Pure-ish: reads broker state but never submits. The dispatch loop
    submits the resulting intents.
    """
    decision_date = decision_date or dt.date.today()
    underlying, universe_payload = _resolve_underlying_with_fallback(
        asset_fetcher=asset_fetcher, decision_date=decision_date,
        volume_provider=volume_provider,
    )
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
        underlying=underlying, underlying_price=0.0,
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
            universe=(underlying,),
            universe_payload=universe_payload or {},
        )

    # Pick the expiry chain
    expiries = list_option_expirations(underlying)
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
            universe=(underlying,),
            universe_payload=universe_payload or {},
        )

    chain = fetch_option_chain(underlying, expiry)
    if chain is None or chain.underlying_price <= 0:
        return WheelDecision(
            decision_date=decision_date, state=state, signal=null_signal,
            equity=equity, intents=[],
            universe=(underlying,),
            universe_payload=universe_payload or {},
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
            universe=(underlying,),
            universe_payload=universe_payload or {},
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
                underlying=underlying, underlying_price=chain.underlying_price,
                side=side, action="wait",
                contract_symbol=occ_ticker(underlying, expiry, side, contract.strike),
                strike=contract.strike, expiry=expiry,
                delta_estimate=target_delta, mid_price=contract.mid,
                contracts=0,
                rationale=f"qty=0 (options_bp=${options_bp:.0f}, "
                          f"strike={contract.strike}); skip this week",
            ),
            equity=equity, intents=[],
            universe=(underlying,),
            universe_payload=universe_payload or {},
        )

    occ = occ_ticker(underlying, expiry, side, contract.strike)
    sig = WheelSignal(
        decision_date=decision_date, state=state.value,
        underlying=underlying, underlying_price=chain.underlying_price,
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
        universe=(underlying,),
        universe_payload=universe_payload or {},
    )


__all__ = [
    "DISCOVERY_RULE", "WheelDecision",
    "evaluate_strategy", "should_rebalance_today",
]
