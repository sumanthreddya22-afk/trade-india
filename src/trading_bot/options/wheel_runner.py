"""run_wheel_scan + run_wheel_manage — the orchestrator entry points.
The deps-bag pattern makes the runner deterministic and unit-testable.

Audit logging (added 2026-05-01): every wheel_scan now records a
WheelScanStats summary — universe size, per-stage rejection counts, and
representative reasons — both as a structured log event
(`wheel_scan_summary`) and as a JSON file at
`data/wheel_scan_last.json`. Mirrors the equity scanner's last_scan.json
pattern so the dashboard and the daemon log can answer "where are
options candidates dying?" without re-running the scan.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.alerts import AlertEvent
from trading_bot.config import WheelConfig
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.log_structured import StructuredLogger
from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.chain import ChainContract, annualized_yield
from trading_bot.options.wheel_lane import WheelInputs, WheelLane
from trading_bot.options.wheel_state import (
    Phase, WheelStateRepo, close_cycle, increment_rolls, mark_assigned,
    open_cc, open_csp,
)
from trading_bot.options.symbols import parse_occ
from trading_bot.state_db import OptionFill, UnblockDebateRun, WheelCycle


_log = logging.getLogger(__name__)
_audit_log = StructuredLogger(role="wheel_scan")
_LAST_SCAN_PATH = Path("data/wheel_scan_last.json")
_REASON_SAMPLE_CAP = 5  # cap per-stage representative reasons in the summary


@dataclass
class WheelScanStats:
    """Per-stage counts + reason histograms recorded across one wheel_scan
    run. Persisted as `data/wheel_scan_last.json` and emitted as a
    `wheel_scan_summary` structured event so the dashboard / log can show
    a candidate funnel without re-running the scan.
    """
    started_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    finished_at: str | None = None
    universe_size: int = 0
    preflight_skipped: int = 0
    chain_fetch_failed: int = 0
    no_contract_picked: int = 0
    risk_alloc_rejected: int = 0
    sector_cap_rejected: int = 0
    submit_failed: int = 0
    orders_placed: int = 0
    preflight_reasons: dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))
    no_contract_reasons: dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))
    risk_alloc_reasons: dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))
    sector_cap_reasons: dict[str, int] = field(default_factory=lambda: collections.defaultdict(int))

    def to_dict(self) -> dict:
        # Build manually — dataclasses.asdict() chokes on defaultdict
        # because it tries to call type(obj)((k, v), ...) on the dict.
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "universe_size": self.universe_size,
            "preflight_skipped": self.preflight_skipped,
            "chain_fetch_failed": self.chain_fetch_failed,
            "no_contract_picked": self.no_contract_picked,
            "risk_alloc_rejected": self.risk_alloc_rejected,
            "sector_cap_rejected": self.sector_cap_rejected,
            "submit_failed": self.submit_failed,
            "orders_placed": self.orders_placed,
            "preflight_reasons": dict(self.preflight_reasons),
            "no_contract_reasons": dict(self.no_contract_reasons),
            "risk_alloc_reasons": dict(self.risk_alloc_reasons),
            "sector_cap_reasons": dict(self.sector_cap_reasons),
        }


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


def _alloc_overage_ratio(*, deps: WheelDeps, equity: Decimal,
                          existing_opt: Decimal,
                          per_symbol_collateral: Decimal) -> float:
    """Ratio of how far over the options_max_pct cap this proposed trade
    sits. 0.0 = exactly at cap. 0.5 = 50% over. Used by the predicate
    to gate which rejections are worth debating."""
    cap_pct = float(getattr(deps.cfg, "options_max_pct",
                              getattr(getattr(deps.risk_manager, "_cfg", None),
                                      "allocation", None) and
                              getattr(getattr(deps.risk_manager, "_cfg", None).allocation,
                                      "options_max_pct", 20.0) or 20.0))
    cap_usd = float(equity) * cap_pct / 100.0
    proposed_usd = float(existing_opt) + float(per_symbol_collateral)
    if cap_usd <= 0:
        return 9.99  # cap effectively zero — anything is huge over
    return max(0.0, (proposed_usd - cap_usd) / cap_usd)


def _sector_overage_ratio(*, symbol: str, equity: Decimal,
                            per_symbol_collateral: Decimal,
                            sector_exposure: dict[str, float],
                            classifier, cap_pct: float) -> float:
    """Ratio over the sector-cap. Same shape as _alloc_overage_ratio."""
    sector = classifier.get(symbol)
    current = sector_exposure.get(sector, 0.0)
    addition = float(per_symbol_collateral) / float(equity) if equity > 0 else 0.0
    proposed = current + addition
    if cap_pct <= 0:
        return 9.99
    return max(0.0, (proposed - cap_pct) / cap_pct)


def _candidate_score(*, contract: ChainContract, iv_rank: float | None,
                     today: dt.date) -> float:
    """0-10 score blending IV rank (0-50% of weight) + ann yield (0-50%).

    Used by the unblock-committee predicate to gate which rejected
    candidates are worth debating. Generic-shape "rich premium" alone
    isn't enough; we want IV-rich AND yield-rich together.
    """
    iv_component = 0.0
    if iv_rank is not None:
        iv_component = max(0.0, min(50.0, float(iv_rank))) / 10.0  # 0-5
    ay = annualized_yield(contract, today)  # decimal, e.g. 0.30 = 30%
    yield_component = max(0.0, min(0.50, ay)) * 10.0  # 0-5 (cap at 50% ay)
    return round(iv_component + yield_component, 2)


def _count_todays_unblock_debates(engine) -> int:
    """Count debates fired today across all asset classes — used by the
    daily cap predicate to bound LLM spend."""
    try:
        with Session(engine) as s:
            today_utc_start = dt.datetime.combine(
                dt.datetime.now(dt.timezone.utc).date(),
                dt.time.min, tzinfo=dt.timezone.utc,
            )
            return s.query(UnblockDebateRun).filter(
                UnblockDebateRun.run_at >= today_utc_start
            ).count()
    except Exception:
        return 0


def _maybe_run_wheel_unblock(
    deps: WheelDeps,
    *,
    symbol: str,
    contract: ChainContract,
    block_reason: str,
    overage_ratio: float,
    today: dt.date,
    iv_rank: float | None,
    operational_context: str,
) -> bool:
    """Returns True if the gate should be OVERRIDDEN (proceed to place);
    False to RESPECT the gate (skip).

    Default-deny: any error / disabled flag / failed predicate / committee
    'reject' → returns False so existing skip behavior is preserved.
    """
    if not deps.cfg.unblock_debate_enabled:
        return False

    score = _candidate_score(contract=contract, iv_rank=iv_rank, today=today)
    daily_count = _count_todays_unblock_debates(deps.engine)

    from trading_bot.unblock_debate import (
        run_unblock_debate, should_unblock_debate,
    )
    if not should_unblock_debate(
        rejection_reason=block_reason,
        rejection_overage_ratio=overage_ratio,
        candidate_score=score,
        daily_debate_count=daily_count,
        max_overage_ratio=deps.cfg.unblock_max_overage_ratio,
        min_score=deps.cfg.unblock_min_candidate_score,
        daily_cap=deps.cfg.unblock_daily_debate_cap,
    ):
        return False

    proposal = (
        f"  symbol:           {symbol}\n"
        f"  contract:         {contract.contract_symbol}\n"
        f"  action:           sell-to-open CSP\n"
        f"  strike:           {contract.strike}\n"
        f"  expiration:       {contract.expiration}\n"
        f"  bid:              {contract.bid}\n"
        f"  ask:              {contract.ask}\n"
        f"  delta:            {contract.delta}\n"
        f"  iv:               {contract.implied_volatility}\n"
        f"  ann_yield_est:    {annualized_yield(contract, today):.4f}\n"
    )
    fundamentals = (
        f"  iv_rank:          {iv_rank}\n"
        f"  candidate_score:  {score} (0-10)\n"
        f"  collateral_usd:   {float(contract.strike) * 100}\n"
    )

    verdict = run_unblock_debate(
        deps.engine,
        proposal_summary=proposal,
        block_reason=block_reason,
        overage_ratio=overage_ratio,
        fundamentals=fundamentals,
        operational_context=operational_context,
        # lessons_block left empty until the lesson loop is wired
        # Wheel-scan is a synchronous interactive flow — using the
        # async mailbox here would block the scan for up to N minutes
        # waiting for the next routine fire. Mailbox is the right
        # transport for nightly batch (decision_reflector); direct API
        # is the right transport for in-loop wheel debates.
        use_mailbox=False,
    )

    # Persist the row whether verdict is None, place, or reject — full audit.
    try:
        with Session(deps.engine) as s:
            row = UnblockDebateRun(
                run_at=_now(),
                asset_class="wheel",
                symbol=symbol,
                candidate_score=score,
                block_reason=block_reason,
                overage_ratio=overage_ratio,
                verdict=(verdict.recommendation if verdict else "fail_closed"),
                confidence=(verdict.confidence if verdict else "low"),
                judge_reason=(verdict.reason if verdict else "no verdict (fail-closed)"),
                aggressive_text=(verdict.aggressive_text if verdict else ""),
                conservative_text=(verdict.conservative_text if verdict else ""),
                neutral_text=(verdict.neutral_text if verdict else ""),
                synthetic=False,
            )
            s.add(row)
            s.commit()
    except Exception as e:
        _log.warning("unblock_debate persist failed for %s: %s", symbol, e)

    # Operator-visibility: email every debate (whether the verdict was
    # acted on or not) so the operator sees what the committee considered
    # for each ticker. Wrapped in try/except — email failures never
    # break the scan loop.
    try:
        from trading_bot.email_unblock_debate import (
            DebateEmailContext, send_debate_email,
        )
        send_debate_email(DebateEmailContext(
            asset_class="wheel", symbol=symbol,
            block_reason=block_reason, overage_ratio=overage_ratio,
            candidate_score=score,
            proposal_summary=proposal,
            fundamentals=fundamentals,
            operational_context=operational_context,
            verdict=verdict,
        ))
    except Exception as e:
        _log.warning("wheel debate email failed for %s: %s", symbol, e)

    if verdict is None:
        _audit_log.event(
            "wheel_unblock_fail_closed", symbol=symbol,
            block_reason=block_reason, overage_ratio=overage_ratio,
        )
        return False

    _audit_log.event(
        "wheel_unblock_verdict", symbol=symbol,
        verdict=verdict.recommendation, confidence=verdict.confidence,
        block_reason=block_reason, overage_ratio=overage_ratio,
        candidate_score=score,
    )
    return verdict.recommendation == "place"


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
    stats = WheelScanStats(universe_size=len(eligible))
    # Write the started snapshot immediately so a hang past _emit_scan_summary
    # leaves a trace on disk for the stall-watchdog to pick up.
    _persist_scan_summary(stats)

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
            finnhub=deps.finnhub, today=today,
            chain=[],  # filled in below if preflight passes
            cycle=cycle, cost_basis=cost_basis,
        )
        # Stage 1: cheap gates (regime, VIX, sentiment, IV rank, WSB,
        # earnings, cycle state). Skips ~95% of names on a typical day.
        skip_reason = lane.passes_preflight(intel)
        if skip_reason is not None:
            stats.preflight_skipped += 1
            stats.preflight_reasons[_norm_reason(skip_reason)] += 1
            continue

        # Stage 2: chain fetch (only for symbols that passed preflight)
        try:
            chain = deps.option_alpaca.get_chain(
                symbol,
                expiration_gte=today + dt.timedelta(days=deps.cfg.dte_min),
                expiration_lte=today + dt.timedelta(days=deps.cfg.dte_max),
            )
        except Exception as e:
            stats.chain_fetch_failed += 1
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"chain fetch failed: {symbol}",
                  detail_html=f"<p>{e}</p>", dedup_key=f"chain_fail_{symbol}_{today}")
            continue

        # Stage 3: full lane evaluation (preflight + contract pick)
        from dataclasses import replace
        decision = lane.evaluate(replace(intel, chain=chain))
        if decision.action == "skip" or decision.contract is None:
            stats.no_contract_picked += 1
            stats.no_contract_reasons[_norm_reason(getattr(decision, "reason", "no_contract"))] += 1
            continue
        contract = decision.contract
        per_symbol_collateral = Decimal(str(contract.strike)) * Decimal(100)
        ok, reason = deps.risk_manager.option_collateral_ok(
            equity=equity, prospective_collateral=per_symbol_collateral,
            existing_options_value=existing_opt,
            per_symbol_collateral=per_symbol_collateral,
        )
        if not ok:
            # Compute overage ratio for the unblock-committee predicate:
            # how far over the cap is THIS proposed trade. Cap is captured
            # from the AppConfig the risk manager carries (options_max_pct,
            # %); proposed = (existing + this trade) / equity.
            overage_ratio_alloc = _alloc_overage_ratio(
                deps=deps, equity=equity, existing_opt=existing_opt,
                per_symbol_collateral=per_symbol_collateral,
            )
            override = _maybe_run_wheel_unblock(
                deps, symbol=symbol, contract=contract,
                block_reason=str(reason),
                overage_ratio=overage_ratio_alloc,
                today=today, iv_rank=intel.iv_rank,
                operational_context=(
                    f"  equity_usd:      {equity}\n"
                    f"  existing_opt:    {existing_opt}\n"
                    f"  cap_setting:     options_max_pct\n"
                ),
            )
            if not override:
                stats.risk_alloc_rejected += 1
                stats.risk_alloc_reasons[_norm_reason(reason)] += 1
                _emit(deps, kind="wheel_allocation_cap", severity="bad",
                      title=f"wheel skipped {symbol}: {reason}",
                      detail_html=f"<p>{symbol}: {reason}</p>",
                      dedup_key=f"alloc_cap_{symbol}_{today}")
                continue
            # Override path: log and proceed to sector-cap check (which
            # may itself fire another debate).
            _emit(deps, kind="wheel_unblock_override", severity="info",
                  title=f"unblock: override risk-cap for {symbol}",
                  detail_html=f"<p>committee voted override for {symbol}: {reason}</p>",
                  dedup_key=f"unblock_alloc_{symbol}_{today}")
        sector_ok, sector_reason = _sector_cap_check(
            symbol=symbol, prospective_collateral=per_symbol_collateral,
            equity=equity, sector_exposure=sector_exposure,
            classifier=sector_classifier, cap_pct=sector_cap_pct,
        )
        if not sector_ok:
            overage_ratio_sector = _sector_overage_ratio(
                symbol=symbol, equity=equity,
                per_symbol_collateral=per_symbol_collateral,
                sector_exposure=sector_exposure,
                classifier=sector_classifier, cap_pct=sector_cap_pct,
            )
            override = _maybe_run_wheel_unblock(
                deps, symbol=symbol, contract=contract,
                block_reason=str(sector_reason),
                overage_ratio=overage_ratio_sector,
                today=today, iv_rank=intel.iv_rank,
                operational_context=(
                    f"  equity_usd:    {equity}\n"
                    f"  cap_setting:   sector_cap_pct={sector_cap_pct}\n"
                ),
            )
            if not override:
                stats.sector_cap_rejected += 1
                stats.sector_cap_reasons[_norm_reason(sector_reason)] += 1
                _emit(deps, kind="wheel_allocation_cap", severity="bad",
                      title=f"wheel skipped {symbol}: {sector_reason}",
                      detail_html=f"<p>{symbol}: {sector_reason}</p>",
                      dedup_key=f"sector_cap_{symbol}_{today}")
                continue
            _emit(deps, kind="wheel_unblock_override", severity="info",
                  title=f"unblock: override sector-cap for {symbol}",
                  detail_html=f"<p>committee voted override for {symbol}: {sector_reason}</p>",
                  dedup_key=f"unblock_sector_{symbol}_{today}")
        # Bucket E: limit at MID, not bid. Selling at the bid rarely fills
        # on liquid options because everyone else is also crossing the
        # spread; mid is the marketable-but-fair price for an STO.
        mid = (contract.bid + contract.ask) / 2.0
        limit = Decimal(str(round(mid, 2)))
        try:
            order_id = deps.option_alpaca.sell_to_open(
                contract_symbol=contract.contract_symbol, qty=1, limit_price=limit,
            )
        except Exception as e:
            stats.submit_failed += 1
            _emit(deps, kind="wheel_chain_fetch_failure", severity="bad",
                  title=f"sell-to-open failed: {symbol}",
                  detail_html=f"<p>{e}</p>",
                  dedup_key=f"sto_fail_{symbol}_{today}")
            continue
        stats.orders_placed += 1
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

    _emit_scan_summary(stats)


def _norm_reason(reason) -> str:
    """Normalise a freeform reject reason into a histogram bucket — strip
    symbol-specific suffixes ('rsi 41.2 outside [55,70]' → 'rsi outside band')
    so the per-stage histograms stay legible.
    """
    s = str(reason or "unspecified").strip().lower()
    # Keep first 60 chars; replace digits with N to collapse value variants.
    import re
    s = re.sub(r"-?\d+(?:\.\d+)?", "N", s)
    return s[:60]


def _emit_scan_summary(stats: WheelScanStats) -> None:
    """Persist + log the scan summary so we can answer 'why didn't wheel
    place anything?' from artifacts alone — without re-running the scan."""
    stats.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _persist_scan_summary(stats)
    try:
        payload = stats.to_dict()
        _audit_log.event("wheel_scan_summary", **{
            k: v for k, v in payload.items()
            if k in ("universe_size", "preflight_skipped", "chain_fetch_failed",
                     "no_contract_picked", "risk_alloc_rejected",
                     "sector_cap_rejected", "submit_failed", "orders_placed")
        })
    except Exception:
        pass


def _persist_scan_summary(stats: WheelScanStats) -> None:
    """Atomic write of the scan-state file. Called both at scan start
    (with finished_at=None) and at scan end (with stats fully filled).
    The presence of finished_at=None on a stale started_at is the
    signal a stall watchdog uses."""
    payload = stats.to_dict()
    try:
        _LAST_SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LAST_SCAN_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=2))
        tmp.rename(_LAST_SCAN_PATH)
    except OSError as e:
        _log.warning("could not write %s: %s", _LAST_SCAN_PATH, e)


def is_wheel_scan_stalled(*, max_age_seconds: int = 900,
                           path: Path | None = None,
                           now_utc: dt.datetime | None = None) -> tuple[bool, dict]:
    """Check whether the latest wheel_scan started but never finished.

    Returns (stalled, info_dict). ``info_dict`` carries started_at,
    finished_at, age_seconds, and universe_size for downstream alerting.
    A clean run (finished_at populated) returns (False, ...).
    Missing scan-state file returns (False, {"reason": "no_scan_state"}).
    """
    p = path or _LAST_SCAN_PATH
    if not p.exists():
        return False, {"reason": "no_scan_state"}
    try:
        payload = json.loads(p.read_text())
    except Exception as e:
        return False, {"reason": f"unparseable_scan_state: {e}"}
    if payload.get("finished_at"):
        return False, {"reason": "completed",
                       "started_at": payload.get("started_at"),
                       "finished_at": payload["finished_at"]}
    started_iso = payload.get("started_at")
    if not started_iso:
        return False, {"reason": "no_started_at"}
    try:
        started = dt.datetime.fromisoformat(str(started_iso))
    except Exception:
        return False, {"reason": "unparseable_started_at"}
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    age = (now - started).total_seconds()
    if age > max_age_seconds:
        return True, {
            "started_at": started_iso, "finished_at": None,
            "age_seconds": int(age),
            "universe_size": payload.get("universe_size", 0),
        }
    return False, {"started_at": started_iso, "finished_at": None,
                   "age_seconds": int(age)}


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
    # Open new — Bucket E: limit at mid, not bid (see open_csp/open_cc above)
    new_mid = (pick.bid + pick.ask) / 2.0
    new_credit = Decimal(str(round(new_mid, 2)))
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
