"""decision_lessons module tests — table I/O + prompt-injection helper."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine

from trading_bot.decision_lessons import (
    Lesson,
    append_lesson,
    get_lessons,
    has_lesson,
    recent_lessons_text,
)
from trading_bot.state_db import Base, DecisionLesson


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _common(**over):
    base = dict(
        decision_id="d_1", entry_order_id="o_1",
        symbol="AAPL", strategy="momentum", regime="trending_up",
        pnl_pct=2.5, hold_hours=18.5,
        lesson="Entry was clean; exited too early on noise.",
        tags=["good_entry", "bad_exit"],
    )
    base.update(over)
    return base


def test_append_lesson_persists_all_fields(engine):
    inserted = append_lesson(engine, **_common())
    assert inserted is True
    rows = get_lessons(engine)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, Lesson)
    assert r.decision_id == "d_1"
    assert r.symbol == "AAPL"
    assert r.tags == ("good_entry", "bad_exit")
    assert r.pnl_pct == pytest.approx(2.5)
    assert isinstance(r.created_at, dt.datetime)


def test_append_lesson_is_idempotent(engine):
    append_lesson(engine, **_common())
    inserted_again = append_lesson(engine, **_common(lesson="Different text"))
    assert inserted_again is False
    rows = get_lessons(engine)
    assert len(rows) == 1
    assert rows[0].lesson.startswith("Entry was clean")


def test_has_lesson(engine):
    assert has_lesson(engine, decision_id="d_X") is False
    append_lesson(engine, **_common(decision_id="d_X"))
    assert has_lesson(engine, decision_id="d_X") is True


def test_get_lessons_filters_by_symbol_and_strategy(engine):
    append_lesson(engine, **_common(decision_id="d_1", symbol="AAPL", strategy="momentum"))
    append_lesson(engine, **_common(decision_id="d_2", symbol="AAPL", strategy="mean_reversion", entry_order_id="o_2"))
    append_lesson(engine, **_common(decision_id="d_3", symbol="MSFT", strategy="momentum", entry_order_id="o_3"))

    aapl = get_lessons(engine, symbol="AAPL")
    assert {l.decision_id for l in aapl} == {"d_1", "d_2"}

    momentum = get_lessons(engine, strategy="momentum")
    assert {l.decision_id for l in momentum} == {"d_1", "d_3"}

    both = get_lessons(engine, symbol="AAPL", strategy="momentum")
    assert [l.decision_id for l in both] == ["d_1"]


def test_recent_lessons_text_empty_returns_empty_string(engine):
    assert recent_lessons_text(engine) == ""


def test_recent_lessons_text_focused_and_cross(engine):
    # Three lessons; ask for 2 focused on AAPL, 1 cross.
    append_lesson(engine, **_common(decision_id="d_1", symbol="AAPL"))
    append_lesson(engine, **_common(decision_id="d_2", symbol="AAPL", entry_order_id="o_2",
                                    lesson="Second AAPL miss."))
    append_lesson(engine, **_common(decision_id="d_3", symbol="MSFT", entry_order_id="o_3",
                                    lesson="MSFT cross-context note."))
    text = recent_lessons_text(engine, symbol="AAPL", n_focused=2, n_cross=1)
    assert "Focused" in text
    assert "Cross-context" in text
    # Both AAPL lessons appear in focused.
    assert text.count("AAPL") >= 2
    assert "MSFT" in text  # cross-context


def test_recent_lessons_text_omits_focused_section_when_no_filter(engine):
    append_lesson(engine, **_common(decision_id="d_1", symbol="AAPL"))
    text = recent_lessons_text(engine, n_focused=5, n_cross=2)
    # No filter → focused section is empty; only cross-context appears.
    assert "Focused" not in text
    assert "Cross-context" in text


def test_tags_json_round_trip(engine):
    append_lesson(engine, **_common(tags=["good_entry", "bad_exit", "noise"]))
    # Read raw row to verify storage encoding (JSON list of strings).
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.query(DecisionLesson).first()
    parsed = json.loads(row.tags_json)
    assert parsed == ["good_entry", "bad_exit", "noise"]
