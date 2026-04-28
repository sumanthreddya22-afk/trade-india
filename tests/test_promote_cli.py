"""bot promote CLI tests — focus on the live-mode safety gates."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.promote_cli import (
    LIVE_CONFIRM_STRING,
    LOCKED_LIVE_RISK_CAPS,
    PromoteRefused,
    promote_to_live,
    promote_to_paper,
)
from trading_bot.state_db import Base, ConfigHistory, Leaderboard


@pytest.fixture
def state_db_path(tmp_path):
    p = tmp_path / "state.db"
    eng = create_engine(f"sqlite:///{p}", future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(
            Leaderboard(
                template_name="momentum",
                params_hash="h",
                params_json=json.dumps({"rsi_lower": 58.0}),
                alpha_vs_spy_x=1.7,
                sortino=1.3,
                max_dd_pct=15.0,
                folds_passed=6,
                folds_total=6,
                fitness_score=2.0,
                recorded_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        s.commit()
    return p


@pytest.fixture
def paper_active_path(tmp_path):
    p = tmp_path / "paper_active.json"
    p.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "active_template": "momentum",
                "params": {"rsi_lower": 55.0},
                "fitness_at_promotion": 1.0,
                "risk_caps": {
                    "max_position_pct": 10,
                    "daily_loss_pct": 3,
                    "max_drawdown_pct": 20,
                },
            }
        )
    )
    return p


# === paper-target tests ===


def test_paper_promote_rewrites_when_top_clears_gate(state_db_path, paper_active_path):
    result = promote_to_paper(
        state_db_path=state_db_path, active_path=paper_active_path
    )
    assert result["promoted"] is True
    written = json.loads(paper_active_path.read_text())
    assert written["params"]["rsi_lower"] == 58.0


def test_paper_promote_refuses_when_no_leaderboard(tmp_path):
    state_db = tmp_path / "empty.db"
    eng = create_engine(f"sqlite:///{state_db}", future=True)
    Base.metadata.create_all(eng)
    paper = tmp_path / "active.json"
    paper.write_text("{}")
    with pytest.raises(PromoteRefused, match="empty"):
        promote_to_paper(state_db_path=state_db, active_path=paper)


# === live-target gate tests ===


def test_live_refused_without_creds(tmp_path, paper_active_path, monkeypatch):
    monkeypatch.delenv("ALPACA_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_LIVE_API_SECRET", raising=False)
    live_path = tmp_path / "live.json"
    with pytest.raises(PromoteRefused, match="ALPACA_LIVE_API_KEY"):
        promote_to_live(
            paper_active_path=paper_active_path,
            live_active_path=live_path,
            state_db_path=tmp_path / "state.db",
            i_know_real_money=True,
            confirm_input_provider=lambda _prompt: LIVE_CONFIRM_STRING,
        )
    assert not live_path.exists()


def test_live_refused_without_flag(tmp_path, paper_active_path, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "s")
    live_path = tmp_path / "live.json"
    with pytest.raises(PromoteRefused, match="--i-know-this-is-real-money"):
        promote_to_live(
            paper_active_path=paper_active_path,
            live_active_path=live_path,
            state_db_path=tmp_path / "state.db",
            i_know_real_money=False,
            confirm_input_provider=lambda _: LIVE_CONFIRM_STRING,
        )
    assert not live_path.exists()


def test_live_refused_with_lowercase_confirmation(tmp_path, paper_active_path, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "s")
    live_path = tmp_path / "live.json"
    with pytest.raises(PromoteRefused, match="confirmation"):
        promote_to_live(
            paper_active_path=paper_active_path,
            live_active_path=live_path,
            state_db_path=tmp_path / "state.db",
            i_know_real_money=True,
            confirm_input_provider=lambda _: "yes, flip to live",  # wrong case
        )
    assert not live_path.exists()


def test_live_refused_with_substring_confirmation(tmp_path, paper_active_path, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "s")
    live_path = tmp_path / "live.json"
    with pytest.raises(PromoteRefused):
        promote_to_live(
            paper_active_path=paper_active_path,
            live_active_path=live_path,
            state_db_path=tmp_path / "state.db",
            i_know_real_money=True,
            confirm_input_provider=lambda _: "  YES, FLIP TO LIVE  ",  # whitespace
        )
    assert not live_path.exists()


def test_live_refused_when_paper_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "s")
    live_path = tmp_path / "live.json"
    with pytest.raises(PromoteRefused, match="paper_active.json missing"):
        promote_to_live(
            paper_active_path=tmp_path / "missing.json",
            live_active_path=live_path,
            state_db_path=tmp_path / "state.db",
            i_know_real_money=True,
            confirm_input_provider=lambda _: LIVE_CONFIRM_STRING,
        )


def test_live_succeeds_with_all_three_passing(state_db_path, paper_active_path, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "s")
    live_path = tmp_path / "live.json"
    result = promote_to_live(
        paper_active_path=paper_active_path,
        live_active_path=live_path,
        state_db_path=state_db_path,
        i_know_real_money=True,
        confirm_input_provider=lambda _: LIVE_CONFIRM_STRING,
    )
    assert result["promoted"] is True
    assert live_path.exists()
    written = json.loads(live_path.read_text())
    # Risk caps replaced with locked stricter ones
    assert written["risk_caps"] == LOCKED_LIVE_RISK_CAPS
    assert written["bot_mode"] == "live"
    # ConfigHistory recorded
    eng = create_engine(f"sqlite:///{state_db_path}", future=True)
    with Session(eng) as s:
        rows = s.query(ConfigHistory).filter_by(account="live").all()
    assert len(rows) == 1


def test_live_no_tmp_file_left_after_success(state_db_path, paper_active_path, tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "k")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "s")
    live_path = tmp_path / "live.json"
    promote_to_live(
        paper_active_path=paper_active_path,
        live_active_path=live_path,
        state_db_path=state_db_path,
        i_know_real_money=True,
        confirm_input_provider=lambda _: LIVE_CONFIRM_STRING,
    )
    tmp_file = live_path.with_suffix(live_path.suffix + ".tmp")
    assert not tmp_file.exists()


def test_promote_command_registered():
    from trading_bot.cli import main as cli_main

    assert "promote" in cli_main.commands
