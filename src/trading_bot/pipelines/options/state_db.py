"""Options pipeline — owned tables (Phase 3).

Per Option 2: options owns its own tables. The options domain has
two structurally-novel concepts compared to stocks/crypto:

  1. **Contract-level positions** — one ``ContractPositionOptions`` row
     per (underlying, option_type, strike, expiry). A trader can hold
     multiple contracts on the same underlying simultaneously
     (e.g. long the $180 CSP, short the $190 CC).

  2. **Wheel state machine** — ``WheelCycle`` carries the current state:
     ``cash → CSP → assigned → CC → called_away → cash``. Each transition
     writes a new state-history row so the audit trail captures the
     full cycle. The wheel cycle joins to ``ContractPositionOptions``
     via ``cycle_id``.

  3. **Scout / Wheel debate audit tables** — Phase 3 mirrors the crypto
     pipeline pattern (per-pipeline scout + entry debate audit rows so
     options history doesn't mix with crypto/stocks history).

  4. **Options intel candidates** — Phase 3 adds an
     ``IntelCandidateOptions`` table parallel to crypto/stocks; rolls
     up IV rank + earnings + skew + flow signals per underlying.

  5. **Lessons + circuit breaker** — same isolation principle.

These tables coexist with the legacy ``trading_bot.state_db.WheelCycles``
table (which lives on the shared schema). Phase 3 reads from this new
per-pipeline table; Phase 2 stocks-extraction can later relocate the
legacy one if needed.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint,
)

from trading_bot.state_db import Base


class ContractPositionOptions(Base):
    """One open option contract position.

    Multiple rows can share an ``underlying`` — that's intentional, the
    wheel can hold a CSP at one strike + a CC at another strike on the
    same underlying simultaneously.
    """
    __tablename__ = "contract_positions_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    underlying = Column(String(16), nullable=False, index=True)
    option_type = Column(String(8), nullable=False)        # call | put
    side = Column(String(8), nullable=False)               # long | short
    strike = Column(Float, nullable=False)
    expiry = Column(DateTime(timezone=True), nullable=False, index=True)
    multiplier = Column(Integer, nullable=False, default=100)
    qty = Column(Integer, nullable=False)                  # contracts (signed)
    avg_open_price = Column(Float, nullable=False)         # per-share, NOT per-contract
    open_order_id = Column(String(64), nullable=True)
    cycle_id = Column(Integer, nullable=True, index=True)  # join to WheelCycleOptions
    opened_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    realized_pnl = Column(Float, nullable=True)            # backfilled on close
    __table_args__ = (
        UniqueConstraint(
            "underlying", "option_type", "strike", "expiry", "side",
            name="ux_contract_positions_options_dedup",
        ),
    )


class WheelCycleOptions(Base):
    """One wheel cycle from initial CSP entry through called-away exit.

    ``state`` is the current state machine position. State transitions
    are append-only via ``WheelStateHistoryOptions`` so the audit trail
    captures every step (cash → CSP at strike X, premium $Y → CSP
    expired worthless, premium kept → ...).
    """
    __tablename__ = "wheel_cycles_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    underlying = Column(String(16), nullable=False, index=True)
    state = Column(String(24), nullable=False)             # cash | csp_open | assigned | cc_open | called_away
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    initial_csp_strike = Column(Float, nullable=True)
    assignment_share_basis = Column(Float, nullable=True)  # cost basis on assignment
    final_called_away_at = Column(Float, nullable=True)    # exit strike on assignment
    cumulative_premium = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=True)            # backfilled on cycle close
    target_delta_csp = Column(Float, nullable=True)
    target_delta_cc = Column(Float, nullable=True)


class WheelStateHistoryOptions(Base):
    """Append-only state-transition log for one wheel cycle.

    Every state change in WheelCycleOptions writes a row here so the
    audit trail is reconstructable. ``transition`` describes the move
    (e.g. ``cash → csp_open``, ``csp_open → assigned``).
    """
    __tablename__ = "wheel_state_history_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, nullable=False, index=True)
    transitioned_at = Column(DateTime(timezone=True), nullable=False, index=True)
    from_state = Column(String(24), nullable=False)
    to_state = Column(String(24), nullable=False)
    transition = Column(String(48), nullable=False)
    details_json = Column(Text, nullable=False, default="{}")


class IntelEventOptions(Base):
    """Append-only audit row for one options intel mention (Phase 3).

    Mirrors ``IntelEventCrypto`` / ``IntelEvent`` shape. Examples:
      - earnings_calendar: upcoming earnings date for an underlying
      - cboe_skew: index-level skew from FRED (per timestamp, no symbol)
      - unusual_options_flow: large block trade or sweep on an underlying
    """
    __tablename__ = "intel_events_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    underlying = Column(String(16), nullable=False, index=True)
    source = Column(String(32), nullable=False, index=True)
    headline = Column(Text, nullable=False, default="")
    url = Column(Text, nullable=False, default="")
    sentiment = Column(Float, nullable=True)
    raw_score = Column(Float, nullable=True)
    event_at = Column(DateTime(timezone=True), nullable=True)
    ingested_at = Column(DateTime(timezone=True), nullable=False, index=True)
    event_hash = Column(String(64), nullable=False, default="")
    __table_args__ = (
        UniqueConstraint(
            "underlying", "source", "event_hash",
            name="ux_intel_events_options_dedup",
        ),
    )


class IntelCandidateOptions(Base):
    """One row per wheel candidate underlying. Parallel to IntelCandidateCrypto.

    Score-decayed roll-up of intel events; the scout debate reads the
    top-N rows here, runs Hank/Sofia/Marcus, and writes back
    ``scout_verdict='elevate'`` (or ``dismiss`` with a TTL).
    """
    __tablename__ = "intel_candidates_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    underlying = Column(String(16), nullable=False)
    score = Column(Float, nullable=False, index=True)
    n_mentions = Column(Integer, nullable=False, default=0)
    n_sources = Column(Integer, nullable=False, default=0)
    first_seen = Column(DateTime(timezone=True), nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False, index=True)
    top_reason = Column(Text, nullable=False, default="")
    sources_json = Column(Text, nullable=False, default="{}")
    sentiment_avg = Column(Float, nullable=True)
    rolled_up_at = Column(DateTime(timezone=True), nullable=False)
    # IV / earnings / skew context — set by intel sources
    iv_rank = Column(Float, nullable=True)
    earnings_in_dte_window = Column(Boolean, nullable=False, default=False)
    days_to_earnings = Column(Integer, nullable=True)
    cboe_skew = Column(Float, nullable=True)  # at most-recent ingest
    # Phase 3 scout-debate verdict + dismissal TTL (mirrors crypto)
    scout_verdict = Column(String(16), nullable=True)        # elevate | dismiss | NULL
    scout_dismissed_until = Column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    __table_args__ = (
        UniqueConstraint("underlying", name="ux_intel_candidates_options_underlying"),
    )


class ScoutDebateRunOptions(Base):
    """Audit log for Phase 3 options scout debate.

    Two-call shape (combined skeptic+analyst Sonnet + Opus judge).
    Mirrors ``ScoutDebateRunCrypto`` per-pipeline.
    """
    __tablename__ = "scout_debate_runs_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    underlying = Column(String(16), nullable=False, index=True)
    candidate_score = Column(Float, nullable=True)
    iv_rank = Column(Float, nullable=True)
    earnings_in_dte_window = Column(Boolean, nullable=False, default=False)
    top_reason = Column(Text, nullable=False, default="")
    verdict = Column(String(16), nullable=False)        # elevate | dismiss
    confidence = Column(String(16), nullable=False)     # high | medium | low
    judge_reason = Column(Text, nullable=False, default="")
    skeptic_text = Column(Text, nullable=False, default="")
    analyst_text = Column(Text, nullable=False, default="")
    prompt_version = Column(String(64), nullable=False, default="")
    synthetic = Column(Boolean, nullable=False, default=False)


class WheelDebateRunOptions(Base):
    """Audit log for Phase 3 options wheel-entry debate.

    Three-reviewer + judge shape (Aurelio/Beatrice/Yusuf → Catherine
    Lloyd). The judge produces ``chosen_delta`` and ``chosen_dte_days``
    along with the place/skip verdict so the broker submitter knows the
    final structure to use.
    """
    __tablename__ = "wheel_debate_runs_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    underlying = Column(String(16), nullable=False, index=True)
    candidate_score = Column(Float, nullable=True)
    iv_rank = Column(Float, nullable=True)
    proposed_delta = Column(Float, nullable=True)
    proposed_dte_days = Column(Integer, nullable=True)
    proposed_strike = Column(Float, nullable=True)
    regime = Column(String(32), nullable=True)
    verdict = Column(String(16), nullable=False)        # place | skip | defer_restale
    confidence = Column(String(16), nullable=False)
    chosen_delta = Column(Float, nullable=True)
    chosen_dte_days = Column(Integer, nullable=True)
    chosen_structure = Column(String(16), nullable=True)  # csp | cc | vertical | cash
    judge_reason = Column(Text, nullable=False, default="")
    aggressive_text = Column(Text, nullable=False, default="")
    conservative_text = Column(Text, nullable=False, default="")
    neutral_text = Column(Text, nullable=False, default="")
    entry_order_id = Column(String(64), nullable=True)
    cycle_id = Column(Integer, nullable=True)            # joins to wheel_cycles_options
    prompt_version = Column(String(64), nullable=False, default="")
    synthetic = Column(Boolean, nullable=False, default=False)


class DebateLessonOptions(Base):
    """Phase 3 — nightly aggregated lessons from options debate outcomes.

    One row per (analysis_date, lookback_days) pair. Read by the next
    debate's ``RECENT LESSONS`` block. Adds two options-native attribution
    dimensions on top of the crypto/stocks shape:
      ``per_iv_rank_band_winrate_json``  — winrate keyed by IV rank
                                            band (low / mid / high)
      ``per_dte_band_winrate_json``      — winrate keyed by DTE band
                                            (weekly / monthly / quarterly)
    """
    __tablename__ = "debate_lessons_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_date = Column(DateTime(timezone=True), nullable=False, index=True)
    lookback_days = Column(Integer, nullable=False)
    n_cycles_closed = Column(Integer, nullable=False, default=0)
    n_wheel_debates = Column(Integer, nullable=False, default=0)
    n_scout_debates = Column(Integer, nullable=False, default=0)
    summary_text = Column(Text, nullable=False, default="")
    per_source_winrate_json = Column(Text, nullable=False, default="{}")
    per_iv_rank_band_winrate_json = Column(Text, nullable=False, default="{}")
    per_dte_band_winrate_json = Column(Text, nullable=False, default="{}")
    per_structure_winrate_json = Column(Text, nullable=False, default="{}")
    candidate_prompt_edits_json = Column(Text, nullable=False, default="[]")
    prompt_version = Column(String(64), nullable=False, default="")


class CircuitBreakerEventOptions(Base):
    """Phase 3 — append-only audit row per options circuit-breaker trip.

    Independent from stocks + crypto breakers. Trip conditions are
    options-specific (VIX spike, earnings season clustering, term-structure
    inversion). cleared_at is set when cooldown elapses or the operator
    manually clears.
    """
    __tablename__ = "circuit_breaker_events_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tripped_at = Column(DateTime(timezone=True), nullable=False, index=True)
    cleared_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(String(48), nullable=False)
    severity = Column(String(16), nullable=False, default="warning")  # warning | hard
    trip_state_json = Column(Text, nullable=False, default="{}")
    cooldown_minutes = Column(Integer, nullable=False, default=60)
