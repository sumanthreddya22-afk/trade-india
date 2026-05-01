"""DecisionReflectorRole tests.

End-to-end: build a real (in-memory) state.db with a Decisions row, a real
file-backed closed_trades.db with the matching trade, mock the LLM, run the
role, and verify a DecisionLesson row was written.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.anthropic_client import StructuredResponse
from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore
from trading_bot.roles.decision_reflector import DecisionReflectorRole
from trading_bot.state_db import Base, Decisions, DecisionLesson


@pytest.fixture
def state_engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _seed_decision(engine, *, decision_id="d_AAPL", entry_order_id="o_1",
                   symbol="AAPL", strategy="momentum", regime="trending_up"):
    with Session(engine) as s:
        s.add(Decisions(
            decision_id=decision_id,
            timestamp_utc=dt.datetime.now(dt.timezone.utc),
            symbol=symbol, action="placed_order", reason="rsi_pullback",
            strategy=strategy, regime=regime, asset_class="stock",
            confidence=0.62, expected_edge_bps=35.0,
            entry_order_id=entry_order_id, stop_loss_order_id="s_1",
        ))
        s.commit()


def _seed_closed_trade(db_path, *, entry_order_id="o_1", symbol="AAPL",
                       strategy="momentum", regime="trending_up", pnl_pct=-2.1):
    store = ClosedTradeStore(db_path)
    now = dt.datetime.now(dt.timezone.utc)
    store.append(ClosedTrade(
        symbol=symbol, side="buy", qty=Decimal("10"),
        entry_price=Decimal("180.00"), exit_price=Decimal("176.20"),
        realized_pnl=Decimal("-38.00"), pnl_pct=pnl_pct,
        strategy=strategy, regime=regime,
        entry_time=now - dt.timedelta(hours=20),
        exit_time=now - dt.timedelta(hours=2),
        hold_hours=18.0, entry_order_id=entry_order_id,
        notes="stop hit",
    ))


def test_reflects_unreflected_trade_and_writes_lesson(state_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    _seed_decision(state_engine)
    closed_db = tmp_path / "closed_trades.db"
    _seed_closed_trade(closed_db)

    fake_resp = StructuredResponse(
        data={
            "lesson": "Stop hit on intraday reversal after a thin breakout — "
                      "RSI was already 78 at entry, late-cycle momentum signal.",
            "tags": ["stop_hit", "bad_entry"],
        },
        text="",
        used_structured=True,
        input_tokens=200, output_tokens=80,
        request_id="req-1", model="claude-opus-4-7",
    )

    role = DecisionReflectorRole(engine=state_engine, closed_trades_db=closed_db)
    with patch("trading_bot.roles.decision_reflector.MailboxBackedClient") as mock_cls:
        instance = MagicMock()
        instance.complete_structured.return_value = fake_resp
        mock_cls.return_value = instance
        result = role.safe_run(ctx={})

    assert result.outputs["reflected"] == 1
    with Session(state_engine) as s:
        rows = s.query(DecisionLesson).all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].strategy == "momentum"
    assert "RSI" in rows[0].lesson


def test_skips_when_no_closed_trades_db(state_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    role = DecisionReflectorRole(
        engine=state_engine, closed_trades_db=tmp_path / "missing.db"
    )
    result = role.safe_run(ctx={})
    assert result.outputs["skipped"] is True
    assert result.outputs["reason"] == "no_closed_trades_db"


def test_skips_trades_without_matching_decision(state_engine, tmp_path, monkeypatch):
    """A closed trade whose entry_order_id has no Decisions row is silently
    skipped — that's expected for trades placed before the reflector existed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    closed_db = tmp_path / "closed_trades.db"
    _seed_closed_trade(closed_db, entry_order_id="o_orphan")

    role = DecisionReflectorRole(engine=state_engine, closed_trades_db=closed_db)
    with patch("trading_bot.roles.decision_reflector.MailboxBackedClient") as mock_cls:
        instance = MagicMock()
        # Should not be called; if it is, the test will catch a 'no candidate' branch.
        instance.complete_structured.side_effect = AssertionError("LLM should not be called")
        mock_cls.return_value = instance
        result = role.safe_run(ctx={})

    assert result.outputs.get("reflected", 0) == 0
    assert result.outputs.get("reason") == "nothing_to_reflect"


def test_idempotent_does_not_double_reflect(state_engine, tmp_path, monkeypatch):
    """Running twice over the same closed trade writes one lesson, not two."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    _seed_decision(state_engine)
    closed_db = tmp_path / "closed_trades.db"
    _seed_closed_trade(closed_db)

    fake_resp = StructuredResponse(
        data={"lesson": "First pass lesson — long enough to clear minlen.", "tags": []},
        text="", used_structured=True,
        input_tokens=10, output_tokens=10, request_id="r", model="claude-opus-4-7",
    )

    role = DecisionReflectorRole(engine=state_engine, closed_trades_db=closed_db)
    with patch("trading_bot.roles.decision_reflector.MailboxBackedClient") as mock_cls:
        instance = MagicMock()
        instance.complete_structured.return_value = fake_resp
        mock_cls.return_value = instance

        first = role.safe_run(ctx={})
        second = role.safe_run(ctx={})

    assert first.outputs["reflected"] == 1
    assert second.outputs["reflected"] == 0  # already-reflected trade is skipped
    with Session(state_engine) as s:
        assert s.query(DecisionLesson).count() == 1


def test_falls_back_when_tool_use_skipped(state_engine, tmp_path, monkeypatch):
    """If Claude emits free text instead of using the tool, the reflector
    still records the prose lesson (truncated to 600 chars)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    _seed_decision(state_engine)
    closed_db = tmp_path / "closed_trades.db"
    _seed_closed_trade(closed_db)

    fallback = StructuredResponse(
        data=None,
        text="Free-text fallback lesson — not structured, but still useful.",
        used_structured=False,
        input_tokens=10, output_tokens=10, request_id="r", model="claude-opus-4-7",
    )

    role = DecisionReflectorRole(engine=state_engine, closed_trades_db=closed_db)
    with patch("trading_bot.roles.decision_reflector.MailboxBackedClient") as mock_cls:
        instance = MagicMock()
        instance.complete_structured.return_value = fallback
        mock_cls.return_value = instance
        result = role.safe_run(ctx={})

    assert result.outputs["reflected"] == 1
    assert result.outputs["skipped_text_only"] == 1
    with Session(state_engine) as s:
        row = s.query(DecisionLesson).first()
    assert "Free-text fallback" in row.lesson
