"""state.db ORM models. Shared coordination surface for daemon, lab, supervisor.
WAL mode is enabled at engine creation in get_engine() so concurrent reads are safe.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class Heartbeat(Base):
    __tablename__ = "heartbeats"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    pid = Column(Integer, nullable=False)
    version = Column(String(64), nullable=False)
    last_action = Column(String(128), nullable=True)


class EquityHighWaterMark(Base):
    __tablename__ = "equity_high_water_mark"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(16), nullable=False, index=True)  # "paper" | "live"
    equity = Column(Float, nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class RoleRun(Base):
    __tablename__ = "role_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(64), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=False)  # ok | error | blocked | halted
    latency_ms = Column(Integer, nullable=True)
    error_text = Column(Text, nullable=True)


class RoleKpi(Base):
    __tablename__ = "role_kpis"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(64), nullable=False, index=True)
    kpi_name = Column(String(64), nullable=False)
    value = Column(Float, nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class RegimeHistory(Base):
    __tablename__ = "regime_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    regime = Column(String(32), nullable=False)
    vix = Column(Float, nullable=True)
    spy_breadth = Column(Float, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)


class ConfigHistory(Base):
    __tablename__ = "config_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(String(16), nullable=False)
    version = Column(String(64), nullable=False)
    git_sha = Column(String(64), nullable=True)
    promoted_at = Column(DateTime(timezone=True), nullable=False)
    promoted_by = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)


class Leaderboard(Base):
    __tablename__ = "leaderboard"
    id = Column(Integer, primary_key=True, autoincrement=True)
    template_name = Column(String(64), nullable=False, index=True)
    params_hash = Column(String(64), nullable=False, index=True)
    params_json = Column(Text, nullable=False)
    alpha_vs_spy_x = Column(Float, nullable=False)
    sortino = Column(Float, nullable=False)
    max_dd_pct = Column(Float, nullable=False)
    folds_passed = Column(Integer, nullable=False)
    folds_total = Column(Integer, nullable=False)
    fitness_score = Column(Float, nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    # Per-trade predicted P&L from the most recent test fold's trades.
    # Used by Calibrator to compare against realized paper P&L. JSON list:
    # [{"symbol": "...", "entry_date": "ISO", "predicted_pnl": float}, ...]
    per_trade_predictions_json = Column(Text, nullable=True)


class EvolutionRun(Base):
    __tablename__ = "evolution_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    template_name = Column(String(64), nullable=False)
    n_trials = Column(Integer, nullable=False)
    best_fitness = Column(Float, nullable=True)
    best_params_hash = Column(String(64), nullable=True)
    auto_promoted = Column(Integer, nullable=False, default=0)
    promotion_gate_pass = Column(Text, nullable=True)


class CalibrationRun(Base):
    __tablename__ = "calibration_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    template_name = Column(String(64), nullable=False)
    n_trades = Column(Integer, nullable=False)
    spearman_corr = Column(Float, nullable=True)  # null when n < 10
    severity = Column(String(16), nullable=False)  # ok|warning|high|insufficient_data


class PromoterHalt(Base):
    __tablename__ = "promoter_halts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    halted_until = Column(DateTime(timezone=True), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    set_by = Column(String(64), nullable=False)
    set_at = Column(DateTime(timezone=True), nullable=False)


class FallbackFlag(Base):
    """Append-only audit trail of fallback (hold-SPY) flag transitions.
    Current flag = row with the most recent set_at."""
    __tablename__ = "fallback_flags"
    id = Column(Integer, primary_key=True, autoincrement=True)
    fallback_active = Column(Integer, nullable=False)  # 0|1
    set_at = Column(DateTime(timezone=True), nullable=False, index=True)
    set_by = Column(String(64), nullable=False)         # strategy_coach|manual|bootstrap
    reason = Column(Text, nullable=True)


class HoldSpyTransitionState(Base):
    """Tracks 5-day exit/reverse progress for Hold-SPY Coordinator."""
    __tablename__ = "hold_spy_transitions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    fallback_flag_id = Column(Integer, nullable=False, index=True)
    phase = Column(String(16), nullable=False)          # exit|reverse
    day_index = Column(Integer, nullable=False, default=0)
    last_action_at = Column(DateTime(timezone=True), nullable=True)


class AnthropicCostLog(Base):
    """Per-call Anthropic API usage + computed cost. Phase 5 Strategy Architect."""
    __tablename__ = "anthropic_cost_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    called_at = Column(DateTime(timezone=True), nullable=False, index=True)
    role_name = Column(String(64), nullable=False)
    model = Column(String(64), nullable=False)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cost_usd = Column(Float, nullable=False)
    request_id = Column(String(128), nullable=True)


class CostHalt(Base):
    """Anthropic spend cap exceeded — halts LLM-driven roles until halted_until."""
    __tablename__ = "cost_halts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    halted_until = Column(DateTime(timezone=True), nullable=False, index=True)
    reason = Column(Text, nullable=False)
    set_at = Column(DateTime(timezone=True), nullable=False)


class TemplateProposal(Base):
    """Strategy Architect's proposed templates, before/after Code Reviewer."""
    __tablename__ = "template_proposals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposed_at = Column(DateTime(timezone=True), nullable=False, index=True)
    name = Column(String(64), nullable=False, index=True)
    rationale = Column(Text, nullable=False)
    expected_regime = Column(String(32), nullable=False)
    code = Column(Text, nullable=False)
    tests = Column(Text, nullable=False)
    params_to_search_json = Column(Text, nullable=False)
    review_status = Column(String(32), nullable=False, index=True)  # pending|accepted|rejected
    review_findings_json = Column(Text, nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)


def get_engine(db_path: str | Path = "data/state.db"):
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def session_for(db_path: str | Path = "data/state.db") -> Session:
    return Session(get_engine(db_path))
