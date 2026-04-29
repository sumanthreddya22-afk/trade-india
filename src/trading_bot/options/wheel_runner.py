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
    eligible_for_today: Callable[[], set[str]]  # universe (auto-discovered)
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


def _build_sector_classifier(deps: WheelDeps):
    """Lazy import — sector_exposure pulls in yfinance only when wheel runs."""
    from trading_bot.sector_exposure import SectorClassifier
    return SectorClassifier(deps.engine)


def _compute_sector_exposure(positions, *, equity, classifier, option_collateral_by_symbol):
    from trading_bot.sector_exposure import compute_exposure
    return compute_exposure(
        positions, equity=equity, classifier=classifier,
        option_collateral_by_symbol=option_collateral_by_symbol,
    )


def _sector_cap_pct(deps: WheelDeps) -> float:
    """Read the cap from the AppConfig the risk manager carries.
    Defaults to 0.25 (25%) if the AppConfig isn't reachable."""
    rm = deps.risk_manager
    cfg = getattr(rm, "_cfg", None)
    risk = getattr(cfg, "risk", None) if cfg is not None else None
    return float(getattr(risk, "sector_cap_pct", 0.25))


def _sector_cap_check(
    *, symbol: str, prospective_collateral: Decimal, equity: Decimal,
    sector_exposure: dict[str, float], classifier, cap_pct: float,
) -> tuple[bool, str]:
    from trading_bot.sector_exposure import sector_cap_ok
    return sector_cap_ok(
        symbol=symbol, prospective_dollars=prospective_collateral,
        equity=equity, existing_exposure=sector_exposure,
        classifier=classifier, cap_pct=cap_pct,
    )


def run_wheel_scan(deps: WheelDeps) -> None:
    """Single per-scan flow, mirrors the equity orchestrator:
        universe → per-symbol intel → lane.passes_preflight (cheap gates,
        no chain) → chain fetch (only for survivors) → lane.evaluate
        (contract pick) → risk gates → order."""
    if not deps.cfg.enabled:
        return
    today = _today()
    regime = deps.regime_detector.detect()
    macro = deps.intelligence_macro.snapshot()
    vix = getattr(macro, "vix", None)
    repo = WheelStateRepo(deps.engine)
    eligible = deps.eligible_for_today()
    account = deps.alpaca_client.get_account()
    equity = Decimal(str(account.equity))
    existing_opt = _existing_options_value(deps)
    lane = WheelLane(deps.cfg)

    # Precompute current sector exposure once per scan: equity positions +
    # pending option collateral on open wheel cycles. Mutated as we open new
    # CSPs so two same-sector candidates in one scan can't both pass.
    sector_classifier = _build_sector_classifier(deps)
    sector_cap_pct = _sector_cap_pct(deps)
    open_cycles = repo.list_active()
    open_collateral_by_symbol: dict[str, Decimal] = {}
    for c in open_cycles:
        strike = c.cc_strike if c.phase == Phase.CC_OPEN.value else c.csp_strike
        if strike is not None:
            open_collateral_by_symbol[c.symbol] = (
                open_collateral_by_symbol.get(c.symbol, Decimal(0))
                + Decimal(str(strike)) * Decimal(100)
            )
    try:
        positions = deps.alpaca_client.get_positions()
    except Exception:
        positions = []
    sector_exposure = _compute_sector_exposure(
        positions, equity=equity, classifier=sector_classifier,
        option_collateral_by_symbol=open_collateral_by_symbol,
    )

    for symbol in sorted(eligible):
        # Per-symbol intel (cheap — all from cached data, no network calls
        # except the Finnhub earnings calendar which Finnhub itself caches).
        cycle = repo.get_active(symbol=symbol)
        cost_basis: float | None = None
        if cycle is not None and cycle.phase == Phase.ASSIGNED.value:
            cost_basis = float(cycle.cost_basis or 0)
        intel = WheelInputs(
            symbol=symbol, regime=regime, vix=vix,
            sentiment_score=deps.sentiment_for(symbol),
            spot=(deps.spot_for(symbol) or 0.0),
            iv_rank=deps.iv_rank_for(symbol),
            finnhub=deps.finnhub, apewisdom=deps.apewisdom, today=today,
            chain=[],  # filled in below if preflight passes
            cycle=cycle, cost_basis=cost_basis,
        )
        # Stage 1: cheap gates (regime, VIX, sentiment, IV rank, WSB,
        # earnings, cycle state). Skips ~95% of names on a typical day.
        skip_reason = lane.passes_preflight(intel)
        if skip_reason is not None:
            continue

        # Stage 2: chain fetch (only for symbols that passed preflight)
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

        # Stage 3: full lane evaluation (preflight + contract pick)
        from dataclasses import replace
        decision = lane.evaluate(replace(intel, chain=chain))
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
        sector_ok, sector_reason = _sector_cap_check(
            symbol=symbol, prospective_collateral=per_symbol_collateral,
            equity=equity, sector_exposure=sector_exposure,
            classifier=sector_classifier, cap_pct=sector_cap_pct,
        )
        if not sector_ok:
            _emit(deps, kind="wheel_allocation_cap", severity="bad",
                  title=f"wheel skipped {symbol}: {sector_reason}",
                  detail_html=f"<p>{symbol}: {sector_reason}</p>",
                  dedup_key=f"sector_cap_{symbol}_{today}")
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
        # Update sector exposure so a second same-sector candidate in this same
        # scan can't pass the gate against pre-trade exposure.
        sector = sector_classifier.get(symbol)
        if equity > 0:
            addition = float(per_symbol_collateral / equity)
            sector_exposure[sector] = sector_exposure.get(sector, 0.0) + addition
        existing_opt = existing_opt + per_symbol_collateral


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
