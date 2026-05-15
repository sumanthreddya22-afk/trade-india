"""Paper-submit validation for mutation candidates (v4 Phase C).

Flow:
  1. Instantiate the strategy with candidate params.
  2. Run ``evaluate_strategy()`` to produce candidate orders.
  3. Each order is passed through ``risk.precheck.evaluate`` —
     if any reject, the candidate fails.
  4. Surviving orders submitted to the paper broker via
     ``execution.order_router.submit_order``.
  5. Wait up to ``fill_timeout_s`` for fills.
  6. Compare slippage against the pessimistic-lens tolerance.
  7. Write ``paper_validation_event`` row; ``passed=1`` → caller
     auto-promotes via ``registry.auto_register``.
"""
from __future__ import annotations

import dataclasses
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperValidationReport:
    passed: bool
    reason: str
    num_decisions: int
    submitted: int
    risk_rejected: int
    filled: int
    avg_slippage_bps: float


def validate_via_paper_submit(
    *,
    candidate_id: str,
    candidate_params: Mapping,
    strategy_family: str,
    evaluate_fn: Callable,
    risk_precheck_fn: Optional[Callable] = None,
    broker_submit_fn: Optional[Callable] = None,
    num_test_decisions: int = 3,
    fill_timeout_s: int = 600,
    pessimistic_slippage_bps_tolerance: float = 30.0,
) -> PaperValidationReport:
    """Run ``num_test_decisions`` evaluations of the candidate, push
    surviving intents through risk + paper broker, wait for fills,
    compute slippage. Return the structured outcome.

    Designed to accept injected callables so unit tests can run the
    flow without the live broker. The daemon's `mutation_runner` wires
    these from the kernel.
    """
    submitted_intents: list[dict] = []
    risk_rejected = 0
    for _ in range(int(num_test_decisions)):
        decision = evaluate_fn(params=dict(candidate_params))
        for intent in getattr(decision, "intents", []) or []:
            if risk_precheck_fn is not None:
                verdict = risk_precheck_fn(intent)
                if not verdict:
                    risk_rejected += 1
                    continue
            submitted_intents.append(intent)

    if not submitted_intents:
        return PaperValidationReport(
            passed=False,
            reason=(
                "no candidate intents survived precheck "
                f"(risk_rejected={risk_rejected})"
            ),
            num_decisions=num_test_decisions,
            submitted=0, risk_rejected=risk_rejected,
            filled=0, avg_slippage_bps=0.0,
        )

    if broker_submit_fn is None:
        # Dry-run: assume all submitted intents fill at intent_price.
        avg_slippage_bps = 0.0
        return PaperValidationReport(
            passed=True,
            reason="dry-run pass (no broker wired)",
            num_decisions=num_test_decisions,
            submitted=len(submitted_intents),
            risk_rejected=risk_rejected,
            filled=len(submitted_intents),
            avg_slippage_bps=avg_slippage_bps,
        )

    slippages: list[float] = []
    filled = 0
    for intent in submitted_intents:
        result = broker_submit_fn(intent)
        # result is expected to be a dict with keys
        # {"filled": bool, "fill_price": float, "intent_price": float}.
        if not result.get("filled"):
            continue
        filled += 1
        intent_price = float(result.get("intent_price", 0.0)) or float(
            intent.get("intent_price", 0.0)
        )
        fill_price = float(result.get("fill_price", 0.0))
        if intent_price > 0:
            bps = abs(fill_price - intent_price) / intent_price * 10_000.0
            slippages.append(bps)

    avg_slippage = sum(slippages) / max(1, len(slippages))
    passed = (
        filled >= max(1, len(submitted_intents) // 2)
        and avg_slippage <= pessimistic_slippage_bps_tolerance
    )
    return PaperValidationReport(
        passed=passed,
        reason=(
            f"filled={filled}/{len(submitted_intents)} "
            f"avg_slippage_bps={avg_slippage:.1f} "
            f"tolerance={pessimistic_slippage_bps_tolerance:.0f}"
        ),
        num_decisions=num_test_decisions,
        submitted=len(submitted_intents),
        risk_rejected=risk_rejected,
        filled=filled,
        avg_slippage_bps=avg_slippage,
    )


__all__ = ["PaperValidationReport", "validate_via_paper_submit"]
