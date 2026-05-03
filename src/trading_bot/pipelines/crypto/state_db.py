"""Crypto pipeline — owned tables.

Per Option 2 (three independent pipelines), crypto owns its own tables.
Same shape as the legacy ``trading_bot.state_db.IntelEvent``,
``IntelCandidate``, and ``IntelStreamEvent`` so the existing aggregator /
adversarial / scoring patterns port over cleanly — but distinct
``*_crypto`` table names so crypto data never mixes with stocks data
and crypto schema can evolve independently (e.g. crypto-native columns
like ``chain``, ``tx_hash``, ``cold_start_token`` flag) without
disturbing the stocks tables.

Schema convention:
- ``intel_events_crypto``:        Phase 1A — append-only audit row per source mention
- ``intel_candidates_crypto``:    Phase 1A — aggregated candidate (ONE per symbol)
- ``intel_stream_events_crypto``: Phase 1G — express-lane events (Whale Alert, Coinbase WS, etc.)

The ``Base`` is the same global SQLAlchemy declarative base — same
``state.db`` file. Per-pipeline isolation is at the *table* level, not
the database level (single Alpaca account, single buying-power view —
sharing one DB is correct).
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint,
)

# Reuse the global declarative Base so migrations + sessions stay unified.
# Per-pipeline tables are *named* to be isolated; they live in the same DB.
from trading_bot.state_db import Base


class IntelEventCrypto(Base):
    """Append-only audit row for a single crypto source mention.

    Mirrors ``IntelEvent`` shape so the existing aggregator/scoring code
    ports over cleanly. Crypto-specific fields added: ``chain`` (eth, sol,
    btc, etc.) and ``tx_hash`` (set when source is on-chain).
    """
    __tablename__ = "intel_events_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    source = Column(String(32), nullable=False, index=True)
    headline = Column(Text, nullable=False, default="")
    url = Column(Text, nullable=False, default="")
    sentiment = Column(Float, nullable=True)         # -1..+1
    raw_score = Column(Float, nullable=True)         # source-native score
    event_at = Column(DateTime(timezone=True), nullable=True)
    ingested_at = Column(DateTime(timezone=True), nullable=False, index=True)
    event_hash = Column(String(64), nullable=False, default="")
    chain = Column(String(16), nullable=True)        # eth | sol | btc | bsc | arb | base | etc.
    tx_hash = Column(String(80), nullable=True)      # set for on-chain events
    __table_args__ = (
        UniqueConstraint(
            "symbol", "source", "event_hash",
            name="ux_intel_events_crypto_dedup",
        ),
    )


class IntelCandidateCrypto(Base):
    """Aggregated, score-decayed crypto candidate. ONE row per symbol.

    Mirrors ``IntelCandidate``. Crypto-specific adversarial flags added:
    cold_start_token, whale_concentration, honeypot_detected, sybil_coordinated
    (set during Phase F.2 adversarial check).
    """
    __tablename__ = "intel_candidates_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False)
    score = Column(Float, nullable=False, index=True)
    n_mentions = Column(Integer, nullable=False, default=0)
    n_sources = Column(Integer, nullable=False, default=0)
    first_seen = Column(DateTime(timezone=True), nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False, index=True)
    top_reason = Column(Text, nullable=False, default="")
    sources_json = Column(Text, nullable=False, default="{}")
    sentiment_avg = Column(Float, nullable=True)
    rolled_up_at = Column(DateTime(timezone=True), nullable=False)
    # Phase F shared adversarial flags (URL dedup, velocity spike, coordination, pump signature)
    dedup_url_hashes_json = Column(Text, nullable=False, default="[]")
    suspicious_spike = Column(Boolean, nullable=False, default=False)
    coordinated = Column(Boolean, nullable=False, default=False)
    pump_signature = Column(Boolean, nullable=False, default=False)
    # Phase F.2 crypto-specific adversarial flags
    cold_start_token = Column(Boolean, nullable=False, default=False)
    whale_concentration = Column(Boolean, nullable=False, default=False)
    honeypot_detected = Column(Boolean, nullable=False, default=False)
    sybil_coordinated = Column(Boolean, nullable=False, default=False)
    # Phase B scout-debate verdict + dismissal TTL.
    scout_verdict = Column(String(16), nullable=True)        # elevate | dismiss | NULL
    scout_dismissed_until = Column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    __table_args__ = (
        UniqueConstraint("symbol", name="ux_intel_candidates_crypto_symbol"),
    )


class ScoutDebateRunCrypto(Base):
    """Audit log for Phase 1B crypto scout debate (one row per debated symbol).

    Mirrors ``trading_bot.state_db.ScoutDebateRun`` but per-pipeline so
    crypto scout history doesn't mix with stocks history. Two-call
    debate (combined skeptic+analyst Sonnet call + Opus judge call)
    means ``skeptic_text`` and ``analyst_text`` come from the same LLM
    call, while ``judge_reason`` is a separate audit-of-record call.
    """
    __tablename__ = "scout_debate_runs_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    candidate_score = Column(Float, nullable=True)
    top_reason = Column(Text, nullable=False, default="")
    verdict = Column(String(16), nullable=False)        # elevate | dismiss
    confidence = Column(String(16), nullable=False)     # high | medium | low
    judge_reason = Column(Text, nullable=False, default="")
    skeptic_text = Column(Text, nullable=False, default="")
    analyst_text = Column(Text, nullable=False, default="")
    prompt_version = Column(String(64), nullable=False, default="")
    synthetic = Column(Boolean, nullable=False, default=False)


class HoldDebateRunCrypto(Base):
    """Audit log for Phase 1C crypto hold debate.

    One row per hold-debate firing. Mirrors ``trading_bot.state_db.HoldDebateRun``
    but per-pipeline so crypto hold history doesn't mix with stocks.
    Two-call shape (combined Sonnet reviewers + Opus judge) is the same
    as scout — ``aggressive_text`` / ``conservative_text`` / ``neutral_text``
    come from one Sonnet call; ``judge_reason`` is the Opus audit-of-record.
    """
    __tablename__ = "hold_debate_runs_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    entry_order_id = Column(String(64), nullable=True, index=True)
    trigger_reason = Column(String(64), nullable=False, default="")  # one of the crypto trigger names
    current_score = Column(Float, nullable=True)
    current_sentiment = Column(Float, nullable=True)
    entry_score = Column(Float, nullable=True)
    entry_sentiment = Column(Float, nullable=True)
    verdict = Column(String(16), nullable=False)        # hold | tighten_stop | exit_now
    confidence = Column(String(16), nullable=False)     # high | medium | low
    judge_reason = Column(Text, nullable=False, default="")
    aggressive_text = Column(Text, nullable=False, default="")
    conservative_text = Column(Text, nullable=False, default="")
    neutral_text = Column(Text, nullable=False, default="")
    action_taken = Column(String(32), nullable=False, default="")  # none | stop_replaced | flattened
    resulting_pnl_pct = Column(Float, nullable=True)               # backfilled on close
    prompt_version = Column(String(64), nullable=False, default="")
    synthetic = Column(Boolean, nullable=False, default=False)


class DebateLessonCrypto(Base):
    """Phase 1D — nightly aggregated lessons from crypto debate outcomes.

    One row per (analysis_date, lookback_days) pair. Generated by the
    crypto outcome analyzer role, consumed by the next debate's
    ``RECENT LESSONS`` brief block.

    Schema mirrors the shared ``DebateLesson`` shape but adds two
    crypto-native attribution dimensions:
      ``per_chain_winrate_json``     — winrate keyed by chain (eth, sol, ...)
      ``per_funding_band_winrate_json`` — winrate keyed by funding band
                                           (low / neutral / high / extreme)
    """
    __tablename__ = "debate_lessons_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_date = Column(DateTime(timezone=True), nullable=False, index=True)
    lookback_days = Column(Integer, nullable=False)
    n_trades_closed = Column(Integer, nullable=False, default=0)
    n_hold_debates = Column(Integer, nullable=False, default=0)
    n_scout_debates = Column(Integer, nullable=False, default=0)
    summary_text = Column(Text, nullable=False, default="")
    per_source_winrate_json = Column(Text, nullable=False, default="{}")
    per_trigger_winrate_json = Column(Text, nullable=False, default="{}")
    per_chain_winrate_json = Column(Text, nullable=False, default="{}")
    per_funding_band_winrate_json = Column(Text, nullable=False, default="{}")
    candidate_prompt_edits_json = Column(Text, nullable=False, default="[]")
    prompt_version = Column(String(64), nullable=False, default="")


class CircuitBreakerEventCrypto(Base):
    """Phase 1F — append-only audit row per crypto circuit-breaker trip.

    The crypto breaker (``pipelines/crypto/circuit_breaker.evaluate_crypto_metrics``)
    is independent from the stocks breaker — different trip conditions
    (BTC 4h drawdown, funding extreme, stablecoin depeg, exchange API
    error, liquidation cascade), different lookback windows, and the
    crypto trip should NOT block stock trades or vice versa.

    ``cleared_at`` is set when the cooldown elapses or the operator
    manually clears. ``trip_state`` carries the raw metrics that
    crossed the trip threshold for after-the-fact debugging.
    """
    __tablename__ = "circuit_breaker_events_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tripped_at = Column(DateTime(timezone=True), nullable=False, index=True)
    cleared_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(String(48), nullable=False)
    severity = Column(String(16), nullable=False, default="warning")  # warning | hard
    trip_state_json = Column(Text, nullable=False, default="{}")
    cooldown_minutes = Column(Integer, nullable=False, default=30)


class ThresholdOverrideCrypto(Base):
    """Phase 1E — adaptive threshold overrides for the crypto pipeline.

    Append-only log of threshold proposals from the threshold-tuner.
    Reader (``adaptive_thresholds.lookup_threshold``) returns the most
    recent live (non-shadow) row per (knob, regime) pair, falling back
    to a YAML/code default when no override exists.

    Shadow mode (``shadow=True``) lets the tuner experiment for 14 days
    without affecting actual trades — the analyzer compares shadow
    "what-if" outcomes vs. the live threshold's actual outcomes; once
    the shadow's expected value beats the live threshold, the operator
    flips it to live.
    """
    __tablename__ = "threshold_overrides_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    knob = Column(String(64), nullable=False, index=True)        # e.g. "intel_threshold"
    regime = Column(String(32), nullable=True, index=True)        # e.g. "crypto_range"; null for global
    proposed_value = Column(Float, nullable=False)
    rationale = Column(Text, nullable=False, default="")
    proposed_by = Column(String(64), nullable=False, default="threshold_tuner")
    proposed_at = Column(DateTime(timezone=True), nullable=False, index=True)
    shadow = Column(Boolean, nullable=False, default=True)
    shadow_what_if_pnl = Column(Float, nullable=True)            # backfilled by analyzer
    promoted_to_live_at = Column(DateTime(timezone=True), nullable=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)


class IntelStreamEventCrypto(Base):
    """Express-lane crypto stream events (Phase 1G).

    Whale Alert pulls, Coinbase Pro WebSocket pushes, Binance funding /
    liquidations, Etherscan tracked-wallet polls — all land here.
    Distinct from ``IntelEventCrypto`` so the express-lane handler can
    find unprocessed rows without touching the polled history.
    """
    __tablename__ = "intel_stream_events_crypto"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    source = Column(String(32), nullable=False)
    payload = Column(Text, nullable=False, default="{}")  # raw JSON for audit
    sentiment = Column(Float, nullable=True)
    event_at = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), nullable=False)
    chain = Column(String(16), nullable=True)
    tx_hash = Column(String(80), nullable=True)
    event_hash = Column(String(64), nullable=False, default="")
    processed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    __table_args__ = (
        UniqueConstraint(
            "symbol", "source", "event_hash",
            name="ux_intel_stream_events_crypto_dedup",
        ),
    )
