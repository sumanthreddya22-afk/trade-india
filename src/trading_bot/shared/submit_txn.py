"""Optimistic-concurrency transactional submit wrapper.

Single safety layer between every pipeline's debate-verdict and the
broker. All actual order submissions across all three pipelines (stocks,
crypto, options) go through ``submit_with_guard`` so the rules are
applied uniformly:

- Generate a deterministic ``client_order_id`` per verdict (broker-side
  duplicate protection).
- Inside one DB transaction:
    1. Re-read current position for the symbol/contract.
    2. Re-read the latest verdict for the same key from the pipeline's
       debate tables. If a newer verdict exists (by ``trigger_event_at``)
       this verdict is superseded — ABORT cleanly.
    3. Re-read free buying power. If insufficient — ABORT cleanly.
    4. Call the broker via the supplied submitter.
    5. Map broker errors (insufficient buying power, no such position,
       duplicate client_order_id) to clean ``SubmitOutcome`` states.
- Trust the broker (Alpaca) as source of truth for cash + positions;
  log + abort on broker rejection rather than retry mechanically.

Pipelines plug their own DB lookups via callbacks — this module never
imports per-pipeline tables. Lock keys are asset-class-aware:
- stocks/crypto: ``{asset_class}:{symbol}``
- options:       ``{asset_class}:{underlying}:{contract_id}``

See docs/adrs/0003-optimistic-concurrency-no-blocking.md.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome model
# ---------------------------------------------------------------------------


class SubmitStatus(str, Enum):
    SUBMITTED = "submitted"
    SUPERSEDED = "superseded"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    NO_SUCH_POSITION = "no_such_position"
    DUPLICATE_ORDER = "duplicate_order"
    BROKER_REJECTED = "broker_rejected"
    PRE_CHECK_FAILED = "pre_check_failed"


@dataclass
class SubmitOutcome:
    status: SubmitStatus
    client_order_id: str
    broker_order_id: Optional[str] = None
    reason: Optional[str] = None
    raw: Optional[dict] = None


# ---------------------------------------------------------------------------
# Verdict + lock key
# ---------------------------------------------------------------------------


@dataclass
class VerdictRef:
    """Minimal description of a verdict that wants to act on a position.

    Pipelines build this from their own per-pipeline ``*_debate_runs``
    rows. ``trigger_event_at`` is the UTC timestamp of the event that
    triggered the debate (NOT the time the verdict was written) — this
    is the canonical ordering field for supersession checks.

    For options, ``contract_id`` distinguishes contracts on the same
    underlying so a debate on the $180 CSP doesn't supersede a debate
    on the $190 CC.
    """
    verdict_id: str
    symbol: str
    asset_class: str           # "stock" | "crypto" | "options"
    trigger_event_at: datetime
    contract_id: Optional[str] = None
    estimated_buying_power_needed: float = 0.0


def lock_key(verdict: VerdictRef) -> str:
    """Per-position dedup key used in the supersession check.

    Two parallel debates produce the same lock_key only when they target
    the same position; debates on different symbols (or different
    contracts on the same underlying for options) never collide here.
    """
    if verdict.asset_class == "options" and verdict.contract_id:
        return f"{verdict.asset_class}:{verdict.symbol}:{verdict.contract_id}"
    return f"{verdict.asset_class}:{verdict.symbol}"


def make_client_order_id(verdict: VerdictRef, *, prefix: str = "tb") -> str:
    """Deterministic per-verdict client_order_id.

    Format: ``tb-{asset}-{symbol_safe}-{verdict_id_short}``.
    Alpaca caps client_order_id at 64 chars and uses it for duplicate
    detection — submitting the same id twice returns "duplicate order".
    """
    symbol_safe = verdict.symbol.replace("/", "").replace(":", "").lower()
    short_vid = verdict.verdict_id.replace("-", "")[:16]
    base = f"{prefix}-{verdict.asset_class[:5]}-{symbol_safe}-{short_vid}"
    return base[:64]


# ---------------------------------------------------------------------------
# Pluggable lookups (the pipeline-specific bits)
# ---------------------------------------------------------------------------


class LatestVerdictLookup(Protocol):
    """Returns the trigger_event_at of the most recent verdict for the
    symbol/contract from this pipeline's debate tables, or None if none."""

    def __call__(self, session: Session, verdict: VerdictRef) -> Optional[datetime]: ...


class FreeBuyingPowerLookup(Protocol):
    """Returns currently free buying power in account currency."""

    def __call__(self, session: Session) -> float: ...


class PositionLookup(Protocol):
    """Returns truthy if a non-zero position exists for the symbol/contract."""

    def __call__(self, session: Session, verdict: VerdictRef) -> bool: ...


class Submitter(Protocol):
    """Pipeline-specific broker call. Takes the precomputed client_order_id
    and returns the broker's order id (or raises BrokerRejection)."""

    def __call__(self, client_order_id: str) -> "BrokerSubmitResult": ...


@dataclass
class BrokerSubmitResult:
    broker_order_id: str
    raw: Optional[dict] = None


class BrokerRejection(RuntimeError):
    """Raised by the submitter callback when the broker rejects the order.

    The message is matched against well-known patterns (insufficient_buying_power,
    no such position, duplicate client_order_id) so the SubmitOutcome's
    status is set correctly. Unrecognised messages map to BROKER_REJECTED.
    """

    def __init__(self, message: str, raw: Optional[dict] = None) -> None:
        super().__init__(message)
        self.raw = raw


# ---------------------------------------------------------------------------
# Core submit
# ---------------------------------------------------------------------------


def _classify_broker_error(err_message: str) -> SubmitStatus:
    msg = (err_message or "").lower()
    if any(k in msg for k in ("insufficient_buying_power", "insufficient buying power", "buying_power")):
        return SubmitStatus.INSUFFICIENT_BUYING_POWER
    if any(k in msg for k in ("no such position", "position not found", "no_position")):
        return SubmitStatus.NO_SUCH_POSITION
    if any(k in msg for k in ("client_order_id", "duplicate", "already exists")):
        return SubmitStatus.DUPLICATE_ORDER
    return SubmitStatus.BROKER_REJECTED


def submit_with_guard(
    *,
    engine: Any,
    verdict: VerdictRef,
    submitter: Submitter,
    latest_verdict_lookup: LatestVerdictLookup,
    free_buying_power_lookup: FreeBuyingPowerLookup,
    position_lookup: Optional[PositionLookup] = None,
    require_position: bool = False,
    require_no_position: bool = False,
    client_order_id: Optional[str] = None,
) -> SubmitOutcome:
    """Submit one verdict's action to the broker under the optimistic-concurrency contract.

    ``submitter`` is the only piece that talks to Alpaca. Everything
    around it is the safety envelope. ``latest_verdict_lookup`` and
    ``free_buying_power_lookup`` are required (both are pipeline-specific).
    ``position_lookup`` plus ``require_position`` / ``require_no_position``
    let the caller assert the expected pre-state for the action being
    taken (e.g. an entry should require_no_position; a flatten should
    require_position).
    """
    coid = client_order_id or make_client_order_id(verdict)

    with Session(engine) as session:
        # --- pre-check 1: supersession ---
        latest_at = latest_verdict_lookup(session, verdict)
        if latest_at is not None and latest_at > verdict.trigger_event_at:
            outcome = SubmitOutcome(
                status=SubmitStatus.SUPERSEDED,
                client_order_id=coid,
                reason=(
                    f"verdict {verdict.verdict_id} for {lock_key(verdict)} superseded "
                    f"by newer verdict at {latest_at.isoformat()}"
                ),
            )
            logger.info(
                "submit_txn superseded: verdict=%s key=%s newer_at=%s",
                verdict.verdict_id, lock_key(verdict), latest_at.isoformat(),
            )
            return outcome

        # --- pre-check 2: position pre-state ---
        if position_lookup is not None and (require_position or require_no_position):
            has_pos = bool(position_lookup(session, verdict))
            if require_position and not has_pos:
                return SubmitOutcome(
                    status=SubmitStatus.NO_SUCH_POSITION,
                    client_order_id=coid,
                    reason=(
                        f"verdict {verdict.verdict_id} requires open position for "
                        f"{lock_key(verdict)} but none found (already closed?)"
                    ),
                )
            if require_no_position and has_pos:
                return SubmitOutcome(
                    status=SubmitStatus.PRE_CHECK_FAILED,
                    client_order_id=coid,
                    reason=(
                        f"verdict {verdict.verdict_id} would open new position for "
                        f"{lock_key(verdict)} but a position already exists"
                    ),
                )

        # --- pre-check 3: buying power ---
        if verdict.estimated_buying_power_needed > 0:
            free = float(free_buying_power_lookup(session))
            if free < verdict.estimated_buying_power_needed:
                return SubmitOutcome(
                    status=SubmitStatus.INSUFFICIENT_BUYING_POWER,
                    client_order_id=coid,
                    reason=(
                        f"verdict {verdict.verdict_id} needs ${verdict.estimated_buying_power_needed:.2f} "
                        f"but only ${free:.2f} available — outpriced by parallel order or position drift"
                    ),
                )

    # --- broker call (outside the read-only DB session; fresh session for any caller-side write) ---
    try:
        result = submitter(coid)
    except BrokerRejection as e:
        status = _classify_broker_error(str(e))
        logger.warning(
            "submit_txn broker rejected verdict=%s key=%s status=%s msg=%s",
            verdict.verdict_id, lock_key(verdict), status.value, str(e)[:200],
        )
        return SubmitOutcome(
            status=status,
            client_order_id=coid,
            reason=str(e),
            raw=getattr(e, "raw", None),
        )
    except Exception as e:  # noqa: BLE001 — bubble unknowns as broker_rejected
        logger.exception(
            "submit_txn unexpected error verdict=%s key=%s",
            verdict.verdict_id, lock_key(verdict),
        )
        return SubmitOutcome(
            status=SubmitStatus.BROKER_REJECTED,
            client_order_id=coid,
            reason=f"unexpected submitter error: {type(e).__name__}: {e}",
        )

    return SubmitOutcome(
        status=SubmitStatus.SUBMITTED,
        client_order_id=coid,
        broker_order_id=result.broker_order_id,
        raw=result.raw,
    )


# ---------------------------------------------------------------------------
# Convenience: wall-clock helper
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Single source of UTC truth for trigger_event_at timestamps."""
    return datetime.now(timezone.utc)
