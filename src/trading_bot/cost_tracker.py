"""Anthropic API cost tracking + monthly cap enforcement.

Per-model rates (locked, in USD/Mtok input + output):
  claude-opus-4-7  : $15 / $75
  claude-haiku-4-5 : $0.80 / $4

Monthly cap default $20 (configurable via ANTHROPIC_MONTHLY_BUDGET_USD env).
At 80% spend → warning. At 100% → CostHalt row written, LLM roles refuse.
"""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy.orm import Session

from trading_bot.state_db import AnthropicCostLog, CostHalt

# (input_per_mtok, output_per_mtok) USD
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-haiku-4-5": (0.80, 4.00),
}

DEFAULT_MONTHLY_CAP_USD = 20.0
WARN_THRESHOLD_FRACTION = 0.80


def monthly_cap_usd() -> float:
    return float(os.environ.get("ANTHROPIC_MONTHLY_BUDGET_USD", DEFAULT_MONTHLY_CAP_USD))


def _price_per_call(model: str, in_tokens: int, out_tokens: int) -> float:
    rates = _MODEL_PRICES.get(model)
    if rates is None:
        # Conservative fallback to Opus pricing if model unknown
        rates = _MODEL_PRICES["claude-opus-4-7"]
    in_cost = in_tokens * rates[0] / 1_000_000
    out_cost = out_tokens * rates[1] / 1_000_000
    return in_cost + out_cost


def record_call(
    session: Session,
    *,
    role_name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    request_id: str | None = None,
) -> float:
    cost = _price_per_call(model, input_tokens, output_tokens)
    session.add(
        AnthropicCostLog(
            called_at=dt.datetime.now(dt.timezone.utc),
            role_name=role_name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            request_id=request_id,
        )
    )
    session.commit()
    # Auto-halt if cumulative monthly spend now exceeds cap.
    spend = monthly_spend(session)
    cap = monthly_cap_usd()
    if spend >= cap:
        _set_halt_until_next_month(
            session, reason=f"monthly_spend ${spend:.2f} ≥ cap ${cap:.2f}"
        )
    return cost


def monthly_spend(
    session: Session, *, year: int | None = None, month: int | None = None
) -> float:
    now = dt.datetime.now(dt.timezone.utc)
    y = year or now.year
    m = month or now.month
    start = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    end = (
        dt.datetime(y + 1, 1, 1, tzinfo=dt.timezone.utc)
        if m == 12
        else dt.datetime(y, m + 1, 1, tzinfo=dt.timezone.utc)
    )
    rows = (
        session.query(AnthropicCostLog)
        .filter(AnthropicCostLog.called_at >= start, AnthropicCostLog.called_at < end)
        .all()
    )
    return float(sum(r.cost_usd for r in rows))


def is_halted(session: Session) -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    row = (
        session.query(CostHalt)
        .filter(CostHalt.halted_until > now)
        .first()
    )
    return row is not None


def _set_halt_until_next_month(session: Session, *, reason: str) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    if now.month == 12:
        next_first = dt.datetime(now.year + 1, 1, 1, tzinfo=dt.timezone.utc)
    else:
        next_first = dt.datetime(now.year, now.month + 1, 1, tzinfo=dt.timezone.utc)
    session.add(
        CostHalt(halted_until=next_first, reason=reason, set_at=now)
    )
    session.commit()
