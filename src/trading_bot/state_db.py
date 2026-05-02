"""state.db ORM models. Shared coordination surface for daemon, lab, supervisor.
WAL mode is enabled at engine creation in get_engine() so concurrent reads are safe.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
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


class OptionFill(Base):
    __tablename__ = "option_fills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    underlying = Column(String(16), nullable=False, index=True)
    contract_symbol = Column(String(32), nullable=False)
    option_type = Column(String(4), nullable=False)  # CSP|CC|ROLL
    side = Column(String(8), nullable=False)  # SELL|BUY
    strike = Column(Numeric(20, 4), nullable=False)
    expiration = Column(Date, nullable=False)
    qty = Column(Integer, nullable=False)
    premium = Column(Numeric(20, 4), nullable=False)
    alpaca_order_id = Column(String(64), nullable=False, unique=True)
    cycle_id = Column(String(64), nullable=True)
    notes = Column(Text, nullable=False, default="")


class OptionIvHistory(Base):
    __tablename__ = "option_iv_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(16), nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    atm_iv_30d = Column(Float, nullable=False)


class WheelCycle(Base):
    __tablename__ = "wheel_cycles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(String(64), nullable=False, unique=True)
    symbol = Column(String(16), nullable=False, index=True)
    phase = Column(String(32), nullable=False)
    opened_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    csp_contract = Column(String(32), nullable=True)
    csp_strike = Column(Numeric(20, 4), nullable=True)
    csp_expiration = Column(Date, nullable=True)
    csp_credit = Column(Numeric(20, 4), nullable=True)
    cc_contract = Column(String(32), nullable=True)
    cc_strike = Column(Numeric(20, 4), nullable=True)
    cc_expiration = Column(Date, nullable=True)
    cc_credit = Column(Numeric(20, 4), nullable=True)
    rolls_used = Column(Integer, nullable=False, default=0)
    cost_basis = Column(Numeric(20, 4), nullable=True)
    realized_pnl = Column(Numeric(20, 4), nullable=False, default=0)


class WheelUniverseCache(Base):
    __tablename__ = "wheel_universe_cache"
    symbol = Column(String(16), primary_key=True)
    eligible = Column(Boolean, nullable=False)
    reason = Column(Text, nullable=False, default="")
    cached_at = Column(DateTime(timezone=True), nullable=False)


class SectorCache(Base):
    __tablename__ = "sector_cache"
    symbol = Column(String(16), primary_key=True)
    sector = Column(String(32), nullable=False)
    industry = Column(String(64), nullable=False, default="")
    cached_at = Column(DateTime(timezone=True), nullable=False)


class Decisions(Base):
    """Append-only audit log of every Decision the bot makes.

    Mirrors the PDF strict JSON contract. JSON columns hold the five PDF
    sub-objects (risk_after, compliance, data_quality, execution_constraints,
    alerts, audit). Top-level columns are the ones that show up in dashboards.
    """
    __tablename__ = "decisions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(String(64), nullable=False, unique=True)
    timestamp_utc = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    action = Column(String(48), nullable=False, index=True)
    reason = Column(Text, nullable=False, default="")
    strategy = Column(String(64), nullable=False, default="", index=True)
    regime = Column(String(32), nullable=False, default="")
    asset_class = Column(String(16), nullable=False, default="")
    confidence = Column(Float, nullable=True)
    expected_edge_bps = Column(Float, nullable=True)
    risk_after_json = Column(Text, nullable=False, default="{}")
    compliance_json = Column(Text, nullable=False, default="{}")
    data_quality_json = Column(Text, nullable=False, default="{}")
    execution_constraints_json = Column(Text, nullable=False, default="{}")
    alerts_json = Column(Text, nullable=False, default="[]")
    audit_json = Column(Text, nullable=False, default="{}")
    entry_order_id = Column(String(64), nullable=False, default="")
    stop_loss_order_id = Column(String(64), nullable=False, default="")


class DecisionLesson(Base):
    """Post-mortem note for a decision whose trade has closed.

    Written by the ``decision_reflector`` role: for each closed trade, joins
    its ``entry_order_id`` back to a ``decisions`` row and records a 2-4
    sentence lesson plus optional categorical tags. Read by the architect
    and other learning roles via ``trading_bot.decision_lessons``.
    """
    __tablename__ = "decision_lessons"
    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(String(64), nullable=False, unique=True, index=True)
    entry_order_id = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    strategy = Column(String(64), nullable=False, index=True)
    regime = Column(String(32), nullable=False, default="")
    pnl_pct = Column(Float, nullable=False)
    hold_hours = Column(Float, nullable=False)
    lesson = Column(Text, nullable=False)
    tags_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), nullable=False)


class UnblockDebateRun(Base):
    """One row per unblock-committee debate. Persisted whether or not the
    verdict was acted upon — gives the dashboard + lesson loop a complete
    audit trail of where the LLM committee considered overriding a gate.

    When the verdict was 'place' AND the order subsequently filled,
    ``entry_order_id`` is back-filled by the reconciler joining on
    ``symbol`` + a small time window. ``closed_pnl_pct`` is the realized
    outcome once the trade closes — populated by the decision_reflector
    when reflecting on unblock-class lessons.
    """
    __tablename__ = "unblock_debate_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False, index=True)  # stock|crypto|wheel
    symbol = Column(String(32), nullable=False, index=True)
    candidate_score = Column(Float, nullable=True)
    block_reason = Column(Text, nullable=False, default="")
    overage_ratio = Column(Float, nullable=True)
    verdict = Column(String(16), nullable=False)  # place|reject
    confidence = Column(String(16), nullable=False)  # high|medium|low
    judge_reason = Column(Text, nullable=False, default="")
    aggressive_text = Column(Text, nullable=False, default="")
    conservative_text = Column(Text, nullable=False, default="")
    neutral_text = Column(Text, nullable=False, default="")
    entry_order_id = Column(String(64), nullable=True, index=True)
    closed_pnl_pct = Column(Float, nullable=True)
    synthetic = Column(Boolean, nullable=False, default=False)


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
