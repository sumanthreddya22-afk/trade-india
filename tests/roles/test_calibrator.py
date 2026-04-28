"""CalibratorRole tests."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.base import RoleStatus
from trading_bot.roles.calibrator import CalibratorRole, _pair_trades
from trading_bot.state_db import (
    Base,
    CalibrationRun,
    Leaderboard,
    PromoterHalt,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def active_path(tmp_path):
    p = tmp_path / "paper_active.json"
    p.write_text(json.dumps({"active_template": "momentum"}))
    return p


def _add_leaderboard_row_with_predictions(session, predictions: list[dict]):
    session.add(
        Leaderboard(
            template_name="momentum",
            params_hash="h",
            params_json="{}",
            alpha_vs_spy_x=1.6,
            sortino=1.2,
            max_dd_pct=15.0,
            folds_passed=6,
            folds_total=6,
            fitness_score=2.1,
            recorded_at=dt.datetime.now(dt.timezone.utc),
            per_trade_predictions_json=json.dumps(predictions),
        )
    )
    session.commit()


def test_skips_when_no_active_config(engine, tmp_path):
    role = CalibratorRole(
        engine=engine,
        config_path=tmp_path / "missing.json",
        closed_trades_db=tmp_path / "missing.db",
    )
    result = role.safe_run(ctx={})
    assert result.outputs["skipped"] is True
    assert result.outputs["reason"] == "no_active_config"


def test_skips_when_no_leaderboard_with_predictions(engine, active_path, tmp_path):
    role = CalibratorRole(
        engine=engine, config_path=active_path, closed_trades_db=tmp_path / "x.db"
    )
    result = role.safe_run(ctx={})
    assert result.outputs["skipped"] is True
    # Still records a run row for audit
    with Session(engine) as s:
        runs = s.query(CalibrationRun).all()
    assert len(runs) == 1
    assert runs[0].severity == "insufficient_data"


def test_skips_when_no_closed_trades(engine, active_path, tmp_path):
    role = CalibratorRole(
        engine=engine, config_path=active_path, closed_trades_db=tmp_path / "x.db"
    )
    with Session(engine) as s:
        _add_leaderboard_row_with_predictions(
            s, [{"symbol": "AAPL", "entry_date": "2026-04-01", "predicted_pnl": 100.0}]
        )
    result = role.safe_run(ctx={})
    assert result.outputs["skipped"] is True
    assert result.outputs["reason"] == "no_closed_trades"


def test_pair_trades_matches_by_symbol_and_date():
    predictions = [
        {"symbol": "AAPL", "entry_date": "2026-04-01", "predicted_pnl": 100.0},
        {"symbol": "AAPL", "entry_date": "2026-04-15", "predicted_pnl": -50.0},
        {"symbol": "TSLA", "entry_date": "2026-04-10", "predicted_pnl": 200.0},
    ]
    realized = [
        {"symbol": "AAPL", "entry_date": "2026-04-02", "realized_pnl": 90.0},
        {"symbol": "TSLA", "entry_date": "2026-04-11", "realized_pnl": 180.0},
        {"symbol": "GOOG", "entry_date": "2026-04-05", "realized_pnl": 30.0},  # no match
    ]
    pairs = _pair_trades(predictions, realized)
    assert len(pairs) == 2
    # AAPL paired with closest (Apr 1 prediction for Apr 2 realized): (100, 90)
    assert (100.0, 90.0) in pairs
    assert (200.0, 180.0) in pairs


def test_full_path_writes_calibration_run_and_halts_on_high_drift(
    engine, active_path, tmp_path, monkeypatch
):
    """Stub realized trades into the role; predictions and realized are anti-correlated → high drift."""
    n = 15
    predictions = [
        {
            "symbol": f"SYM{i}",
            "entry_date": "2026-04-01",
            "predicted_pnl": float(i),
        }
        for i in range(n)
    ]
    fake_realized = [
        {
            "symbol": f"SYM{i}",
            "entry_date": "2026-04-01",
            "realized_pnl": float(-i),  # perfect inverse
        }
        for i in range(n)
    ]
    role = CalibratorRole(
        engine=engine, config_path=active_path, closed_trades_db=tmp_path / "x.db"
    )
    with Session(engine) as s:
        _add_leaderboard_row_with_predictions(s, predictions)

    with patch.object(role, "_load_realized_trades", return_value=fake_realized):
        result = role.safe_run(ctx={})

    assert result.status == RoleStatus.OK
    assert result.outputs["severity"] == "high"
    assert result.outputs["n_trades"] == n
    # PromoterHalt row written
    with Session(engine) as s:
        halts = s.query(PromoterHalt).all()
    assert len(halts) == 1
    assert halts[0].set_by == "calibrator"


def test_full_path_no_halt_on_ok_drift(engine, active_path, tmp_path, monkeypatch):
    n = 15
    predictions = [
        {"symbol": f"SYM{i}", "entry_date": "2026-04-01", "predicted_pnl": float(i)}
        for i in range(n)
    ]
    realized = [
        {"symbol": f"SYM{i}", "entry_date": "2026-04-01", "realized_pnl": float(i) * 1.05}
        for i in range(n)
    ]
    role = CalibratorRole(
        engine=engine, config_path=active_path, closed_trades_db=tmp_path / "x.db"
    )
    with Session(engine) as s:
        _add_leaderboard_row_with_predictions(s, predictions)
    with patch.object(role, "_load_realized_trades", return_value=realized):
        result = role.safe_run(ctx={})
    assert result.outputs["severity"] == "ok"
    with Session(engine) as s:
        assert s.query(PromoterHalt).count() == 0
