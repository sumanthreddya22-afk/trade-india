"""Three-way adversarial risk review for proposed orders.

Pattern adapted from TauricResearch/TradingAgents'
``agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py`` plus
the portfolio_manager judge: three free-text reviewers attack the trade
from different stances, then a structured judge decides whether to
override.

This is an *opt-in* gate. Live scanners typically evaluate dozens of
symbols per cadence cycle; debating every order is cost-prohibitive at
Opus rates. Callers should consult :func:`should_debate` (or their own
predicate) and only run the debate when the operational signal warrants
the four extra LLM calls — typical triggers:

  - ``state.consecutive_losing_days >= 2``
  - ``state.size_multiplier < 1.0`` (risk system already throttling)
  - per-trade VaR is high relative to portfolio

**Fail-open contract**: any error path returns ``None``. Callers MUST
treat None as "no override" — i.e., proceed with the existing risk-gate
verdict. Adding this gate must never block a trade that the existing
flow would have allowed when the LLM is unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
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
class RiskDebateVerdict:
    recommendation: Literal["place", "reject"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    aggressive_text: str
    conservative_text: str
    neutral_text: str


class _RiskJudgeOutput(BaseModel):
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(min_length=20, max_length=400)
    recommendation: Literal["place", "reject"] = Field(
        description="'place' to allow the order; 'reject' to block it."
    )


_JUDGE_TOOL_SCHEMA = _RiskJudgeOutput.model_json_schema()


_AGGRESSIVE_SYSTEM = """You are the AGGRESSIVE risk reviewer. The trade \
proposal below has already passed the deterministic risk gates. Argue \
for placing it.

Focus on: edge worth capturing, opportunity cost of skipping, any \
asymmetry where the upside outsizes the loss case. Be specific about \
the entry conditions and the strategy's stated edge."""


_CONSERVATIVE_SYSTEM = """You are the CONSERVATIVE risk reviewer. The \
trade proposal below has passed the deterministic risk gates but there \
are warning signs in the operational context (recent losing streak, \
throttled size multiplier, elevated VaR, etc.).

Argue for skipping the trade. Focus on: capital preservation under \
adverse regime conditions, recent lessons that echo this setup, \
sequence-risk from a thin streak. Be specific."""


_NEUTRAL_SYSTEM = """You are the NEUTRAL risk reviewer. You read the \
aggressive and conservative cases and give a balanced read.

State explicitly which side has the stronger argument given the \
specific operational context, and what additional information would \
flip your view. Keep it short and structured."""


_JUDGE_SYSTEM = """You are the Risk Judge. The deterministic risk gates \
have already passed; you are the optional adversarial-review layer. \
Read the three reviewer notes and decide whether to OVERRIDE the gate \
verdict and reject the trade.

Output via the ``cast_risk_verdict`` tool. Recommend "reject" only \
when the conservative case identifies a concrete, immediate risk that \
the aggressive case fails to answer. Otherwise recommend "place" — the \
deterministic gates are the primary defence and the operator is paying \
LLM cost for a *second opinion*, not a veto-by-default."""


def should_debate(
    *,
    consecutive_losing_days: int,
    size_multiplier: Decimal | float | None,
    trade_var: Decimal | float | None = None,
    trade_var_threshold: float = 0.005,
    losing_days_threshold: int = 2,
    size_multiplier_threshold: float = 1.0,
) -> bool:
    """Default trigger predicate: True iff the operational context is
    'borderline' enough to justify four extra LLM calls.

    Triggers:
      - ``consecutive_losing_days >= losing_days_threshold``
      - OR ``size_multiplier < size_multiplier_threshold`` (risk system
        already throttling position size)
      - OR ``trade_var >= trade_var_threshold`` (large per-trade VaR)
    """
    if consecutive_losing_days >= losing_days_threshold:
        return True
    if size_multiplier is not None:
        try:
            if float(size_multiplier) < size_multiplier_threshold:
                return True
        except (TypeError, ValueError):
            pass
    if trade_var is not None:
        try:
            if float(trade_var) >= trade_var_threshold:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _order_brief(
    *,
    symbol: str,
    action: str,
    qty,
    entry_price,
    stop_loss_price,
    strategy: str,
    regime: str,
    consecutive_losing_days: int,
    size_multiplier,
    trade_var,
    lessons_block: str,
    extra_context: str,
) -> str:
    return (
        f"PROPOSED ORDER\n"
        f"  symbol:           {symbol}\n"
        f"  action:           {action}\n"
        f"  qty:              {qty}\n"
        f"  entry_price:      {entry_price}\n"
        f"  stop_loss_price:  {stop_loss_price}\n"
        f"  strategy:         {strategy}\n"
        f"  regime:           {regime}\n"
        f"\nOPERATIONAL CONTEXT\n"
        f"  consecutive_losing_days: {consecutive_losing_days}\n"
        f"  size_multiplier:         {size_multiplier}\n"
        f"  trade_var:               {trade_var}\n"
        f"\nPRIOR LESSONS\n{lessons_block or '(none)'}\n"
        f"\nEXTRA CONTEXT\n{extra_context or '(none)'}\n"
    )


def run_risk_debate(
    engine,
    *,
    symbol: str,
    action: str,
    qty,
    entry_price,
    stop_loss_price,
    strategy: str,
    regime: str,
    consecutive_losing_days: int,
    size_multiplier=None,
    trade_var=None,
    lessons_block: str = "",
    extra_context: str = "",
    role_name: str = "risk_debate",
    max_turn_tokens: int = 500,
    max_judge_tokens: int = 350,
) -> RiskDebateVerdict | None:
    """Run the four-call aggressive/conservative/neutral/judge sequence.

    Returns ``None`` on any error path so the caller falls back to the
    deterministic risk-gate verdict.
    """
    try:
        client = AnthropicClient(
            role_name=role_name, model=default_architect_model(), engine=engine
        )
    except AnthropicCredsMissingError:
        log.info("risk_debate: skipped (no anthropic creds)")
        return None

    brief = _order_brief(
        symbol=symbol, action=action, qty=qty,
        entry_price=entry_price, stop_loss_price=stop_loss_price,
        strategy=strategy, regime=regime,
        consecutive_losing_days=consecutive_losing_days,
        size_multiplier=size_multiplier, trade_var=trade_var,
        lessons_block=lessons_block, extra_context=extra_context,
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
            f"{brief}\n\nAGGRESSIVE:\n{aggressive.text}\n\n"
            f"CONSERVATIVE:\n{conservative.text}\n"
        )
        neutral = client.complete(
            system=_NEUTRAL_SYSTEM,
            messages=[{"role": "user", "content": neutral_user}],
            max_tokens=max_turn_tokens,
        )
        judge_user = (
            f"{brief}\n\nAGGRESSIVE:\n{aggressive.text}\n\n"
            f"CONSERVATIVE:\n{conservative.text}\n\n"
            f"NEUTRAL:\n{neutral.text}\n"
        )
        judge = client.complete_structured(
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": judge_user}],
            tool_name="cast_risk_verdict",
            tool_description="Cast the final risk-debate verdict.",
            tool_schema=_JUDGE_TOOL_SCHEMA,
            max_tokens=max_judge_tokens,
        )
    except BudgetExceededError:
        log.info("risk_debate: skipped (budget halt)")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("risk_debate: SDK error, failing open: %s", e)
        return None

    if not (judge.used_structured and judge.data):
        log.warning("risk_debate: judge returned free text only, failing open")
        return None
    try:
        v = _RiskJudgeOutput.model_validate(judge.data)
    except Exception as e:  # noqa: BLE001
        log.warning("risk_debate: judge schema mismatch, failing open: %s", e)
        return None
    verdict = RiskDebateVerdict(
        recommendation=v.recommendation,
        confidence=v.confidence,
        reason=v.reason,
        aggressive_text=aggressive.text,
        conservative_text=conservative.text,
        neutral_text=neutral.text,
    )
    # Real-time bus emit (Phase 2). Note: risk_debate doesn't see all the
    # caller's context (qty, entry, etc.) at the verdict callsite; we
    # emit the load-bearing fields. Consumer joins by symbol if needed.
    try:
        from trading_bot.event_bus import bus as _bus
        _bus.emit(
            "debate.risk.completed",
            {
                "symbol": symbol, "action": action,
                "verdict": verdict.recommendation,
                "confidence": verdict.confidence,
                "strategy": strategy, "regime": regime,
            },
            source="risk_debate",
        )
    except Exception:
        pass
    return verdict
