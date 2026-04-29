"""run_wheel_scan + run_wheel_manage — the orchestrator entry points.
The deps-bag pattern makes the runner deterministic and unit-testable."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.alerts import AlertEvent
from trading_bot.config import WheelConfig
from trading_bot.intelligence_apewisdom import ApeWisdomClient
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_lane import WheelInputs, WheelLane
from trading_bot.options.wheel_state import (
    Phase, WheelStateRepo, close_cycle, increment_rolls, mark_assigned,
    open_cc, open_csp,
)
from trading_bot.options.symbols import parse_occ
from trading_bot.state_db import OptionFill, WheelCycle


@dataclass
class WheelDeps:
    cfg: WheelConfig
    engine: Engine
    option_alpaca: OptionAlpacaClient
    alpaca_client: object  # AlpacaClient (equity)
    risk_manager: object
    intelligence_macro: object
    regime_detector: object
    universe_filter: Callable[[], set[str]]
    iv_rank_for: Callable[[str], float | None]
    spot_for: Callable[[str], float | None]
    sentiment_for: Callable[[str], float | None]
    finnhub: FinnhubClient
    apewisdom: ApeWisdomClient
    alert_queue: Callable[[AlertEvent], None]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _today() -> dt.date:
    return _now().date()


def _journal_fill(
    engine: Engine, *, ts: dt.datetime, underlying: str, contract: str,
    option_type: str, side: str, strike: Decimal, expiration: dt.date,
    qty: int, premium: Decimal, alpaca_order_id: str, cycle_id: str | None,
    notes: str = "",
) -> None:
    with Session(engine) as s:
        s.add(OptionFill(
            ts=ts, underlying=underlying, contract_symbol=contract,
            option_type=option_type, side=side, strike=strike,
            expiration=expiration, qty=qty, premium=premium,
            alpaca_order_id=alpaca_order_id, cycle_id=cycle_id, notes=notes,
        ))
        s.commit()


def _emit(deps: WheelDeps, *, kind: str, severity: str, title: str,
          detail_html: str, dedup_key: str) -> None:
    deps.alert_queue(AlertEvent(
        kind=kind, severity=severity, title=title, detail_html=detail_html,
        fired_at=_now(), dedup_key=dedup_key,
    ))


def _existing_options_value(deps: WheelDeps) -> Decimal:
    """Sum of |market_value| across current option positions (rough collateral proxy)."""
    total = Decimal(0)
    try:
        positions = deps.option_alpaca.get_option_positions()
    except Exception:
        return total
    for p in positions:
        try:
            mv = abs(Decimal(str(getattr(p, "market_value", 0))))
            total += mv
        except Exception:
            continue
    return total


def run_wheel_scan(deps: WheelDeps) -> None:
    if not deps.cfg.enabled:
        return
    today = _today()
    regime = deps.regime_detector.detect()
    macro = deps.intelligence_macro.snapshot()
    vix = getattr(macro, "vix", None)
    repo = WheelStateRepo(deps.engine)
    eligible = deps.universe_filter()
    account = deps.alpaca_client.get_account()
    equity = Decimal(str(account.equity))
    existing_opt = _existing_options_value(deps)
    lane = WheelLane(deps.cfg)

    for symbol in sorted(eligible):
        try:
            chain = deps.option_alpaca.get_chain(
                symbol,
                expiration_gte=today + dt.timedelta(days=deps.cfg.dte_min),
                expiration_lte=today + dt.timedelta(days=deps.cfg.dte_max),
            )
        except Exception as e:
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"chain fetch failed: {symbol}",
                  detail_html=f"<p>{e}</p>", dedup_key=f"chain_fail_{symbol}_{today}")
            continue
        cycle = repo.get_active(symbol=symbol)
        # Guard: only the ASSIGNED phase or no-cycle should produce a new
        # entry. csp_open / cc_open are already managed by run_wheel_manage.
        if cycle is not None and cycle.phase != Phase.ASSIGNED.value:
            continue
        cost_basis: float | None = None
        if cycle is not None and cycle.phase == Phase.ASSIGNED.value:
            cost_basis = float(cycle.cost_basis or 0)
        decision = lane.evaluate(WheelInputs(
            symbol=symbol, regime=regime, vix=vix,
            sentiment_score=deps.sentiment_for(symbol),
            spot=(deps.spot_for(symbol) or 0.0),
            iv_rank=deps.iv_rank_for(symbol),
            finnhub=deps.finnhub, apewisdom=deps.apewisdom, today=today,
            chain=chain, cycle=cycle, cost_basis=cost_basis,
        ))
        if decision.action == "skip" or decision.contract is None:
            continue
        contract = decision.contract
        per_symbol_collateral = Decimal(str(contract.strike)) * Decimal(100)
        ok, reason = deps.risk_manager.option_collateral_ok(
            equity=equity, prospective_collateral=per_symbol_collateral,
            existing_options_value=existing_opt,
            per_symbol_collateral=per_symbol_collateral,
        )
        if not ok:
            _emit(deps, kind="wheel_allocation_cap", severity="bad",
                  title=f"wheel skipped {symbol}: {reason}",
                  detail_html=f"<p>{symbol}: {reason}</p>",
                  dedup_key=f"alloc_cap_{symbol}_{today}")
            continue
        limit = Decimal(str(round(contract.bid, 2)))
        try:
            order_id = deps.option_alpaca.sell_to_open(
                contract_symbol=contract.contract_symbol, qty=1, limit_price=limit,
            )
        except Exception as e:
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"sell-to-open failed: {symbol}",
                  detail_html=f"<p>{e}</p>",
                  dedup_key=f"sto_fail_{symbol}_{today}")
            continue
        if decision.action == "open_csp":
            cid = open_csp(repo, symbol=symbol, contract=contract.contract_symbol,
                           strike=Decimal(str(contract.strike)),
                           expiration=contract.expiration, credit=limit)
            otype = "CSP"
        else:
            assert cycle is not None
            open_cc(repo, cycle_id=cycle.cycle_id, contract=contract.contract_symbol,
                    strike=Decimal(str(contract.strike)),
                    expiration=contract.expiration, credit=limit)
            cid = cycle.cycle_id
            otype = "CC"
        _journal_fill(
            deps.engine, ts=_now(), underlying=symbol,
            contract=contract.contract_symbol, option_type=otype, side="SELL",
            strike=Decimal(str(contract.strike)), expiration=contract.expiration,
            qty=1, premium=limit, alpaca_order_id=order_id, cycle_id=cid,
        )
        _emit(deps, kind=("wheel_csp_opened" if otype == "CSP" else "wheel_cc_opened"),
              severity="info",
              title=f"{otype} opened: {symbol} {contract.strike} exp {contract.expiration}",
              detail_html=(f"<p>{symbol} sold {otype} @ {contract.strike} "
                           f"for {limit} (delta {contract.delta:.2f})</p>"),
              dedup_key=f"open_{otype}_{contract.contract_symbol}")


def _dte(expiration: dt.date, today: dt.date) -> int:
    return (expiration - today).days


def run_wheel_manage(deps: WheelDeps) -> None:
    if not deps.cfg.enabled:
        return
    today = _today()
    repo = WheelStateRepo(deps.engine)
    try:
        positions = deps.option_alpaca.get_option_positions()
    except Exception as e:
        _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
              title="get_option_positions failed",
              detail_html=f"<p>{e}</p>", dedup_key=f"pos_fail_{today}")
        return

    pos_by_contract = {str(p.symbol): p for p in positions}

    for cyc in repo.list_active():
        contract_sym = cyc.cc_contract or cyc.csp_contract
        if not contract_sym or contract_sym not in pos_by_contract:
            continue
        is_cc = (cyc.phase == Phase.CC_OPEN.value)
        try:
            snap = deps.option_alpaca.snapshot_for_contract(contract_sym)
        except Exception as e:
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"snapshot failed {contract_sym}",
                  detail_html=f"<p>{e}</p>",
                  dedup_key=f"snap_fail_{contract_sym}_{today}")
            continue
        mid = (snap.bid + snap.ask) / 2.0
        credit = float(cyc.cc_credit if is_cc else cyc.csp_credit or 0)
        exp = parse_occ(contract_sym).expiration
        dte = _dte(exp, today)
        delta_now = abs(snap.delta)
        # Take-profit: mid <= (1 - take_profit_pct) * credit
        take_profit_threshold = credit * (1 - deps.cfg.take_profit_pct)
        if mid <= take_profit_threshold:
            _close_short(deps, cyc, contract_sym, kind="wheel_take_profit",
                         price=Decimal(str(round(mid, 2))))
            continue
        if dte <= deps.cfg.dte_force_close:
            _close_short(deps, cyc, contract_sym, kind="wheel_dte_close",
                         price=Decimal(str(round(mid, 2))))
            continue
        breach = (deps.cfg.delta_breach_cc if is_cc else deps.cfg.delta_breach_csp)
        if delta_now >= breach and (cyc.rolls_used or 0) < deps.cfg.max_rolls_per_cycle:
            _try_roll(deps, cyc, contract_sym, is_cc=is_cc, today=today,
                      current_mid=mid)


def _close_short(deps: WheelDeps, cyc: WheelCycle, contract_sym: str,
                 *, kind: str, price: Decimal) -> None:
    try:
        order_id = deps.option_alpaca.buy_to_close(
            contract_symbol=contract_sym, qty=1, limit_price=price,
        )
    except Exception as e:
        _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
              title=f"buy-to-close failed {contract_sym}",
              detail_html=f"<p>{e}</p>",
              dedup_key=f"btc_fail_{contract_sym}_{_today()}")
        return
    is_cc = (cyc.phase == Phase.CC_OPEN.value)
    meta = parse_occ(contract_sym)
    _journal_fill(
        deps.engine, ts=_now(), underlying=cyc.symbol, contract=contract_sym,
        option_type=("CC" if is_cc else "CSP"), side="BUY",
        strike=Decimal(str(meta.strike)), expiration=meta.expiration, qty=1,
        premium=price, alpaca_order_id=order_id, cycle_id=cyc.cycle_id,
        notes=kind,
    )
    credit = (cyc.cc_credit if is_cc else cyc.csp_credit) or Decimal(0)
    pnl = (credit - price) * Decimal(100)
    if kind in ("wheel_take_profit", "wheel_dte_close"):
        repo = WheelStateRepo(deps.engine)
        if is_cc:
            total_pnl = pnl + (cyc.csp_credit or Decimal(0)) * Decimal(100)
        else:
            total_pnl = pnl
        close_cycle(repo, cycle_id=cyc.cycle_id, realized_pnl=total_pnl)
    _emit(deps, kind=kind, severity="info",
          title=f"{kind} {cyc.symbol} {contract_sym}",
          detail_html=f"<p>closed {contract_sym} for {price}, P&L {pnl}</p>",
          dedup_key=f"{kind}_{contract_sym}")


def _try_roll(
    deps: WheelDeps, cyc: WheelCycle, contract_sym: str, *,
    is_cc: bool, today: dt.date, current_mid: float,
) -> None:
    """Buy-to-close current short and sell-to-open a new one one expiry out
    at the same delta band. Best effort — if no replacement contract found,
    just close (treated as DTE-style close)."""
    try:
        new_chain = deps.option_alpaca.get_chain(
            cyc.symbol,
            expiration_gte=today + dt.timedelta(days=deps.cfg.dte_min),
            expiration_lte=today + dt.timedelta(days=deps.cfg.dte_max),
        )
    except Exception:
        new_chain = []
    if is_cc:
        from trading_bot.options.chain import pick_cc_contract
        pick = pick_cc_contract(new_chain,
                                cost_basis=float(cyc.cost_basis or 0),
                                cfg=deps.cfg, today=today)
    else:
        from trading_bot.options.chain import pick_csp_contract
        pick = pick_csp_contract(new_chain, cfg=deps.cfg, today=today)
    if pick is None:
        # Fall back to a defensive close
        _close_short(deps, cyc, contract_sym, kind="wheel_dte_close",
                     price=Decimal(str(round(current_mid, 2))))
        return
    # Close existing
    try:
        deps.option_alpaca.buy_to_close(contract_symbol=contract_sym, qty=1,
                                        limit_price=Decimal(str(round(current_mid, 2))))
    except Exception:
        return
    # Open new
    new_credit = Decimal(str(round(pick.bid, 2)))
    try:
        order_id = deps.option_alpaca.sell_to_open(
            contract_symbol=pick.contract_symbol, qty=1, limit_price=new_credit,
        )
    except Exception:
        return
    if is_cc:
        from trading_bot.options.wheel_state import open_cc as _open_cc
        _open_cc(WheelStateRepo(deps.engine), cycle_id=cyc.cycle_id,
                 contract=pick.contract_symbol,
                 strike=Decimal(str(pick.strike)),
                 expiration=pick.expiration, credit=new_credit)
    else:
        # Roll a CSP: same cycle stays in csp_open with updated contract
        with Session(deps.engine) as s:
            row = s.query(WheelCycle).filter(WheelCycle.cycle_id == cyc.cycle_id).one()
            row.csp_contract = pick.contract_symbol
            row.csp_strike = Decimal(str(pick.strike))
            row.csp_expiration = pick.expiration
            row.csp_credit = new_credit
            s.commit()
    increment_rolls(WheelStateRepo(deps.engine), cycle_id=cyc.cycle_id)
    _journal_fill(
        deps.engine, ts=_now(), underlying=cyc.symbol,
        contract=pick.contract_symbol, option_type="ROLL", side="SELL",
        strike=Decimal(str(pick.strike)), expiration=pick.expiration, qty=1,
        premium=new_credit, alpaca_order_id=order_id, cycle_id=cyc.cycle_id,
        notes="rolled_from " + contract_sym,
    )
    _emit(deps, kind="wheel_roll", severity="warn",
          title=f"wheel roll {cyc.symbol} {contract_sym} → {pick.contract_symbol}",
          detail_html=f"<p>rolled to delta {pick.delta:.2f}</p>",
          dedup_key=f"roll_{cyc.cycle_id}_{today}")
