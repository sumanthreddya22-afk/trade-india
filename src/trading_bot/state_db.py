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
    UniqueConstraint,
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
    # Phase D — composed persona/version tag captured at debate time.
    prompt_version = Column(String(64), nullable=False, default="")


class EntryDebateRun(Base):
    """One row per pre-trade entry-committee debate. Same audit shape as
    ``UnblockDebateRun`` so the dashboard + lessons loop can consume both
    uniformly. ``intel_score`` + ``signal_reason`` + ``regime`` capture
    the entry-side context (the unblock counterpart uses ``overage_ratio``
    + ``block_reason`` for the rejection-override side).

    ``entry_order_id`` is back-filled by the reconciler when the order
    fills; ``closed_pnl_pct`` populated when the position closes — those
    are the join columns the decision_reflector uses to write entry-class
    lessons.
    """
    __tablename__ = "entry_debate_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False, index=True)  # stock|crypto
    symbol = Column(String(32), nullable=False, index=True)
    intel_score = Column(Float, nullable=True)
    signal_reason = Column(Text, nullable=False, default="")
    regime = Column(String(32), nullable=False, default="")
    verdict = Column(String(16), nullable=False)  # place|skip
    confidence = Column(String(16), nullable=False)  # high|medium|low
    judge_reason = Column(Text, nullable=False, default="")
    aggressive_text = Column(Text, nullable=False, default="")
    conservative_text = Column(Text, nullable=False, default="")
    neutral_text = Column(Text, nullable=False, default="")
    entry_order_id = Column(String(64), nullable=True, index=True)
    closed_pnl_pct = Column(Float, nullable=True)
    synthetic = Column(Boolean, nullable=False, default=False)
    # Phase D — composed persona/version tag captured at debate time.
    prompt_version = Column(String(64), nullable=False, default="")


class IntelEvent(Base):
    """Append-only audit row for a single news/filing/social mention.

    The ingester collects events from each source, computes an event_hash
    (typically SHA1 of source+url) for dedup, and inserts here. The
    aggregator reads from this table to roll up per-symbol scores into
    ``intel_candidates``.
    """
    __tablename__ = "intel_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False)  # stock|crypto|option_underlying
    source = Column(String(32), nullable=False, index=True)
    headline = Column(Text, nullable=False, default="")
    url = Column(Text, nullable=False, default="")
    sentiment = Column(Float, nullable=True)         # -1..+1
    raw_score = Column(Float, nullable=True)         # source-native score (e.g. GDELT tone)
    event_at = Column(DateTime(timezone=True), nullable=True)
    ingested_at = Column(DateTime(timezone=True), nullable=False, index=True)
    event_hash = Column(String(64), nullable=False, default="")
    __table_args__ = (
        UniqueConstraint("symbol", "source", "event_hash", name="ux_intel_events_dedup"),
    )


class IntelCandidate(Base):
    """Aggregated, score-decayed candidate. ONE row per (symbol, asset_class).

    Daemon scans read this table FIRST (preferred over opportunities.md /
    scout JSON / wheel allowlist / CORE_LIQUID_TICKERS). The intel ingestor
    role updates rows on each tick (every ~30 min market hours, hourly
    after-hours).

    ``sources_json`` is a JSON object: {source_name: count} so the dashboard
    can show "saw this in: 3 news, 1 SEC filing, 12 reddit". ``top_reason``
    is the headline of the highest-weighted recent event for human-readable
    "why is this here".
    """
    __tablename__ = "intel_candidates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False)
    asset_class = Column(String(16), nullable=False)
    score = Column(Float, nullable=False, index=True)
    n_mentions = Column(Integer, nullable=False, default=0)
    n_sources = Column(Integer, nullable=False, default=0)
    first_seen = Column(DateTime(timezone=True), nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False, index=True)
    top_reason = Column(Text, nullable=False, default="")
    sources_json = Column(Text, nullable=False, default="{}")
    sentiment_avg = Column(Float, nullable=True)
    rolled_up_at = Column(DateTime(timezone=True), nullable=False)
    # Phase F — adversarial-defense flags (set by aggregator before scoring).
    dedup_url_hashes_json = Column(Text, nullable=False, default="[]")
    suspicious_spike = Column(Boolean, nullable=False, default=False)
    coordinated = Column(Boolean, nullable=False, default=False)
    pump_signature = Column(Boolean, nullable=False, default=False)
    # Phase B — scout-debate verdict and dismissal TTL.
    # scout_verdict: 'elevate' | 'dismiss' | NULL (not yet debated).
    # scout_dismissed_until: TTL timestamp; pool readers filter rows where
    # this value is in the future. NULL = not dismissed.
    scout_verdict = Column(String(16), nullable=True)
    scout_dismissed_until = Column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    __table_args__ = (
        UniqueConstraint("symbol", "asset_class", name="ux_intel_candidates_symbol_class"),
    )


class ScoutDebateRun(Base):
    """Audit log for the scout debate (Phase B).

    Mirrors EntryDebateRun shape so the dashboard + lessons loop can treat
    all debate tables uniformly. One row per (symbol, debate-tick) pair —
    a batched debate covering 4 symbols writes 4 rows.
    """
    __tablename__ = "scout_debate_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False)
    symbol = Column(String(32), nullable=False, index=True)
    candidate_score = Column(Float, nullable=True)
    top_reason = Column(Text, nullable=False, default="")
    verdict = Column(String(16), nullable=False)       # elevate | dismiss
    confidence = Column(String(16), nullable=False)    # high | medium | low
    judge_reason = Column(Text, nullable=False, default="")
    skeptic_text = Column(Text, nullable=False, default="")
    analyst_text = Column(Text, nullable=False, default="")
    prompt_version = Column(String(32), nullable=False, default="")
    synthetic = Column(Boolean, nullable=False, default=False)


class TradeIntelSnapshot(Base):
    """Phase C — snapshot of intel state at order placement.

    Captured once per order so the hold debate has a stable baseline to
    compare against (e.g. "score dropped 50% from entry"). Keyed by
    entry_order_id which matches trade_journal._TradeRow.entry_order_id.

    Stored in state.db (Alembic-managed) instead of mutating trade_journal's
    schema — keeps the trade journal backwards-compatible.
    """
    __tablename__ = "trade_intel_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_order_id = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    entry_intel_score = Column(Float, nullable=True)
    entry_top_reason = Column(Text, nullable=False, default="")
    entry_sentiment_avg = Column(Float, nullable=True)
    entry_top_sources_json = Column(Text, nullable=False, default="[]")


class HoldDebateRun(Base):
    """Audit log for the hold debate (Phase C).

    One row per hold-debate firing. Mirrors EntryDebateRun + adds hold-
    specific columns for trigger reason and entry-vs-current comparison.
    """
    __tablename__ = "hold_debate_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False)
    symbol = Column(String(32), nullable=False, index=True)
    entry_order_id = Column(String(64), nullable=True, index=True)
    trigger_reason = Column(String(64), nullable=False, default="")
    current_score = Column(Float, nullable=True)
    current_sentiment = Column(Float, nullable=True)
    entry_score = Column(Float, nullable=True)
    entry_sentiment = Column(Float, nullable=True)
    verdict = Column(String(16), nullable=False)       # hold | tighten_stop | exit_now
    confidence = Column(String(16), nullable=False)    # high | medium | low
    judge_reason = Column(Text, nullable=False, default="")
    aggressive_text = Column(Text, nullable=False, default="")
    conservative_text = Column(Text, nullable=False, default="")
    neutral_text = Column(Text, nullable=False, default="")
    action_taken = Column(String(32), nullable=False, default="")  # none | stop_replaced | flattened
    resulting_pnl_pct = Column(Float, nullable=True)               # backfilled on close
    prompt_version = Column(String(64), nullable=False, default="")
    synthetic = Column(Boolean, nullable=False, default=False)


class IntelStreamEvent(Base):
    """Phase G — events captured by the EventStreamer (fast-poll SEC EDGAR
    or websocket-style sources). Distinct from IntelEvent so the express-
    lane handler can find unprocessed rows without touching the polled
    history. ``processed_at`` flips when the express scout/hold debate
    has dispatched.
    """
    __tablename__ = "intel_stream_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    asset_class = Column(String(16), nullable=False)
    source = Column(String(32), nullable=False)
    headline = Column(Text, nullable=False, default="")
    url = Column(Text, nullable=False, default="")
    sentiment = Column(Float, nullable=True)
    event_at = Column(DateTime(timezone=True), nullable=True)
    ingested_at = Column(DateTime(timezone=True), nullable=False, index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    event_hash = Column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("source", "event_hash", name="ux_intel_stream_events_hash"),
    )


class DebateQueue(Base):
    """Phase G — priority queue replacing the hard daily-cap drop. Entries
    queued throughout the day; dispatcher processes top-N by ``priority_score``
    up to the daily budget. Demoted rows roll over rather than being silently
    dropped.
    """
    __tablename__ = "debate_queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    debate_class = Column(String(16), nullable=False, index=True)  # entry|scout|hold
    symbol = Column(String(32), nullable=False)
    asset_class = Column(String(16), nullable=False)
    priority_score = Column(Float, nullable=False, index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    queued_at = Column(DateTime(timezone=True), nullable=False, index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String(32), nullable=True)  # processed | demoted | expired


class CircuitBreakerEvent(Base):
    """Phase F — audit row per circuit-breaker trip / clear event.

    Append-only. The active state is the most recent row whose action='tripped'
    AND (expires_at IS NULL OR expires_at > now); a 'cleared' row supersedes
    a prior trip.
    """
    __tablename__ = "circuit_breaker_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_at = Column(DateTime(timezone=True), nullable=False, index=True)
    action = Column(String(16), nullable=False)   # tripped | cleared
    reason = Column(String(64), nullable=False)
    detail_json = Column(Text, nullable=False, default="{}")
    expires_at = Column(DateTime(timezone=True), nullable=True)


class DebateLesson(Base):
    """Phase D — nightly outcome-analyzer summary.

    One row per analysis run (typically nightly). Joins entry/unblock/hold
    debate runs with closed-trade outcomes (last N days) and persists the
    lessons block that the next debate's brief will inject under "RECENT
    LESSONS". Mutating prompts requires operator review (Layer 2); the
    in-context lesson injection (Layer 1) updates automatically.
    """
    __tablename__ = "debate_lessons"
    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_date = Column(DateTime(timezone=True), nullable=False, index=True)
    lookback_days = Column(Integer, nullable=False)
    n_trades_closed = Column(Integer, nullable=False, default=0)
    n_entry_debates = Column(Integer, nullable=False, default=0)
    n_unblock_debates = Column(Integer, nullable=False, default=0)
    n_hold_debates = Column(Integer, nullable=False, default=0)
    overall_place_winrate = Column(Float, nullable=True)
    overall_skip_winrate = Column(Float, nullable=True)
    summary_text = Column(Text, nullable=False, default="")
    per_source_winrate_json = Column(Text, nullable=False, default="{}")
    per_verdict_winrate_json = Column(Text, nullable=False, default="{}")
    losing_patterns_json = Column(Text, nullable=False, default="[]")
    shadow_skips_json = Column(Text, nullable=False, default="[]")
    candidate_edits_json = Column(Text, nullable=False, default="[]")
    prompt_version = Column(String(64), nullable=False, default="")


class Event(Base):
    """Cross-process event bus row (Phase 0 of real-time dashboard).

    Every producer in any process (daemon / lab / supervisor / mailbox /
    dashboard) writes here via ``trading_bot.event_bus.bus.emit``. The
    dashboard SSE endpoint tails this table and fans events out to
    connected browser clients.

    Append-only. Retention is enforced by a nightly job that DELETEs rows
    older than 7 days and runs ``PRAGMA wal_checkpoint(TRUNCATE)``.
    Payload is a small JSON blob (kept compact — never put price ticks
    here; those have their own ephemeral channel inside the dashboard
    process). ``process`` records which launchd process emitted the row,
    so the dashboard can show per-process health.
    """
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(64), nullable=False, index=True)
    payload = Column(Text, nullable=False, default="{}")
    source = Column(String(64), nullable=False, default="")
    process = Column(String(16), nullable=False, default="unknown")
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)


class ThresholdOverride(Base):
    """Adaptive override for a single risk/wheel/debate threshold knob.

    Written nightly by ``threshold_tuner``. Read at trade time by
    ``risk_manager``, ``wheel_lane``, ``chain.pick_csp_contract``, and
    the orchestrator's unblock-debate predicate via
    ``trading_bot.threshold_overrides.lookup``.

    Freshness gate (``max_age_hours``) is applied at the read site, not
    here — keeping the table append-only makes it easy to audit *why*
    a knob moved on a given day. The ``expires_at`` column is the
    explicit kill-switch: an operator can set it in the past to disable
    a row without deleting it.
    """
    __tablename__ = "threshold_overrides"
    id = Column(Integer, primary_key=True, autoincrement=True)
    knob = Column(String(64), nullable=False, index=True)
    value = Column(Float, nullable=False)
    regime = Column(String(32), nullable=True)
    bounds_min = Column(Float, nullable=False)
    bounds_max = Column(Float, nullable=False)
    set_at = Column(DateTime(timezone=True), nullable=False, index=True)
    set_by = Column(String(64), nullable=False)  # threshold_tuner | operator | operator_kill
    signal_summary = Column(Text, nullable=False, default="{}")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # Phase E — shadow-mode rollout. Live readers must skip rows with shadow=True.
    # The tuner backfills shadow_what_if_pnl after the shadow window closes;
    # if positive vs the live row, the operator (or auto-promoter) flips
    # the row's shadow flag to False to make it live.
    shadow = Column(Boolean, nullable=False, default=False)
    shadow_what_if_pnl = Column(Float, nullable=True)


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
