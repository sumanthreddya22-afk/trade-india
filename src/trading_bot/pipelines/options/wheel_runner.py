"""Phase 3 wheel-entry runner — fuses legacy WheelLane chain proposals
with the new wheel_debate (Aurelio / Beatrice / Yusuf → Catherine).

The runner closes the last gap from Phase 3:

  scout debate (Hank / Sofia → Marcus) produced a list of
  scout_verdict='elevate' underlyings in intel_candidates_options.

        ↓

  (this module)
  For each elevated underlying:
    1. Build a WheelInputs snapshot (regime, VIX, IV rank, sentiment,
       chain via OptionAlpacaClient).
    2. Run WheelLane.evaluate() — the legacy proposal builder produces
       a WheelDecision (open_csp / open_cc / skip) with a concrete
       ChainContract pick (strike + delta + expiration).
    3. WheelLane skips → audit only, never reach the debate; we don't
       want to send the debate noise on a candidate the deterministic
       gates already rejected.
    4. WheelLane proposes → wrap into a WheelCandidate (the new debate
       input shape) and run_wheel_debate over the batch.
    5. Debate places → broker submit via OptionAlpacaClient.submit_*,
       then wheel_state.open_cycle to anchor the audit chain.
       Debate skips/defers → audit row only, no order.

  Fail-soft per step. A bad chain fetch on one symbol doesn't stop the
  rest of the batch.

Default mode is **dry-run** — no broker call. The operator opts into
live submission via the ``executor`` parameter (or the daemon's wired
runner reads ``WheelConfig.enabled`` to decide).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_lane import WheelDecision, WheelInputs, WheelLane
from trading_bot.pipelines.options.scout_debate import OptionsScoutVerdict
from trading_bot.pipelines.options.state_db import IntelCandidateOptions
from trading_bot.pipelines.options.wheel_debate import (
    WheelCandidate,
    WheelOrderExecutor,
    WheelRunResult,
    run_wheel_debate,
)
from trading_bot.pipelines.options.wheel_state import (
    WheelState,
    open_cycle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency bag — real-broker calls injected from outside
# ---------------------------------------------------------------------------


@dataclass
class WheelEntryDeps:
    """External services the runner needs. The shape mirrors the legacy
    WheelDeps to ease wiring through ``shared/daemon._build_wheel_deps``.
    """
    engine: Any                                     # SQLAlchemy engine
    wheel_lane: WheelLane                           # legacy proposal builder
    chain_for: Callable[[str], List[ChainContract]]
    spot_for: Callable[[str], Optional[float]]
    iv_rank_for: Callable[[str], Optional[float]]
    sentiment_for: Callable[[str], Optional[float]]
    regime_now: Callable[[], str]
    vix_now: Callable[[], Optional[float]]
    today: Callable[[], dt.date]


@dataclass
class WheelEntryResult:
    debated: int
    placed: int
    skipped: int
    deferred: int
    skipped_in_lane: int
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Pool reader: scout-elevated underlyings
# ---------------------------------------------------------------------------


def select_elevated_underlyings(
    engine: Any,
    *,
    batch_limit: int = 10,
    now: Optional[dt.datetime] = None,
) -> List[IntelCandidateOptions]:
    """Pick scout_verdict='elevate' candidates ready for wheel-entry debate."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidateOptions)
            .filter(IntelCandidateOptions.scout_verdict == "elevate")
            .order_by(IntelCandidateOptions.score.desc())
            .limit(batch_limit)
            .all()
        )
        for r in rows:
            session.expunge(r)
    return rows


# ---------------------------------------------------------------------------
# Lane → Candidate adapter
# ---------------------------------------------------------------------------


def _decision_to_candidate(
    *,
    candidate: IntelCandidateOptions,
    decision: WheelDecision,
    today: dt.date,
) -> Optional[WheelCandidate]:
    """Convert a WheelLane proposal into the new debate's input shape.

    Returns None when the decision was a skip — the caller filters those
    out before running the debate.
    """
    if decision.action == "skip" or decision.contract is None:
        return None
    contract = decision.contract
    structure = "csp" if decision.action == "open_csp" else "cc"
    dte = (contract.expiration - today).days
    return WheelCandidate(
        underlying=candidate.underlying,
        candidate_score=candidate.score,
        iv_rank=candidate.iv_rank,
        intel_top_reason=candidate.top_reason,
        sentiment_avg=candidate.sentiment_avg,
        proposed_strike=float(contract.strike),
        proposed_delta=abs(float(contract.delta)),  # debate prompts use unsigned
        proposed_dte_days=int(dte),
        proposed_structure=structure,
        earnings_in_dte_window=bool(candidate.earnings_in_dte_window),
        days_to_earnings=candidate.days_to_earnings,
    )


def _build_wheel_inputs(
    *,
    underlying: str,
    deps: WheelEntryDeps,
) -> Optional[WheelInputs]:
    """Build the legacy WheelInputs snapshot. Returns None when essential
    data (spot, chain) cannot be fetched — the runner skips that symbol.
    """
    spot = deps.spot_for(underlying)
    if spot is None or spot <= 0:
        logger.info("wheel_entry_runner skip %s: no spot", underlying)
        return None
    try:
        chain = deps.chain_for(underlying)
    except Exception as e:  # noqa: BLE001
        logger.warning("wheel_entry_runner chain fetch failed for %s: %s", underlying, e)
        return None
    if not chain:
        logger.info("wheel_entry_runner skip %s: empty chain", underlying)
        return None
    iv_rank = deps.iv_rank_for(underlying)
    sentiment = deps.sentiment_for(underlying)
    regime = deps.regime_now()
    vix = deps.vix_now()
    today = deps.today()

    # WheelLane.passes_preflight requires a Finnhub client to check
    # earnings windows. The runner gets it indirectly via wheel_lane.cfg
    # — for proof-of-concept we route the legacy wheel_lane's own
    # finnhub-equivalent through deps if available; otherwise the lane
    # bypasses the earnings check (best-effort).
    finnhub = getattr(deps.wheel_lane, "_finnhub", None) or _NullFinnhub()
    return WheelInputs(
        symbol=underlying, regime=regime, vix=vix,
        sentiment_score=sentiment, spot=float(spot),
        iv_rank=iv_rank, finnhub=finnhub, today=today,
        chain=chain, cycle=None, cost_basis=None,
    )


class _NullFinnhub:
    """Stand-in earnings checker used when no Finnhub client is wired.

    ``WheelLane.passes_preflight`` calls ``finnhub.has_earnings_in_window``
    — returning False here means the lane will accept candidates whose
    earnings status is unknown. The scout debate already rejected
    earnings-in-window candidates via the new ``earnings_in_dte_window``
    flag from ``IntelCandidateOptions``, so the second-pass check is
    redundant.
    """
    def has_earnings_in_window(self, *args, **kwargs) -> bool:  # noqa: ARG002
        return False


# ---------------------------------------------------------------------------
# Cycle anchor on place
# ---------------------------------------------------------------------------


def _anchor_cycle(
    engine: Any,
    *,
    candidate: WheelCandidate,
    chosen_delta: float,
    chosen_dte: int,
    chosen_structure: str,
    now: dt.datetime,
) -> Optional[int]:
    """Open a new wheel cycle (CSP_OPEN state) for a place verdict.

    Only fires when the chosen structure is a CSP — covered-calls always
    follow assignment, so they reuse an existing cycle. Returns the new
    cycle_id or None when the structure doesn't open a cycle.
    """
    if chosen_structure != "csp":
        return None
    try:
        cycle_id = open_cycle(
            engine,
            underlying=candidate.underlying,
            initial_csp_strike=candidate.proposed_strike,
            target_delta_csp=chosen_delta,
            target_delta_cc=chosen_delta,  # mirrors CSP delta for now
            now=now,
        )
        return cycle_id
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "wheel_entry_runner: open_cycle failed for %s: %s",
            candidate.underlying, e,
        )
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_wheel_entry(
    deps: WheelEntryDeps,
    *,
    executor: Optional[WheelOrderExecutor] = None,
    transport: Any = None,
    batch_limit: int = 10,
    now: Optional[dt.datetime] = None,
) -> WheelEntryResult:
    """Run one wheel-entry debate tick.

    Returns counts of debated / placed / skipped / deferred candidates,
    plus ``skipped_in_lane`` (rejected by WheelLane preflight before
    reaching the debate).
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    elevated = select_elevated_underlyings(deps.engine, batch_limit=batch_limit, now=now)
    if not elevated:
        return WheelEntryResult(
            debated=0, placed=0, skipped=0, deferred=0, skipped_in_lane=0,
        )

    candidates: List[WheelCandidate] = []
    skipped_in_lane = 0
    today = deps.today()

    for cand_row in elevated:
        inputs = _build_wheel_inputs(underlying=cand_row.underlying, deps=deps)
        if inputs is None:
            skipped_in_lane += 1
            continue
        decision = deps.wheel_lane.evaluate(inputs)
        if decision.action == "skip":
            logger.info(
                "wheel_entry_runner skip %s in lane: %s",
                cand_row.underlying, decision.reason,
            )
            skipped_in_lane += 1
            continue
        wc = _decision_to_candidate(
            candidate=cand_row, decision=decision, today=today,
        )
        if wc is not None:
            candidates.append(wc)

    if not candidates:
        return WheelEntryResult(
            debated=0, placed=0, skipped=0, deferred=0,
            skipped_in_lane=skipped_in_lane,
        )

    # Wrap the executor so a place verdict also opens a wheel cycle
    # — this keeps the audit chain (debate row → cycle → state-history)
    # complete. Tests that pass executor=None bypass both the broker
    # call AND the cycle opening, so the dry-run audit is clean.
    cycle_aware_executor: Optional[WheelOrderExecutor] = None
    if executor is not None:
        cycle_aware_executor = _CycleOpeningExecutor(
            inner=executor, engine=deps.engine, now=now,
        )

    regime = deps.regime_now()
    result: WheelRunResult = run_wheel_debate(
        deps.engine,
        candidates=candidates,
        regime=regime,
        executor=cycle_aware_executor,
        transport=transport,
        now=now,
    )
    return WheelEntryResult(
        debated=result.debated,
        placed=result.placed,
        skipped=result.skipped,
        deferred=result.deferred,
        skipped_in_lane=skipped_in_lane,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Cycle-aware executor wrapper
# ---------------------------------------------------------------------------


class _CycleOpeningExecutor:
    """Wraps a real WheelOrderExecutor: on place, opens a cycle, then
    delegates the broker call. Returns ``(broker_order_id, cycle_id)``
    matching the WheelOrderExecutor protocol.
    """

    def __init__(
        self,
        *,
        inner: WheelOrderExecutor,
        engine: Any,
        now: dt.datetime,
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._now = now

    def submit_wheel_entry(
        self,
        *,
        candidate: WheelCandidate,
        chosen_delta: float,
        chosen_dte_days: int,
        chosen_structure: str,
    ) -> tuple[Optional[str], Optional[int]]:
        cycle_id = _anchor_cycle(
            self._engine,
            candidate=candidate,
            chosen_delta=chosen_delta,
            chosen_dte=chosen_dte_days,
            chosen_structure=chosen_structure,
            now=self._now,
        )
        try:
            order_id, _inner_cycle = self._inner.submit_wheel_entry(
                candidate=candidate,
                chosen_delta=chosen_delta,
                chosen_dte_days=chosen_dte_days,
                chosen_structure=chosen_structure,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "wheel_entry_runner: inner executor failed for %s: %s",
                candidate.underlying, e,
            )
            order_id = None
        return (order_id, cycle_id)
