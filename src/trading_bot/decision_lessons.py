"""Decision-outcome lessons store + prompt-injection helper.

Lessons are 2-4 sentence post-mortems written by ``decision_reflector`` for
closed trades. They are the project's memory of *what specifically went
right or wrong*, keyed by symbol/strategy/regime, and are cheap to inject
into Claude prompts so the architect (and future reasoning roles) can see
recent mistakes before proposing new strategies.

This module exposes:

- :func:`append_lesson` — idempotent insert by ``decision_id``
- :func:`get_lessons` — typed read with optional symbol/strategy filter
- :func:`recent_lessons_text` — markdown bullet list ready for prompt use

Pattern adapted from TauricResearch/TradingAgents'
``agents/utils/memory.py:get_past_context`` — same idea (bias future
reasoning with hindsight from prior outcomes), without the markdown-file
dependency.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.state_db import DecisionLesson


@dataclass(frozen=True)
class Lesson:
    decision_id: str
    entry_order_id: str
    symbol: str
    strategy: str
    regime: str
    pnl_pct: float
    hold_hours: float
    lesson: str
    tags: tuple[str, ...]
    created_at: dt.datetime


def append_lesson(
    engine,
    *,
    decision_id: str,
    entry_order_id: str,
    symbol: str,
    strategy: str,
    regime: str,
    pnl_pct: float,
    hold_hours: float,
    lesson: str,
    tags: Iterable[str] = (),
) -> bool:
    """Insert one lesson. Idempotent on ``decision_id``: a duplicate insert
    is a no-op (the existing row is preserved). Returns True on insert,
    False on duplicate."""
    tags_list = [str(t).strip() for t in tags if str(t).strip()]
    with Session(engine) as session:
        existing = (
            session.query(DecisionLesson)
            .filter(DecisionLesson.decision_id == decision_id)
            .first()
        )
        if existing is not None:
            return False
        session.add(
            DecisionLesson(
                decision_id=decision_id,
                entry_order_id=entry_order_id,
                symbol=symbol,
                strategy=strategy,
                regime=regime,
                pnl_pct=float(pnl_pct),
                hold_hours=float(hold_hours),
                lesson=lesson.strip(),
                tags_json=json.dumps(tags_list),
                created_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.commit()
        return True


def get_lessons(
    engine,
    *,
    symbol: str | None = None,
    strategy: str | None = None,
    limit: int = 20,
) -> list[Lesson]:
    """Most-recent-first fetch with optional symbol/strategy filter."""
    with Session(engine) as session:
        q = session.query(DecisionLesson)
        if symbol:
            q = q.filter(DecisionLesson.symbol == symbol)
        if strategy:
            q = q.filter(DecisionLesson.strategy == strategy)
        rows = q.order_by(desc(DecisionLesson.created_at)).limit(limit).all()
    out: list[Lesson] = []
    for r in rows:
        try:
            tags = tuple(json.loads(r.tags_json or "[]"))
        except json.JSONDecodeError:
            tags = ()
        out.append(
            Lesson(
                decision_id=r.decision_id,
                entry_order_id=r.entry_order_id,
                symbol=r.symbol,
                strategy=r.strategy,
                regime=r.regime,
                pnl_pct=r.pnl_pct,
                hold_hours=r.hold_hours,
                lesson=r.lesson,
                tags=tags,
                created_at=r.created_at,
            )
        )
    return out


def has_lesson(engine, *, decision_id: str) -> bool:
    with Session(engine) as session:
        return (
            session.query(DecisionLesson)
            .filter(DecisionLesson.decision_id == decision_id)
            .first()
            is not None
        )


def recent_lessons_text(
    engine,
    *,
    symbol: str | None = None,
    strategy: str | None = None,
    n_focused: int = 5,
    n_cross: int = 3,
) -> str:
    """Return a markdown-bulleted list of recent lessons for prompt use.

    Up to ``n_focused`` lessons matching ``symbol`` and/or ``strategy``,
    followed by up to ``n_cross`` of the most recent lessons regardless of
    filter (so the model sees both narrow and broad context). Returns the
    empty string if there are no lessons — callers can treat that as
    "no prior context"."""
    blocks: list[str] = []
    focused: list[Lesson] = []
    if symbol or strategy:
        focused = get_lessons(engine, symbol=symbol, strategy=strategy, limit=n_focused)
    if focused:
        blocks.append("Focused (same context):")
        for lsn in focused:
            blocks.append(_format_one(lsn))
    cross_excludes = {lsn.decision_id for lsn in focused}
    cross_pool = get_lessons(engine, limit=n_focused + n_cross)
    cross = [c for c in cross_pool if c.decision_id not in cross_excludes][:n_cross]
    if cross:
        blocks.append("Cross-context (recent across symbols):")
        for lsn in cross:
            blocks.append(_format_one(lsn))
    return "\n".join(blocks)


def _format_one(lsn: Lesson) -> str:
    tags = f" [{', '.join(lsn.tags)}]" if lsn.tags else ""
    return (
        f"- {lsn.symbol} / {lsn.strategy} / {lsn.regime} "
        f"({lsn.pnl_pct:+.2f}%, {lsn.hold_hours:.1f}h){tags}: {lsn.lesson}"
    )
