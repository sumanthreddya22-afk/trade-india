"""Adversarial debate to override a deterministic risk gate's rejection.

Mirror of :mod:`trading_bot.risk_debate` but in the opposite direction:
``risk_debate`` runs *after* gates pass and may add an extra REJECT;
``unblock_debate`` runs *after* gates reject and may force an OVERRIDE.

Use case (Phase 5): the wheel runner refuses to place a CSP because the
proposed collateral exceeds ``options_max_pct`` or ``sector_cap_pct``.
For borderline rejections on high-conviction candidates (rich premium,
elevated IV, post-earnings, no near-term catalysts), an LLM committee
re-reads the operator's stated risk preference and may decide the
specific candidate is worth overriding the cap.

Fail-CLOSED contract (opposite of risk_debate's fail-open):
    Any error path — credentials missing, budget exceeded, SDK exception,
    judge schema mismatch — returns ``None``. Callers MUST treat ``None``
    as "no override" — i.e., the original gate rejection stands. Adding
    this layer must NEVER place a trade the deterministic flow would
    have blocked when the LLM is unavailable.

Wired by default to :class:`trading_bot.mailbox_backed_client.MailboxBackedClient`
so debates fire through the Claude Code subscription routine instead of
the API key. Falls back to direct API on mailbox timeout.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError,
    BudgetExceededError,
    default_architect_model,
)
from trading_bot.mailbox_backed_client import (
    MailboxBackedClient,
    MailboxRouting,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnblockVerdict:
    """Verdict from the unblock committee.

    ``recommendation``:
        * ``"place"`` — override the deterministic gate; submit the order
        * ``"reject"`` — respect the gate; do NOT submit
    """
    recommendation: Literal["place", "reject"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    aggressive_text: str
    conservative_text: str
    neutral_text: str


class _UnblockJudgeOutput(BaseModel):
    confidence: Literal["high", "medium", "low"]
    # Generous upper bound — Opus often writes 600-1500 char reasons
    # explaining the override / respect call. We want the full reasoning
    # in the audit trail; truncation belongs in the dashboard, not here.
    reason: str = Field(min_length=20, max_length=4000)
    recommendation: Literal["place", "reject"] = Field(
        description=(
            "'place' to OVERRIDE the deterministic gate's rejection and "
            "submit the order; 'reject' to RESPECT the gate and skip."
        )
    )


_JUDGE_TOOL_SCHEMA = _UnblockJudgeOutput.model_json_schema()


_AGGRESSIVE_SYSTEM = """You are the AGGRESSIVE reviewer. The trade below was \
REJECTED by a deterministic risk-cap gate (e.g. options collateral cap, \
sector concentration cap, or strategy fallback flag). Argue for OVERRIDING \
the gate and placing the order anyway.

Focus on:
  - Why this specific candidate is high-conviction enough to break the rule
    (rich premium, elevated IV, post-earnings IV settle, identifiable edge)
  - Magnitude of the overage — borderline (within 50% over cap) is more
    defensible than 2-3x over
  - Asymmetry of outcomes for THIS trade vs. the operator's general
    portfolio-construction preference encoded in the cap
  - Operational state that justifies an exception (testing/validation,
    intentional concentration, recent regime shift)

Do NOT argue for systematically lowering the cap — that's a separate
operator decision. Argue only for THIS specific candidate, this scan."""


_CONSERVATIVE_SYSTEM = """You are the CONSERVATIVE reviewer. The trade was \
REJECTED by a deterministic risk-cap gate. Argue for RESPECTING the gate \
and skipping.

Focus on:
  - The cap was set deliberately by the operator for portfolio safety; \
    it's not a soft preference
  - Concentration risk: a single position at the proposed size means a \
    bad tape concentrates losses dramatically
  - Tail-risk: rich-premium candidates often carry hidden catalyst risk \
    (post-earnings does NOT mean catalyst-free)
  - Slippery-slope: if we override today, we override tomorrow, and the \
    cap stops being a real constraint
  - Sequence risk: if the override loses big, the operator's whole \
    risk-management story is undermined

Default position: caps exist to be respected. Override should be the \
exception, defended by specifics, not the rule."""


_NEUTRAL_SYSTEM = """You are the NEUTRAL reviewer. Read the AGGRESSIVE and \
CONSERVATIVE cases and give a structured balanced read.

State explicitly:
  1. Which side has the stronger argument given the SPECIFIC magnitude \
     of overage and the SPECIFIC candidate quality
  2. What additional information would flip your view
  3. Whether this case meets the bar for "exceptional override" or fits \
     the pattern of "every candidate looks important"

Keep it short and structured."""


_JUDGE_SYSTEM = """You are the Unblock Judge. The deterministic risk-cap \
gate rejected the trade; you are the optional adversarial-review layer \
that may force an override.

Output via the ``cast_unblock_verdict`` tool.

DEFAULT POSITION: recommend ``"reject"`` — respect the gate. The cap is \
the operator's primary risk control; you are not its veto. Recommend \
``"place"`` (override) ONLY when ALL of:

  1. Aggressive identifies a CONCRETE, SPECIFIC reason this candidate is \
     high-conviction beyond average (not generic "rich premium")
  2. Conservative's concerns are answered by the specifics — not just \
     waved away
  3. The overage is borderline (typically within ~50% of the cap), not \
     a 2-3x violation
  4. The neutral reviewer agrees the case is genuinely exceptional

Confidence ``"high"`` means all four above clearly hold. Confidence \
``"low"`` means you're recommending ``"reject"`` because the case for \
override is generic or the overage is large.

Failure modes to AVOID:
  - Drifting toward "place" because the trade looks attractive in \
    isolation. Every rejected trade looks attractive in isolation; \
    that's why the cap exists.
  - Rationalising overrides on accumulated small justifications. The \
    case must be load-bearing on a single specific edge."""


def should_unblock_debate(
    *,
    rejection_reason: str,
    rejection_overage_ratio: float,
    candidate_score: float,
    daily_debate_count: int,
    max_overage_ratio: float = 0.50,
    min_score: float = 7.0,
    daily_cap: int = 15,
) -> bool:
    """Predicate: only debate borderline rejections on high-conviction picks.

    Args:
        rejection_reason: short reason from the gate (informational only)
        rejection_overage_ratio: how far over the cap the proposed trade
            is, as a ratio. 0.0 = at cap, 0.50 = 50% over cap (e.g.
            cap=20%, proposed=30%), 1.00 = 2x cap. Anything above
            ``max_overage_ratio`` is too far gone to debate.
        candidate_score: 0-10 score blending IV rank, premium yield,
            sentiment, etc. Below ``min_score`` we don't bother debating.
        daily_debate_count: how many unblock debates have already fired
            today. Hard cap to bound LLM cost.
        max_overage_ratio: gate by how borderline the rejection is
        min_score: gate by candidate conviction
        daily_cap: gate by total daily debate budget
    """
    if rejection_overage_ratio > max_overage_ratio:
        return False
    if candidate_score < min_score:
        return False
    if daily_debate_count >= daily_cap:
        return False
    return True


def _unblock_brief(
    *,
    proposal_summary: str,
    block_reason: str,
    overage_ratio: float,
    fundamentals: str,
    operational_context: str,
    lessons_block: str,
    extra_context: str,
) -> str:
    return (
        f"PROPOSED ORDER (rejected by deterministic gate)\n"
        f"{proposal_summary}\n"
        f"\nGATE REJECTION\n"
        f"  reason:        {block_reason}\n"
        f"  overage_ratio: {overage_ratio:.2f}  "
        f"(0.00 = at cap, 0.50 = 50% over)\n"
        f"\nCANDIDATE FUNDAMENTALS\n{fundamentals or '(none)'}\n"
        f"\nOPERATIONAL CONTEXT\n{operational_context or '(none)'}\n"
        f"\nPRIOR LESSONS (filtered to unblock-override class)\n"
        f"{lessons_block or '(none)'}\n"
        f"\nEXTRA CONTEXT\n{extra_context or '(none)'}\n"
    )


def run_unblock_debate(
    engine,
    *,
    proposal_summary: str,
    block_reason: str,
    overage_ratio: float,
    fundamentals: str = "",
    operational_context: str = "",
    lessons_block: str = "",
    extra_context: str = "",
    role_name: str = "unblock_debate",
    max_turn_tokens: int = 500,
    max_judge_tokens: int = 400,
    use_mailbox: bool = True,
    mailbox_timeout_seconds: float = 600.0,
) -> UnblockVerdict | None:
    """Run the four-call aggressive/conservative/neutral/judge sequence.

    **Fail-closed**: returns ``None`` on ANY error — credentials missing,
    budget halt, SDK error, judge schema mismatch. Callers MUST treat
    ``None`` as "respect the gate" — i.e., do NOT place the order.
    """
    try:
        client = MailboxBackedClient(
            role_name=role_name,
            model=default_architect_model(),
            engine=engine,
            routing=MailboxRouting(
                enabled=use_mailbox,
                timeout_seconds=mailbox_timeout_seconds,
                model_class="judge",
            ),
        )
    except AnthropicCredsMissingError:
        log.info("unblock_debate: skipped (no anthropic creds) — fail closed")
        return None

    brief = _unblock_brief(
        proposal_summary=proposal_summary,
        block_reason=block_reason,
        overage_ratio=overage_ratio,
        fundamentals=fundamentals,
        operational_context=operational_context,
        lessons_block=lessons_block,
        extra_context=extra_context,
    )

    try:
        aggressive = client.complete(
            system=_AGGRESSIVE_SYSTEM,
            messages=[{"role": "user", "content": brief}],
            max_tokens=max_turn_tokens,
        )
        conservative = client.complete(
            system=_CONSERVATIVE_SYSTEM,
            messages=[{"role": "user", "content": brief}],
            max_tokens=max_turn_tokens,
        )
        neutral_user = (
            f"{brief}\n\nAGGRESSIVE (argues for override):\n{aggressive.text}\n\n"
            f"CONSERVATIVE (argues for respecting cap):\n{conservative.text}\n"
        )
        neutral = client.complete(
            system=_NEUTRAL_SYSTEM,
            messages=[{"role": "user", "content": neutral_user}],
            max_tokens=max_turn_tokens,
        )
        judge_user = (
            f"{brief}\n\nAGGRESSIVE (argues for override):\n{aggressive.text}\n\n"
            f"CONSERVATIVE (argues for respecting cap):\n{conservative.text}\n\n"
            f"NEUTRAL:\n{neutral.text}\n"
        )
        judge = client.complete_structured(
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": judge_user}],
            tool_name="cast_unblock_verdict",
            tool_description=(
                "Cast the final unblock-debate verdict. 'place' means "
                "OVERRIDE the deterministic gate and submit the order; "
                "'reject' means RESPECT the gate and skip."
            ),
            tool_schema=_JUDGE_TOOL_SCHEMA,
            max_tokens=max_judge_tokens,
        )
    except BudgetExceededError:
        log.info("unblock_debate: skipped (budget halt) — fail closed")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("unblock_debate: SDK error, failing closed: %s", e)
        return None

    if not (judge.used_structured and judge.data):
        log.warning("unblock_debate: judge returned free text only, failing closed")
        return None
    try:
        v = _UnblockJudgeOutput.model_validate(judge.data)
    except Exception as e:  # noqa: BLE001
        log.warning("unblock_debate: judge schema mismatch, failing closed: %s", e)
        return None

    return UnblockVerdict(
        recommendation=v.recommendation,
        confidence=v.confidence,
        reason=v.reason,
        aggressive_text=aggressive.text,
        conservative_text=conservative.text,
        neutral_text=neutral.text,
    )
