"""Adversarial-pair hypothesis intake.

Plan v4 §1A: every L3 hypothesis is reviewed by a single call set —
``quant_research_lead.v1`` (proposer/supporter) + ``risk_validator.v1``
(critic). Both transcripts are written to ``strategy_decision``
regardless of who wins.

If risk_validator returns ``verdict=block`` with ``confidence > 0.7``,
the hypothesis is dead unless a human operator records a documented
override.

Phase 5 ships the schema, the runner interface, and a ``MockPersonaRunner``
for tests. Phase 6 wires the real LLM calls through the mailbox /
Claude CLI subprocess.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional

from trading_bot.ledger import write_decision
from trading_bot.research.persona_schema import validate_persona_output

VerdictT = Literal["support", "block", "abstain"]


@dataclass(frozen=True)
class HypothesisProposal:
    thesis_id: str
    hypothesis_id: str
    description: str
    mechanism: str                       # plain-English causal story
    expected_regimes: tuple[str, ...]
    kill_criteria: tuple[str, ...]
    proposed_by: str                     # "operator" | "mutation_engine"

    def hash(self) -> str:
        payload = json.dumps(
            {
                "thesis_id": self.thesis_id,
                "hypothesis_id": self.hypothesis_id,
                "description": self.description,
                "mechanism": self.mechanism,
                "expected_regimes": list(self.expected_regimes),
                "kill_criteria": list(self.kill_criteria),
            },
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


PersonaRunnerT = Callable[[HypothesisProposal], Mapping[str, Any]]
"""A persona runner: takes the proposal, returns the persona's JSON
output. Phase 5 tests use ``MockPersonaRunner``; Phase 6 wires real
LLM."""


@dataclass(frozen=True)
class IntakeResult:
    accepted: bool
    hypothesis_hash: str
    research_lead_output: Mapping[str, Any]
    risk_validator_output: Mapping[str, Any]
    reason: str


@dataclass(frozen=True)
class MockPersonaRunner:
    """Test/stub runner. Returns a canned valid persona output.

    Construct with ``MockPersonaRunner(role='risk_validator.v1',
    verdict='block', confidence=0.9)`` etc. The returned output is
    schema-valid by construction.
    """

    role: str
    verdict: VerdictT = "support"
    confidence: float = 0.5
    concerns: tuple[str, ...] = ()
    kill_conditions: tuple[str, ...] = ()
    free_text: str = ""

    def __call__(self, proposal: HypothesisProposal) -> Mapping[str, Any]:
        return {
            "role": self.role,
            "role_hash": "sha256:phase5-mock",
            "subject_kind": "thesis",
            "subject_id": proposal.thesis_id,
            "verdict": self.verdict,
            "confidence": float(self.confidence),
            "concerns": list(self.concerns),
            "kill_conditions": list(self.kill_conditions),
            "grounding_refs": [f"thesis:{proposal.thesis_id}"],
            "free_text": self.free_text or "(mock persona output)",
        }


def run_intake(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    hypothesis: HypothesisProposal,
    research_lead_runner: PersonaRunnerT,
    risk_validator_runner: PersonaRunnerT,
    policy_hash: str,
    feature_snapshot_id: str,
    operator_override: bool = False,
    block_confidence_threshold: float = 0.7,
    now: Optional[dt.datetime] = None,
) -> IntakeResult:
    """Run the adversarial pair. Persists both transcripts to
    strategy_decision; returns the intake verdict.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    rl_out = research_lead_runner(hypothesis)
    rv_out = risk_validator_runner(hypothesis)

    # Schema-validate; reject the call if either side is malformed.
    rl_valid, rl_errs = validate_persona_output(rl_out)
    rv_valid, rv_errs = validate_persona_output(rv_out)
    if not (rl_valid and rv_valid):
        errs = "; ".join(rl_errs + rv_errs)
        reason = f"persona output schema invalid: {errs}"
        _persist_pair(conn, strategy_id, strategy_ver, hypothesis,
                      rl_out, rv_out, policy_hash, feature_snapshot_id,
                      risk_decision="halt", risk_reason=reason, now=now)
        return IntakeResult(
            accepted=False, hypothesis_hash=hypothesis.hash(),
            research_lead_output=rl_out, risk_validator_output=rv_out,
            reason=reason,
        )

    # Plan §1A block rule.
    rv_blocks_hard = (
        rv_out["verdict"] == "block"
        and float(rv_out["confidence"]) > block_confidence_threshold
        and not operator_override
    )
    if rv_blocks_hard:
        reason = (
            f"risk_validator block (confidence={rv_out['confidence']:.2f} > "
            f"{block_confidence_threshold}); no operator override"
        )
        _persist_pair(conn, strategy_id, strategy_ver, hypothesis,
                      rl_out, rv_out, policy_hash, feature_snapshot_id,
                      risk_decision="halt", risk_reason=reason, now=now)
        return IntakeResult(
            accepted=False, hypothesis_hash=hypothesis.hash(),
            research_lead_output=rl_out, risk_validator_output=rv_out,
            reason=reason,
        )

    reason = "accepted"
    _persist_pair(conn, strategy_id, strategy_ver, hypothesis,
                  rl_out, rv_out, policy_hash, feature_snapshot_id,
                  risk_decision="accept", risk_reason=reason, now=now)
    return IntakeResult(
        accepted=True, hypothesis_hash=hypothesis.hash(),
        research_lead_output=rl_out, risk_validator_output=rv_out,
        reason=reason,
    )


def _persist_pair(
    conn: sqlite3.Connection,
    strategy_id: str,
    strategy_ver: int,
    hypothesis: HypothesisProposal,
    rl_out: Mapping[str, Any],
    rv_out: Mapping[str, Any],
    policy_hash: str,
    feature_snapshot_id: str,
    *,
    risk_decision: str,
    risk_reason: str,
    now: dt.datetime,
) -> None:
    intent_payload = {
        "thesis_id": hypothesis.thesis_id,
        "hypothesis_id": hypothesis.hypothesis_id,
        "hypothesis_hash": hypothesis.hash(),
        "research_lead_v1": dict(rl_out),
        "risk_validator_v1": dict(rv_out),
    }
    write_decision(
        conn,
        strategy_id=strategy_id, strategy_ver=strategy_ver,
        code_hash="hypothesis_intake", config_hash="hypothesis_intake",
        policy_hash=policy_hash,
        feature_snapshot_id=feature_snapshot_id,
        intent=intent_payload,
        risk_decision=risk_decision, risk_reason=risk_reason,
        emitted_client_order_id=None,
        now=now,
    )


__all__ = [
    "HypothesisProposal",
    "IntakeResult",
    "MockPersonaRunner",
    "PersonaRunnerT",
    "run_intake",
]
