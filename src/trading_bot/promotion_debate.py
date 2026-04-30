"""Bull/Bear adversarial debate gate for lab→paper promotions.

Pattern adapted from TauricResearch/TradingAgents'
``agents/researchers/{bull,bear}_researcher.py`` plus
``agents/managers/research_manager.py``: two symmetric arguments are made
about a candidate, then a third LLM call judges whether the bear case was
adequately answered.

This is a *gate*, not a generator: it is consulted ONLY after the existing
fitness + delta gates have already cleared. Cost per promotion is ~3 LLM
calls (bull, bear, judge). The judge is forced to use a structured tool,
the bull/bear are free text (kept short by ``max_tokens``).

**Fail-open contract**: any error path — missing creds, budget halt, SDK
exception, malformed judge output — returns ``None`` so the caller treats
the debate as inconclusive and falls back to the prior behaviour
(promote). Adding this gate must never *block* a promotion that the
existing logic would have allowed when the LLM is unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from trading_bot.anthropic_client import (
    AnthropicClient,
    AnthropicCredsMissingError,
    BudgetExceededError,
    default_architect_model,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebateVerdict:
    recommendation: Literal["promote", "block"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    bull_text: str
    bear_text: str


class _JudgeOutput(BaseModel):
    bear_addressed: bool = Field(
        description="Did the bull case adequately answer the bear's strongest concerns?"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Judge's confidence in the recommendation."
    )
    reason: str = Field(
        min_length=20,
        max_length=400,
        description="2-4 sentences explaining the verdict.",
    )
    recommendation: Literal["promote", "block"] = Field(
        description="'promote' if the candidate should be advanced; "
        "'block' if the bear case is materially unaddressed."
    )


_JUDGE_TOOL_SCHEMA = _JudgeOutput.model_json_schema()


_BULL_SYSTEM = """You are the BULL researcher in a promotion review for an \
autonomous trading system. A candidate strategy variant has cleared the \
backtest fitness and 10% delta gates. Make the strongest possible case \
for promoting it to paper trading.

Argue from the leaderboard metrics, the params, and any prior lessons \
attached. Be specific. Cite which regime the candidate exploits and why \
the metrics support live deployment. Do NOT hedge — the BEAR will get the \
last word."""


_BEAR_SYSTEM = """You are the BEAR researcher in a promotion review for an \
autonomous trading system. The BULL has just argued for promoting a \
candidate strategy. Attack the case.

Focus on overfitting risk, regime-mismatch, drawdown shape, sample \
adequacy, and any pattern the prior-lessons data shows that this \
candidate would re-enact. Cite specific numbers from the candidate or \
lessons. End with the single most damaging question the bull failed to \
answer."""


_JUDGE_SYSTEM = """You are the Research Judge for a promotion review. \
You read the BULL case and the BEAR rebuttal. Decide whether the bear's \
strongest concerns were addressed by the bull.

Output via the ``cast_verdict`` tool. Recommend "block" if the bear \
identified a materially unaddressed risk (overfitting, regime mismatch, \
or a pattern echoed in prior lessons). Recommend "promote" if the bull \
case stands. Use "high" confidence only when the metrics or lessons \
provide concrete grounding; "low" when the case is ambiguous."""


def _candidate_brief(candidate, leaderboard_context: str, lessons_block: str) -> str:
    return (
        f"CANDIDATE\n"
        f"  template:        {candidate.template}\n"
        f"  fitness:         {candidate.fitness}\n"
        f"  alpha_vs_spy_x:  {candidate.alpha_vs_spy_x}\n"
        f"  sortino:         {candidate.sortino}\n"
        f"  max_dd_pct:      {candidate.max_dd_pct}\n"
        f"  params:          {candidate.params}\n"
        f"\nLEADERBOARD CONTEXT\n{leaderboard_context or '(none)'}\n"
        f"\nPRIOR LESSONS\n{lessons_block or '(none)'}\n"
    )


def run_promotion_debate(
    engine,
    candidate,
    *,
    leaderboard_context: str = "",
    lessons_block: str = "",
    role_name: str = "promotion_debate",
    max_bull_tokens: int = 700,
    max_bear_tokens: int = 700,
    max_judge_tokens: int = 400,
) -> DebateVerdict | None:
    """Run the three-call bull/bear/judge sequence. Returns ``None`` on any
    error path — the caller MUST treat None as "promote as before"."""
    try:
        client = AnthropicClient(
            role_name=role_name, model=default_architect_model(), engine=engine
        )
    except AnthropicCredsMissingError:
        log.info("promotion_debate: skipped (no anthropic creds)")
        return None

    brief = _candidate_brief(candidate, leaderboard_context, lessons_block)
    try:
        bull = client.complete(
            system=_BULL_SYSTEM,
            messages=[{"role": "user", "content": brief}],
            max_tokens=max_bull_tokens,
        )
        bear_user = (
            f"The BULL case follows. Attack it.\n\n{brief}\n\n"
            f"BULL CASE:\n{bull.text}"
        )
        bear = client.complete(
            system=_BEAR_SYSTEM,
            messages=[{"role": "user", "content": bear_user}],
            max_tokens=max_bear_tokens,
        )
        judge_user = (
            f"{brief}\n\nBULL CASE:\n{bull.text}\n\nBEAR REBUTTAL:\n{bear.text}\n"
        )
        judge = client.complete_structured(
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": judge_user}],
            tool_name="cast_verdict",
            tool_description="Cast the final verdict on the promotion debate.",
            tool_schema=_JUDGE_TOOL_SCHEMA,
            max_tokens=max_judge_tokens,
        )
    except BudgetExceededError:
        log.info("promotion_debate: skipped (budget halt)")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("promotion_debate: SDK error, failing open: %s", e)
        return None

    if judge.used_structured and judge.data:
        try:
            verdict_data = _JudgeOutput.model_validate(judge.data)
        except Exception as e:  # noqa: BLE001
            log.warning("promotion_debate: judge schema mismatch, failing open: %s", e)
            return None
        return DebateVerdict(
            recommendation=verdict_data.recommendation,
            confidence=verdict_data.confidence,
            reason=verdict_data.reason,
            bull_text=bull.text,
            bear_text=bear.text,
        )
    log.warning("promotion_debate: judge returned free text only, failing open")
    return None
