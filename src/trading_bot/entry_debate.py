"""Pre-trade entry committee — 4-LLM debate before every order placement.

Mirror of :mod:`trading_bot.unblock_debate` but for the *positive* gate:
``unblock_debate`` runs after a deterministic gate REJECTS and may force
an OVERRIDE; ``entry_debate`` runs after every BUY signal that PASSES
the deterministic gates and decides whether to PLACE the order or SKIP.

Use case (Phase 6): the desired flow is
``intel/news → process → debate → place or skip``. The orchestrator
hands the committee a per-symbol brief (intel score, top-source headline,
indicators, regime, signal reason, per-trade VaR, recent loss streak);
the committee returns ``"place"`` or ``"skip"``.

Fail-SOFT contract:
    Any error path — credentials missing, budget halted, mailbox timeout,
    SDK exception, judge schema mismatch — returns ``None``. Callers MUST
    treat ``None`` as "skip the trade AND queue an operator alert" — i.e.,
    do NOT place the order, but do let the operator know the LLM gate is
    unreachable. Same posture choice as the user's plan-time decision.

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
class EntryDebateVerdict:
    """Verdict from the entry committee.

    ``recommendation``:
        * ``"place"`` — proceed with the order
        * ``"skip"`` — do NOT submit (operator-visible reject_by_entry_debate)
    """
    recommendation: Literal["place", "skip"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    aggressive_text: str
    conservative_text: str
    neutral_text: str


class _EntryJudgeOutput(BaseModel):
    recommendation: Literal["place", "skip"] = Field(
        description=(
            "REQUIRED. 'place' to submit the order; 'skip' to abort. "
            "The deterministic risk and intel gates already passed — your "
            "vote decides whether the LLM committee endorses entry."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="REQUIRED. 'high'|'medium'|'low' confidence in the recommendation."
    )
    reason: str = Field(
        default="",
        max_length=4000,
        description=(
            "REQUIRED. 1-3 sentences explaining the verdict: which "
            "reviewer's case carried, and the load-bearing fact. Audit "
            "trail depends on this — do not omit."
        ),
    )


_JUDGE_TOOL_SCHEMA = _EntryJudgeOutput.model_json_schema()


_AGGRESSIVE_SYSTEM = """You are the AGGRESSIVE reviewer. The trade below \
PASSED the deterministic risk gate and is about to be submitted. Argue \
for PLACING the order — the news/intel signal earned this entry.

Focus on:
  - The intel score and top-source headline — what's the actual catalyst?
  - Indicator alignment (RSI in a healthy band, MACD crossover, close > EMA)
  - Asymmetry: this is a BUY at the strategy's preferred entry, not a chase
  - Cost of inaction: a high-conviction signal you don't act on is alpha given away
  - Position-sizing is already capped by per-trade-risk; the downside is bounded

Do NOT argue against the strategy itself or the operator's overall risk \
posture. Argue for THIS specific entry, this scan."""


_CONSERVATIVE_SYSTEM = """You are the CONSERVATIVE reviewer. The trade \
passed deterministic gates but is about to be submitted on news + a \
technical signal. Argue for SKIPPING.

Focus on:
  - Is the intel headline actually a catalyst for the trade direction, \
    or just noise (rumor, recycled story, generic mention)?
  - News-driven entries often buy the top: by the time we read it, the \
    move may have happened
  - Crowded trade risk: if the news is everywhere, the contrarian read \
    may be that the easy money is gone
  - Macro regime: even good news loses in a risk-off tape
  - Idiosyncratic catalysts to come (earnings, rate decision, regulatory) \
    that could swamp this entry's edge

Default position: skip unless the entry has a CONCRETE, identifiable edge. \
Most entries should not be debated; if it's borderline enough to debate, \
it's borderline enough to skip."""


_NEUTRAL_SYSTEM = """You are the NEUTRAL reviewer. Read the AGGRESSIVE \
and CONSERVATIVE cases and give a structured balanced read.

State explicitly:
  1. Which side has the stronger argument given the SPECIFIC intel score, \
     headline, indicators, and regime
  2. What additional information would flip your view
  3. Whether this entry has a concrete identifiable edge, or fits the \
     pattern of "looks attractive in isolation"

Keep it short and structured."""


_JUDGE_SYSTEM = """You are the Entry Judge. The deterministic risk gate \
PASSED the trade; you are the LLM committee's final say on whether to \
actually place it.

Output via the ``cast_entry_verdict`` tool.

DEFAULT POSITION: lean toward ``"place"`` ONLY when the entry has an \
identifiable edge supported by intel + technicals together. When in \
doubt, prefer ``"skip"`` — a missed entry is a much smaller cost than \
a forced entry into noise.

Recommend ``"place"`` when:
  1. The intel headline is a real catalyst pointing in the trade direction
  2. The indicators corroborate (it's not a chase against a broken trend)
  3. The neutral reviewer agrees the edge is identifiable, not generic

Recommend ``"skip"`` when:
  - Intel is generic (mentions without catalyst), recycled, or directionally \
    ambiguous
  - The aggressive case is mostly "the strategy says BUY" — that's already \
    encoded; you add value by checking the qualitative read
  - Operational context (loss streak, throttled sizing) suggests caution

Confidence ``"high"`` means all of the above clearly hold. Confidence \
``"low"`` for marginal cases either way.

Failure modes to AVOID:
  - Drifting to "place" because the technicals look clean. If the intel \
    is noise, clean technicals do not save the entry.
  - Treating every signal as exceptional. Most entries are average; the \
    judge's job is to filter for the genuinely above-average ones."""


def should_entry_debate(
    *,
    daily_debate_count: int,
    daily_cap: int = 50,
) -> bool:
    """Predicate: only fire the debate when we're under the daily-cost cap.

    Unlike unblock_debate's predicate (which gates by overage / score), the
    entry debate fires on every BUY signal that passes risk — the only
    natural cost guard is the daily cap.

    Args:
        daily_debate_count: how many entry debates have already fired today
        daily_cap: hard upper bound to bound mailbox queue + Anthropic spend
    """
    if daily_cap <= 0:
        return False
    return daily_debate_count < daily_cap


def _entry_brief(
    *,
    proposal_summary: str,
    intel_score: float | None,
    intel_top_reason: str,
    signal_reason: str,
    regime: str,
    indicators: str,
    operational_context: str,
    lessons_block: str,
    extra_context: str,
) -> str:
    score_str = (
        f"{intel_score:.2f}" if intel_score is not None else "(none)"
    )
    return (
        f"PROPOSED ENTRY (passed deterministic risk gate)\n"
        f"{proposal_summary}\n"
        f"\nINTEL CONTEXT\n"
        f"  intel_score:    {score_str}\n"
        f"  top_reason:     {intel_top_reason or '(none)'}\n"
        f"  signal_reason:  {signal_reason or '(none)'}\n"
        f"  regime:         {regime or '(unknown)'}\n"
        f"\nINDICATORS\n{indicators or '(none)'}\n"
        f"\nOPERATIONAL CONTEXT\n{operational_context or '(none)'}\n"
        f"\nPRIOR LESSONS (filtered to entry-class)\n"
        f"{lessons_block or '(none)'}\n"
        f"\nEXTRA CONTEXT\n{extra_context or '(none)'}\n"
    )


def run_entry_debate(
    engine,
    *,
    proposal_summary: str,
    intel_score: float | None = None,
    intel_top_reason: str = "",
    signal_reason: str = "",
    regime: str = "",
    indicators: str = "",
    operational_context: str = "",
    lessons_block: str = "",
    extra_context: str = "",
    role_name: str = "entry_debate",
    max_turn_tokens: int = 500,
    max_judge_tokens: int = 400,
    use_mailbox: bool = True,
    mailbox_timeout_seconds: float = 600.0,
) -> EntryDebateVerdict | None:
    """Run the four-call aggressive/conservative/neutral/judge sequence.

    **Fail-soft**: returns ``None`` on ANY error — credentials missing,
    budget halt, SDK error, judge schema mismatch. Callers MUST treat
    ``None`` as "skip the trade AND queue an alert".
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
        log.info("entry_debate: skipped (no anthropic creds) — fail soft")
        return None

    brief = _entry_brief(
        proposal_summary=proposal_summary,
        intel_score=intel_score,
        intel_top_reason=intel_top_reason,
        signal_reason=signal_reason,
        regime=regime,
        indicators=indicators,
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
            f"{brief}\n\nAGGRESSIVE (argues for placing):\n{aggressive.text}\n\n"
            f"CONSERVATIVE (argues for skipping):\n{conservative.text}\n"
        )
        neutral = client.complete(
            system=_NEUTRAL_SYSTEM,
            messages=[{"role": "user", "content": neutral_user}],
            max_tokens=max_turn_tokens,
        )
        judge_user = (
            f"{brief}\n\nAGGRESSIVE (argues for placing):\n{aggressive.text}\n\n"
            f"CONSERVATIVE (argues for skipping):\n{conservative.text}\n\n"
            f"NEUTRAL:\n{neutral.text}\n"
        )
        judge = client.complete_structured(
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": judge_user}],
            tool_name="cast_entry_verdict",
            tool_description=(
                "Cast the final entry-debate verdict. 'place' means "
                "submit the order; 'skip' means abort the entry."
            ),
            tool_schema=_JUDGE_TOOL_SCHEMA,
            max_tokens=max_judge_tokens,
        )
    except BudgetExceededError:
        log.info("entry_debate: skipped (budget halt) — fail soft")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("entry_debate: SDK error, failing soft: %s", e)
        return None

    if not (judge.used_structured and judge.data):
        log.warning("entry_debate: judge returned free text only, failing soft")
        return None
    try:
        v = _EntryJudgeOutput.model_validate(judge.data)
    except Exception as e:  # noqa: BLE001
        log.warning("entry_debate: judge schema mismatch, failing soft: %s", e)
        return None

    final_reason = v.reason.strip() if v.reason else ""
    if not final_reason:
        neutral_excerpt = (neutral.text or "").strip().replace("\n", " ")
        if neutral_excerpt:
            final_reason = (
                "(judge omitted reason; synthesized from neutral reviewer) "
                + neutral_excerpt[:600]
            )
        else:
            final_reason = "(judge omitted reason; no neutral text either)"

    return EntryDebateVerdict(
        recommendation=v.recommendation,
        confidence=v.confidence,
        reason=final_reason,
        aggressive_text=aggressive.text,
        conservative_text=conservative.text,
        neutral_text=neutral.text,
    )


def count_todays_entry_debates(engine) -> int:
    """Return today's entry-debate row count from ``entry_debate_runs``.

    Used by the orchestrator to gate firing via ``should_entry_debate``.
    Defensive on schema/connection errors — returns 0 so the gate
    fail-opens to "yes you may debate" rather than skipping every trade
    when the audit table is missing.
    """
    import datetime as _dt
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    try:
        from trading_bot.state_db import EntryDebateRun
        today_start = _dt.datetime.combine(
            _dt.date.today(), _dt.time.min, tzinfo=_dt.timezone.utc,
        )
        with Session(engine) as s:
            count = s.execute(
                select(func.count(EntryDebateRun.id))
                .where(EntryDebateRun.run_at >= today_start)
            ).scalar_one()
        return int(count or 0)
    except Exception:
        return 0
