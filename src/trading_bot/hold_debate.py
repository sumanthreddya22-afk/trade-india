"""Phase C — Hold Debate (Debate #3).

Fires for held positions when the intel that drove the entry decays
(score drop, sentiment flip, fresh adverse 8-K, etc.). Sequential 4-call
LLM committee using named expert personas:

  1. Aggressive  — Position Trader (15yr, held through drawdowns)
  2. Conservative — Trading Desk Risk Manager (20yr, has seen blow-ups)
  3. Neutral     — Trade Book Runner (15yr, capital-efficiency lens)
  4. Judge       — Senior Portfolio Manager (25yr, hold/tighten/exit)

Verdicts:
  ``hold``           → no action (let bracket order run)
  ``tighten_stop``   → cancel existing stop, submit new stop at breakeven
                       or recent swing low (caller decides the level)
  ``exit_now``       → cancel children + market sell

Fail-SOFT contract: any error path returns ``None``. Caller MUST treat
``None`` as "leave the bracket order untouched, queue an alert" — i.e.,
the LLM gate is unreachable and we never auto-exit on infrastructure
failure.

SEQUENTIAL GUARANTEE: aggressive → conservative → neutral → judge are
four back-to-back LLM calls. No parallelism inside the debate.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError,
    BudgetExceededError,
    default_architect_model,
)
from trading_bot.mailbox_backed_client import (
    MailboxBackedClient,
    MailboxRouting,
)
from trading_bot.personas import (
    hold_aggressive, hold_conservative, hold_neutral, hold_judge,
)
from trading_bot.state_db import HoldDebateRun


log = logging.getLogger(__name__)


# Default knobs — overridable via strategy/config.yaml::hold section.
DEFAULT_DAILY_CAP = 30


# ---------------------------------------------------------------------------
# Verdict types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldDebateVerdict:
    """Verdict from the hold committee."""
    recommendation: Literal["hold", "tighten_stop", "exit_now"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    aggressive_text: str
    conservative_text: str
    neutral_text: str


class _HoldJudgeOutput(BaseModel):
    recommendation: Literal["hold", "tighten_stop", "exit_now"] = Field(
        description=(
            "REQUIRED. 'hold' (no action), 'tighten_stop' (move stop up), "
            "or 'exit_now' (flatten position at market)."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="REQUIRED. 'high'|'medium'|'low' confidence in the verdict.",
    )
    reason: str = Field(
        default="",
        max_length=4000,
        description=(
            "REQUIRED. 1-2 sentences citing the load-bearing fact "
            "(specific trigger, specific catalyst inversion, specific "
            "prior lesson). Audit trail depends on this — do not omit."
        ),
    )


_JUDGE_TOOL_SCHEMA = _HoldJudgeOutput.model_json_schema()


# ---------------------------------------------------------------------------
# Predicate + counters
# ---------------------------------------------------------------------------


def should_hold_debate(
    *,
    daily_debate_count: int,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> bool:
    """Predicate: only fire hold debate when we're under the daily cap."""
    if daily_cap <= 0:
        return False
    return daily_debate_count < daily_cap


def count_todays_hold_debates(engine) -> int:
    """Today's row count from ``hold_debate_runs``. Defensive on errors."""
    from sqlalchemy import func, select
    try:
        today_start = dt.datetime.combine(
            dt.date.today(), dt.time.min, tzinfo=dt.timezone.utc,
        )
        with Session(engine) as s:
            count = s.execute(
                select(func.count(HoldDebateRun.id))
                .where(HoldDebateRun.run_at >= today_start)
            ).scalar_one()
        return int(count or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Brief construction
# ---------------------------------------------------------------------------


def _hold_brief(
    *,
    symbol: str,
    asset_class: str,
    qty: float | int,
    entry_price: float,
    current_price: float | None,
    stop_price: float | None,
    take_profit_price: float | None,
    days_held: int,
    unrealized_pnl_usd: float | None,
    unrealized_pnl_pct: float | None,
    entry_thesis: str,
    entry_intel_score: float | None,
    entry_sentiment: float | None,
    entry_top_sources: list[str],
    current_intel_score: float | None,
    current_sentiment: float | None,
    trigger_reason: str,
    new_events_summary: str,
    lessons_block: str,
) -> str:
    def _fnum(x, fmt=":.2f"):
        if x is None:
            return "(none)"
        return ("{" + fmt + "}").format(x)
    sources_str = ", ".join(entry_top_sources) if entry_top_sources else "(none)"
    return (
        f"HELD POSITION UNDER REVIEW (hold-debate trigger fired)\n"
        f"  symbol:               {symbol}\n"
        f"  asset_class:          {asset_class}\n"
        f"  qty:                  {qty}\n"
        f"  entry_price:          {entry_price}\n"
        f"  current_price:        {_fnum(current_price)}\n"
        f"  stop_price:           {_fnum(stop_price)}\n"
        f"  take_profit_price:    {_fnum(take_profit_price)}\n"
        f"  days_held:            {days_held}\n"
        f"  unrealized_pnl_usd:   {_fnum(unrealized_pnl_usd)}\n"
        f"  unrealized_pnl_pct:   {_fnum(unrealized_pnl_pct, ':+.2f')}\n"
        f"\nENTRY THESIS\n  {entry_thesis or '(none)'}\n"
        f"  entry_intel_score:    {_fnum(entry_intel_score)}\n"
        f"  entry_sentiment:      {_fnum(entry_sentiment, ':+.2f')}\n"
        f"  entry_top_sources:    [{sources_str}]\n"
        f"\nTRIGGER\n  trigger_reason:       {trigger_reason}\n"
        f"  current_intel_score:  {_fnum(current_intel_score)}\n"
        f"  current_sentiment:    {_fnum(current_sentiment, ':+.2f')}\n"
        f"  new_events_since_entry:\n{new_events_summary or '  (none)'}\n"
        f"\nPRIOR LESSONS (filtered to hold-class)\n"
        f"{lessons_block or '(none)'}\n"
    )


# ---------------------------------------------------------------------------
# Sequential 4-call debate
# ---------------------------------------------------------------------------


def _persona_version() -> str:
    return (
        f"agg={hold_aggressive.VERSION}"
        f"|cons={hold_conservative.VERSION}"
        f"|neu={hold_neutral.VERSION}"
        f"|judge={hold_judge.VERSION}"
    )


def run_hold_debate(
    engine,
    *,
    symbol: str,
    asset_class: str,
    qty: float | int,
    entry_price: float,
    current_price: float | None = None,
    stop_price: float | None = None,
    take_profit_price: float | None = None,
    days_held: int = 0,
    unrealized_pnl_usd: float | None = None,
    unrealized_pnl_pct: float | None = None,
    entry_thesis: str = "",
    entry_intel_score: float | None = None,
    entry_sentiment: float | None = None,
    entry_top_sources: list[str] | None = None,
    current_intel_score: float | None = None,
    current_sentiment: float | None = None,
    trigger_reason: str = "",
    new_events_summary: str = "",
    lessons_block: str = "",
    role_name: str = "hold_debate",
    max_turn_tokens: int = 500,
    max_judge_tokens: int = 400,
    use_mailbox: bool = True,
    mailbox_timeout_seconds: float = 600.0,
) -> HoldDebateVerdict | None:
    """Sequential 4-call hold debate. Returns verdict or None (fail-soft).

    Caller responsibilities (NOT done here):
      - Apply the verdict (replace_stop / flatten_position)
      - Persist a HoldDebateRun row (use ``persist_run`` helper)
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
        log.info("hold_debate: skipped (no anthropic creds) — fail soft")
        return None

    brief = _hold_brief(
        symbol=symbol, asset_class=asset_class, qty=qty,
        entry_price=entry_price, current_price=current_price,
        stop_price=stop_price, take_profit_price=take_profit_price,
        days_held=days_held,
        unrealized_pnl_usd=unrealized_pnl_usd,
        unrealized_pnl_pct=unrealized_pnl_pct,
        entry_thesis=entry_thesis,
        entry_intel_score=entry_intel_score,
        entry_sentiment=entry_sentiment,
        entry_top_sources=entry_top_sources or [],
        current_intel_score=current_intel_score,
        current_sentiment=current_sentiment,
        trigger_reason=trigger_reason,
        new_events_summary=new_events_summary,
        lessons_block=lessons_block,
    )

    # SEQUENTIAL: aggressive → conservative → neutral → judge.
    try:
        aggressive = client.complete(
            system=hold_aggressive.PROMPT,
            messages=[{"role": "user", "content": brief}],
            max_tokens=max_turn_tokens,
        )
        conservative = client.complete(
            system=hold_conservative.PROMPT,
            messages=[{"role": "user", "content": brief}],
            max_tokens=max_turn_tokens,
        )
        neutral_user = (
            f"{brief}\n\nAGGRESSIVE (position trader, argues HOLD):\n"
            f"{aggressive.text}\n\n"
            f"CONSERVATIVE (risk manager, argues EXIT/TIGHTEN):\n"
            f"{conservative.text}\n"
        )
        neutral = client.complete(
            system=hold_neutral.PROMPT,
            messages=[{"role": "user", "content": neutral_user}],
            max_tokens=max_turn_tokens,
        )
        judge_user = (
            f"{brief}\n\nAGGRESSIVE (position trader):\n{aggressive.text}\n\n"
            f"CONSERVATIVE (risk manager):\n{conservative.text}\n\n"
            f"NEUTRAL (book runner):\n{neutral.text}\n"
        )
        judge = client.complete_structured(
            system=hold_judge.PROMPT,
            messages=[{"role": "user", "content": judge_user}],
            tool_name="cast_hold_verdict",
            tool_description=(
                "Cast the final hold-debate verdict. 'hold' = no action; "
                "'tighten_stop' = replace stop at breakeven or swing low; "
                "'exit_now' = flatten the position at market."
            ),
            tool_schema=_JUDGE_TOOL_SCHEMA,
            max_tokens=max_judge_tokens,
        )
    except BudgetExceededError:
        log.info("hold_debate: skipped (budget halt) — fail soft")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("hold_debate: SDK error, failing soft: %s", e)
        return None

    if not (judge.used_structured and judge.data):
        log.warning("hold_debate: judge returned free text only, failing soft")
        return None
    try:
        v = _HoldJudgeOutput.model_validate(judge.data)
    except Exception as e:  # noqa: BLE001
        log.warning("hold_debate: judge schema mismatch, failing soft: %s", e)
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

    return HoldDebateVerdict(
        recommendation=v.recommendation,
        confidence=v.confidence,
        reason=final_reason,
        aggressive_text=aggressive.text,
        conservative_text=conservative.text,
        neutral_text=neutral.text,
    )


def persist_run(
    engine,
    *,
    verdict: HoldDebateVerdict | None,
    symbol: str,
    asset_class: str,
    entry_order_id: str | None,
    trigger_reason: str,
    current_score: float | None,
    current_sentiment: float | None,
    entry_score: float | None,
    entry_sentiment: float | None,
    action_taken: str = "none",
    now: dt.datetime | None = None,
) -> int:
    """Write one HoldDebateRun row. ``verdict`` may be None (fail-soft);
    we still persist a row so the audit trail records that the trigger
    fired and we couldn't act.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        session.add(HoldDebateRun(
            run_at=now,
            asset_class=asset_class,
            symbol=symbol,
            entry_order_id=entry_order_id,
            trigger_reason=trigger_reason,
            current_score=current_score,
            current_sentiment=current_sentiment,
            entry_score=entry_score,
            entry_sentiment=entry_sentiment,
            verdict=(verdict.recommendation if verdict else "fail_soft"),
            confidence=(verdict.confidence if verdict else "low"),
            judge_reason=(verdict.reason if verdict else "(LLM gate unreachable)"),
            aggressive_text=(verdict.aggressive_text if verdict else ""),
            conservative_text=(verdict.conservative_text if verdict else ""),
            neutral_text=(verdict.neutral_text if verdict else ""),
            action_taken=action_taken,
            prompt_version=_persona_version(),
        ))
        session.commit()
    return 1


# ---------------------------------------------------------------------------
# Snapshot helper — called at order placement time
# ---------------------------------------------------------------------------


def write_intel_snapshot(
    engine,
    *,
    entry_order_id: str,
    symbol: str,
    asset_class: str,
    entry_intel_score: float | None,
    entry_top_reason: str = "",
    entry_sentiment_avg: float | None = None,
    entry_top_sources: list[str] | None = None,
    now: dt.datetime | None = None,
) -> int:
    """Capture the entry-time intel state so the hold debate has a stable
    baseline. Idempotent on (entry_order_id) — re-running on the same
    order won't double-write.
    """
    import json as _json
    from trading_bot.state_db import TradeIntelSnapshot
    now = now or dt.datetime.now(dt.timezone.utc)
    sources_payload = _json.dumps(entry_top_sources or [])
    with Session(engine) as session:
        existing = (
            session.query(TradeIntelSnapshot)
            .filter(TradeIntelSnapshot.entry_order_id == entry_order_id)
            .first()
        )
        if existing is not None:
            return 0
        session.add(TradeIntelSnapshot(
            entry_order_id=entry_order_id,
            symbol=symbol,
            asset_class=asset_class,
            captured_at=now,
            entry_intel_score=entry_intel_score,
            entry_top_reason=(entry_top_reason or "")[:2000],
            entry_sentiment_avg=entry_sentiment_avg,
            entry_top_sources_json=sources_payload,
        ))
        session.commit()
    return 1


def lookup_snapshot(engine, entry_order_id: str):
    """Return the TradeIntelSnapshot row (or None) for an entry_order_id."""
    from trading_bot.state_db import TradeIntelSnapshot
    with Session(engine) as session:
        row = (
            session.query(TradeIntelSnapshot)
            .filter(TradeIntelSnapshot.entry_order_id == entry_order_id)
            .first()
        )
        if row is not None:
            session.expunge(row)
    return row
