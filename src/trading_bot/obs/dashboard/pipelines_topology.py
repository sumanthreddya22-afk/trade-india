"""Pipeline-oriented topology for the System page.

Replaces the previous zone-based node graph with three explicit
pipelines (stocks / crypto / options), each modelled as a linear
chain of stages. The system page renders one column per pipeline so
the operator sees: signal → roll-up → scout → entry → hold → lesson,
with the named bot operators staffing each LLM stage.

Why three columns instead of one big graph: each pipeline operates
independently (per ADR — Option 2). When the operator asks "is crypto
working?" they care about the crypto column only. A unified graph
buries that signal under cross-pipeline arrows.

Each stage references:
  - ``role_name``     — what shows up in role_runs.role_name (drives health)
  - ``count_query``   — SQL returning a single integer (today's volume)
  - ``persona_ids``   — operator personas, resolved via shared.role_persona_map

Stages with no LLM (sources, aggregators, state machines) leave
``persona_ids`` empty; the renderer shows "automated" in that case.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Stage:
    """One step in a pipeline's data flow."""
    id: str                                       # kebab-cased; DOM id
    label: str                                    # human title
    description: str = ""                         # one-line subtitle
    role_name: Optional[str] = None               # health source in role_runs
    persona_ids: Tuple[str, ...] = ()             # operators; empty = automated
    count_query: Optional[str] = None             # SQL → int (today's volume)
    count_label: str = ""                         # what the integer means
    table_name: Optional[str] = None              # for "rows in DB" badge
    icon: str = "▢"                               # small glyph
    kind: str = "automated"                       # automated | scout | entry | hold | wheel | lesson | state | universe | risk | broker | steward | monitor | reconcile | audit
    # Operator-visible note about a known gap on this stage. Renders
    # under the description in italic amber when set. Examples:
    # "needs ANTHROPIC_API_KEY", "wired but not yet scheduled", etc.
    blocked_note: str = ""
    # Override the "stale" threshold used in health logic (hours). Default 36
    # covers overnight + buffer; set to 90 for roles that only run during
    # market hours so weekends don't trigger false "warn".
    stale_hours: int = 36


@dataclass(frozen=True)
class Pipeline:
    """One vertical column on the System page."""
    id: str
    label: str
    icon: str
    color: str          # accent CSS color (cyan-300 / amber-300 / fuchsia-300)
    description: str
    stages: Tuple[Stage, ...]


# ---------------------------------------------------------------------------
# Persona id constants (mirror role_persona_map for easy maintenance)
# ---------------------------------------------------------------------------

_STOCKS_SCOUT = (
    "stocks_scout_skeptic_v1",
    "stocks_scout_analyst_v1",
    "stocks_scout_judge_v1",
)
_STOCKS_HOLD = (
    "stocks_hold_aggressive_v1",
    "stocks_hold_conservative_v1",
    "stocks_hold_neutral_v1",
    "stocks_hold_judge_v1",
)
_STOCKS_LESSON = ("stocks_lesson_analyst_v1",)

_CRYPTO_SCOUT = (
    "crypto_scout_skeptic_v1",
    "crypto_scout_analyst_v1",
    "crypto_scout_judge_v1",
)
_CRYPTO_ENTRY = (
    "crypto_entry_aggressive_v1",
    "crypto_entry_conservative_v1",
    "crypto_entry_neutral_v1",
    "crypto_entry_judge_v1",
)
_CRYPTO_HOLD = (
    "crypto_hold_aggressive_v1",
    "crypto_hold_conservative_v1",
    "crypto_hold_neutral_v1",
    "crypto_hold_judge_v1",
)
_CRYPTO_LESSON = ("crypto_lesson_analyst_v1",)

_OPTIONS_SCOUT = (
    "options_scout_skeptic_v1",
    "options_scout_analyst_v1",
    "options_scout_judge_v1",
)
_OPTIONS_WHEEL = (
    "options_wheel_aggressive_v1",
    "options_wheel_conservative_v1",
    "options_wheel_neutral_v1",
    "options_wheel_judge_v1",
)
_OPTIONS_HOLD = (
    "options_wheel_aggressive_v1",
    "options_wheel_conservative_v1",
    "options_wheel_neutral_v1",
    "options_wheel_judge_v1",
)
_OPTIONS_LESSON = ("options_lesson_analyst_v1",)


# ---------------------------------------------------------------------------
# Stocks pipeline
# ---------------------------------------------------------------------------

_STOCKS_STAGES: Tuple[Stage, ...] = (
    Stage(
        id="stocks-universe",
        label="Universe Build",
        description="Pre-market rank (07:30 ET) + Polygon grouped refresh. Builds the daily watchlist.",
        role_name="universe_curator",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='universe_curator' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="universe builds today",
        icon="🌐",
        kind="universe",
    ),
    Stage(
        id="stocks-sources",
        label="Intel Sources",
        description="SEC EDGAR · Alpaca News · ApeWisdom · GDELT · Finnhub · VIP feeds",
        role_name="intel_ingestor",
        count_query="""
            SELECT COUNT(*) FROM intel_events
            WHERE ingested_at >= datetime('now', '-1 day')
        """,
        count_label="events today",
        table_name="intel_events",
        icon="📡",
        kind="automated",
    ),
    Stage(
        id="stocks-aggregator",
        label="Aggregator",
        description="Score-decay + cross-source bonus + adversarial flags",
        role_name="intel_ingestor",
        count_query="SELECT COUNT(*) FROM intel_candidates",
        count_label="candidates pooled",
        table_name="intel_candidates",
        icon="⚖",
        kind="automated",
    ),
    Stage(
        id="stocks-cb-gate",
        label="Circuit-Breaker Gate",
        description="Pre-trade halt on macro shock / VIX spike / daily-loss limit. Trips bypass entry debate entirely.",
        role_name=None,  # synchronous gate inside the orchestrator
        count_query="""
            SELECT COUNT(*) FROM circuit_breaker_events
            WHERE tripped_at >= datetime('now', '-7 day')
        """,
        count_label="trips this week",
        icon="🚦",
        kind="risk",
    ),
    Stage(
        id="stocks-scout",
        label="Scout Debate",
        description="Two-call: skeptic + analyst → judge. Elevate or dismiss.",
        role_name="intel_ingestor",  # scout debate fires inside intel_ingestor
        persona_ids=_STOCKS_SCOUT,
        count_query="""
            SELECT COUNT(*) FROM scout_debate_runs
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="scout_debate_runs",
        icon="🔭",
        kind="scout",
        # As of 2026-05-03 the legacy MailboxBackedClient now delegates to
        # ClaudeCliTransport (subscription) by default, so the stocks
        # debates ride the same transport as crypto + options. No
        # blocked_note here — wait for first run, then health logic
        # determines whether it's actually firing.
    ),
    Stage(
        id="stocks-entry",
        label="Entry Debate",
        description="Pre-trade committee. Place / skip / defer with sized qty. Stocks with no intel_score are pre-filtered (skipped_no_intel) before reaching the LLM committee.",
        role_name="stock_scanner",
        persona_ids=_STOCKS_HOLD,  # stocks reuses hold-tier personas for entry
        count_query="""
            SELECT COUNT(*) FROM entry_debate_runs
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="entry_debate_runs",
        icon="🎯",
        kind="entry",
        stale_hours=90,  # market-hours only — covers Fri close → Mon open gap

    ),
    Stage(
        id="stocks-risk-gate",
        label="Risk Manager",
        description="Sector cap · gross/net cap · daily-loss limit · per-trade risk%",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM decisions
            WHERE timestamp_utc >= datetime('now', '-1 day')
              AND action LIKE 'rejected_by_risk%'
        """,
        count_label="risk rejects today",
        icon="🛂",
        kind="risk",
    ),
    Stage(
        id="stocks-unblock",
        label="Unblock Debate",
        description="Borderline risk-cap rejections fire a 4-LLM committee that may override.",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM unblock_debate_runs
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="unblock_debate_runs",
        icon="🗳",
        kind="entry",

    ),
    Stage(
        id="stocks-broker",
        label="Broker Submit",
        description="Alpaca place_order_with_stop_loss + trade_journal write.",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM decisions
            WHERE timestamp_utc >= datetime('now', '-1 day')
              AND action='placed_order'
        """,
        count_label="orders placed today",
        icon="📨",
        kind="broker",
    ),
    Stage(
        id="stocks-steward",
        label="Order Steward",
        description="Verify-stops sweep every :20/:50 — attaches missing stop-losses.",
        role_name="order_steward",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='order_steward' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="sweeps today",
        icon="🛟",
        kind="steward",
    ),
    Stage(
        id="stocks-monitor",
        label="Position Monitor",
        description="Walks every held position — fires hold-debate triggers when thresholds cross.",
        role_name="portfolio_monitor",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='portfolio_monitor' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="checks today",
        icon="👁",
        kind="monitor",
    ),
    Stage(
        id="stocks-hold",
        label="Hold Debate",
        description="On position triggers: hold / tighten stop / exit now.",
        role_name="portfolio_monitor",
        persona_ids=_STOCKS_HOLD,
        count_query="""
            SELECT COUNT(*) FROM hold_debate_runs
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="hold_debate_runs",
        icon="🛡",
        kind="hold",
        stale_hours=90,  # market-hours only

    ),
    Stage(
        id="stocks-reconcile",
        label="Reconciliation",
        description="Diff broker positions against trade_journal. Mark closed trades.",
        role_name="reconciler",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='reconciler' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="reconciles today",
        icon="🧾",
        kind="reconcile",
    ),
    Stage(
        id="stocks-audit",
        label="Decision Audit",
        description="Every decision (placed / rejected / skipped) appended to decisions table.",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM decisions
            WHERE timestamp_utc >= datetime('now', '-1 day')
        """,
        count_label="decisions today",
        table_name="decisions",
        icon="📜",
        kind="audit",
    ),
    Stage(
        id="stocks-lesson",
        label="Lesson Loop",
        description="Nightly post-mortem. Per-source / per-strategy attribution.",
        role_name="debate_outcome_analyzer",
        persona_ids=_STOCKS_LESSON,
        count_query="""
            SELECT COUNT(*) FROM debate_lessons
            WHERE analysis_date >= datetime('now', '-7 day')
        """,
        count_label="lessons this week",
        table_name="debate_lessons",
        icon="📚",
        kind="lesson",
    ),
)


# ---------------------------------------------------------------------------
# Crypto pipeline
# ---------------------------------------------------------------------------

_CRYPTO_STAGES: Tuple[Stage, ...] = (
    Stage(
        id="crypto-sources",
        label="Intel Sources",
        description="Whale Alert · CryptoPanic · CoinDesk · CoinTelegraph · Etherscan · Funding · Skews · Snapshot",
        role_name="crypto_intel_ingestor",
        count_query="""
            SELECT COUNT(*) FROM intel_events_crypto
            WHERE ingested_at >= datetime('now', '-1 day')
        """,
        count_label="events today",
        table_name="intel_events_crypto",
        icon="📡",
        kind="automated",
        blocked_note=(
            "tier-1 sources (whale_alert / etherscan / cryptopanic) skip "
            "silently when their API keys are unset. Tier-2/3 keyless "
            "sources (coindesk_rss / cointelegraph_rss / apewisdom / "
            "binance_funding / defillama) work without keys."
        ),
    ),
    Stage(
        id="crypto-streams",
        label="Express Streams",
        description="Coinbase WS · Binance funding · Etherscan whales · DeFiLlama TVL",
        role_name="crypto_scanner",
        count_query="""
            SELECT COUNT(*) FROM intel_stream_events_crypto
            WHERE received_at >= datetime('now', '-1 day')
        """,
        count_label="stream events today",
        table_name="intel_stream_events_crypto",
        icon="⚡",
        kind="automated",
    ),
    Stage(
        id="crypto-aggregator",
        label="Aggregator",
        description="Per-symbol score + adversarial flags + chain context",
        role_name="crypto_intel_ingestor",
        count_query="SELECT COUNT(*) FROM intel_candidates_crypto",
        count_label="candidates pooled",
        table_name="intel_candidates_crypto",
        icon="⚖",
        kind="automated",
    ),
    Stage(
        id="crypto-scout",
        label="Scout Debate",
        description="Two-call: skeptic + analyst → judge. Elevate or dismiss. 2-hour re-debate cooldown per symbol prevents redundant LLM calls between scans.",
        role_name="crypto_intel_ingestor",
        persona_ids=_CRYPTO_SCOUT,
        count_query="""
            SELECT COUNT(*) FROM scout_debate_runs_crypto
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="scout_debate_runs_crypto",
        icon="🔭",
        kind="scout",
    ),
    Stage(
        id="crypto-entry",
        label="Entry Debate",
        description="Three-reviewer + judge: place / skip / defer with sized qty.",
        role_name="crypto_scanner",
        persona_ids=_CRYPTO_ENTRY,
        count_query="""
            SELECT COUNT(*) FROM entry_debate_runs_crypto
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="entry_debate_runs_crypto",
        icon="🎯",
        kind="entry",
    ),
    Stage(
        id="crypto-cb-gate",
        label="Circuit-Breaker Gate",
        description="BTC crash · funding extreme · stablecoin depeg · liquidation cascade. Halts new entries; hold-exits always allowed.",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM circuit_breaker_events_crypto
            WHERE tripped_at >= datetime('now', '-7 day')
        """,
        count_label="trips this week",
        icon="🚦",
        kind="risk",
    ),
    Stage(
        id="crypto-risk-gate",
        label="Risk Manager",
        description="Per-trade risk% · gross/net cap · daily-loss limit (shared with stocks).",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM decisions
            WHERE timestamp_utc >= datetime('now', '-1 day')
              AND action LIKE 'rejected_by_risk%'
              AND COALESCE(asset_class,'') = 'crypto'
        """,
        count_label="risk rejects today",
        icon="🛂",
        kind="risk",
    ),
    Stage(
        id="crypto-broker",
        label="Broker Submit",
        description="Alpaca crypto submit + stop-loss attach + trade_journal write.",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM decisions
            WHERE timestamp_utc >= datetime('now', '-1 day')
              AND action='placed_order'
              AND COALESCE(asset_class,'') = 'crypto'
        """,
        count_label="orders placed today",
        icon="📨",
        kind="broker",
    ),
    Stage(
        id="crypto-monitor",
        label="Position Monitor",
        description="Per-position triggers (big drop / sentiment flip / whale exit) → fire hold debate.",
        role_name="position_monitor",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='position_monitor' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="checks today",
        icon="👁",
        kind="monitor",
    ),
    Stage(
        id="crypto-hold",
        label="Hold Debate",
        description="On position triggers: hold / tighten stop / flatten.",
        role_name="position_monitor",  # fired by position_monitor, not crypto_scanner
        persona_ids=_CRYPTO_HOLD,
        count_query="""
            SELECT COUNT(*) FROM hold_debate_runs_crypto
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="hold_debate_runs_crypto",
        icon="🛡",
        kind="hold",
        stale_hours=90,  # reactive — fires only when a position triggers
    ),
    Stage(
        id="crypto-reconcile",
        label="Reconciliation",
        description="Shared with stocks reconciler — closed trades roll up to closed_trades.db.",
        role_name="reconciler",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='reconciler' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="reconciles today",
        icon="🧾",
        kind="reconcile",
    ),
    Stage(
        id="crypto-lesson",
        label="Lesson Loop",
        description="Per-chain / per-funding-band / per-source attribution.",
        role_name="debate_outcome_analyzer",
        persona_ids=_CRYPTO_LESSON,
        count_query="""
            SELECT COUNT(*) FROM debate_lessons_crypto
            WHERE analysis_date >= datetime('now', '-7 day')
        """,
        count_label="lessons this week",
        table_name="debate_lessons_crypto",
        icon="📚",
        kind="lesson",
    ),
)


# ---------------------------------------------------------------------------
# Options pipeline
# ---------------------------------------------------------------------------

_OPTIONS_STAGES: Tuple[Stage, ...] = (
    Stage(
        id="options-universe",
        label="Universe Build",
        description="Wheel-eligible builder (21:30 ET nightly): Alpaca optionable × Finnhub fundamentals.",
        role_name=None,  # wheel_universe_build uses _wrap(); count-based health
        count_query="""
            SELECT COUNT(*) FROM wheel_universe_cache WHERE eligible=1
        """,
        count_label="wheel-eligible names",
        icon="🌐",
        kind="universe",
    ),
    Stage(
        id="options-sources",
        label="Intel Sources",
        description="Earnings calendar · CBOE skew · IV capture · unusual flow",
        role_name="options_scanner",
        count_query="""
            SELECT COUNT(*) FROM intel_events_options
            WHERE ingested_at >= datetime('now', '-1 day')
        """,
        count_label="events today",
        table_name="intel_events_options",
        icon="📡",
        kind="automated",
    ),
    Stage(
        id="options-aggregator",
        label="Aggregator",
        description="Score + earnings-in-DTE flag + skew tag",
        role_name="options_scanner",
        count_query="SELECT COUNT(*) FROM intel_candidates_options",
        count_label="candidates pooled",
        table_name="intel_candidates_options",
        icon="⚖",
        kind="automated",
    ),
    Stage(
        id="options-scout",
        label="Scout Debate",
        description="Two-call: skeptic + analyst → judge. Elevate wheel candidates.",
        role_name="options_scanner",
        persona_ids=_OPTIONS_SCOUT,
        count_query="""
            SELECT COUNT(*) FROM scout_debate_runs_options
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="scout_debate_runs_options",
        icon="🔭",
        kind="scout",
    ),
    Stage(
        id="options-wheel",
        label="Wheel-Entry Debate",
        description="Aggressive + conservative + macro-overlay → judge. Place CSP/CC.",
        role_name="wheel_entry_debate",
        persona_ids=_OPTIONS_WHEEL,
        count_query="""
            SELECT COUNT(*) FROM wheel_debate_runs_options
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="wheel_debate_runs_options",
        icon="🎡",
        kind="wheel",
    ),
    Stage(
        id="options-cb-gate",
        label="Circuit-Breaker Gate",
        description="VIX spike · term inversion · earnings cluster · liquidity crisis. Halts new entries; existing-cycle management always allowed.",
        role_name=None,
        count_query="""
            SELECT COUNT(*) FROM circuit_breaker_events_options
            WHERE tripped_at >= datetime('now', '-7 day')
        """,
        count_label="trips this week",
        icon="🚦",
        kind="risk",
    ),
    Stage(
        id="options-broker",
        label="Broker Submit",
        description="Alpaca options submit_short_put / short_call → wheel-cycle anchor.",
        role_name=None,
        count_query="""
            SELECT
              (SELECT COUNT(*) FROM wheel_debate_runs_options
               WHERE run_at >= datetime('now', '-1 day')
                 AND verdict='place' AND entry_order_id IS NOT NULL)
              +
              (SELECT COUNT(*) FROM option_fills
               WHERE ts >= datetime('now', '-1 day'))
        """,
        count_label="orders placed today",
        icon="📨",
        kind="broker",
    ),
    Stage(
        id="options-state",
        label="Wheel State Machine",
        description="cash → CSP → assigned → CC → called away → cash",
        role_name="wheel_manage",
        count_query="""
            SELECT
              (SELECT COUNT(*) FROM wheel_cycles_options WHERE ended_at IS NULL)
              +
              (SELECT COUNT(*) FROM wheel_cycles WHERE closed_at IS NULL)
        """,
        count_label="open cycles",
        table_name=None,
        icon="⚙",
        kind="state",
    ),
    Stage(
        id="options-monitor",
        label="Cycle Manager",
        description="Reads open cycles every 30 min — rolls / closes for profit / accepts assignment.",
        role_name="wheel_manage",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='wheel_manage' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="manage ticks today",
        icon="👁",
        kind="monitor",
    ),
    Stage(
        id="options-hold",
        label="Hold Debate",
        description="On cycle triggers (delta breach / profit target / expiry approach): roll / close / accept assignment.",
        role_name="wheel_manage",
        persona_ids=_OPTIONS_HOLD,
        count_query="""
            SELECT COUNT(*) FROM hold_debate_runs_options
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="hold_debate_runs_options",
        icon="🛡",
        kind="hold",
    ),
    Stage(
        id="options-reconcile",
        label="Reconciliation",
        description="Reconcile broker option positions against contract_positions_options + wheel cycle state.",
        role_name="reconciler",
        count_query="""
            SELECT COUNT(*) FROM role_runs
            WHERE role_name='reconciler' AND status='ok'
              AND started_at >= datetime('now', '-1 day')
        """,
        count_label="reconciles today",
        icon="🧾",
        kind="reconcile",
    ),
    Stage(
        id="options-lesson",
        label="Lesson Loop",
        description="Per-IV-rank / per-DTE / per-structure attribution.",
        role_name="debate_outcome_analyzer",
        persona_ids=_OPTIONS_LESSON,
        count_query="""
            SELECT COUNT(*) FROM debate_lessons_options
            WHERE analysis_date >= datetime('now', '-7 day')
        """,
        count_label="lessons this week",
        table_name="debate_lessons_options",
        icon="📚",
        kind="lesson",
    ),
)


# ---------------------------------------------------------------------------
# Public registry
# ---------------------------------------------------------------------------


PIPELINES: Tuple[Pipeline, ...] = (
    Pipeline(
        id="stocks", label="Stocks", icon="📈",
        color="cyan",
        description="US equities momentum + reversion lanes via Alpaca paper.",
        stages=_STOCKS_STAGES,
    ),
    Pipeline(
        id="crypto", label="Crypto", icon="₿",
        color="amber",
        description="24/7 crypto scout/entry/hold via Coinbase + Binance + on-chain.",
        stages=_CRYPTO_STAGES,
    ),
    Pipeline(
        id="options", label="Options Wheel", icon="⚙",
        color="fuchsia",
        description="CSP → assigned → CC → called away cycle on optionable equities.",
        stages=_OPTIONS_STAGES,
    ),
)
