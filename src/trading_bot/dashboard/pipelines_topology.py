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
    kind: str = "automated"                       # automated | scout | entry | hold | wheel | lesson | state
    # Operator-visible note about a known gap on this stage. Renders
    # under the description in italic amber when set. Examples:
    # "needs ANTHROPIC_API_KEY", "wired but not yet scheduled", etc.
    blocked_note: str = ""


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
_OPTIONS_LESSON = ("options_lesson_analyst_v1",)


# ---------------------------------------------------------------------------
# Stocks pipeline
# ---------------------------------------------------------------------------

_STOCKS_STAGES: Tuple[Stage, ...] = (
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
        # Legacy stocks debate uses MailboxBackedClient → AnthropicAPI.
        # Crypto/options use the Claude CLI subprocess transport (Max 5x).
        # Until migrated, stocks scout debate is skipped silently every
        # tick with "no anthropic creds".
        blocked_note=(
            "needs migration to shared.llm_transport (currently uses "
            "MailboxBackedClient → ANTHROPIC_API_KEY which isn't set)"
        ),
    ),
    Stage(
        id="stocks-entry",
        label="Entry Debate",
        description="Pre-trade committee. Place / skip / defer with sized qty.",
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
        blocked_note="same legacy LLM transport gap as scout debate above",
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
        blocked_note="same legacy LLM transport gap as scout debate above",
    ),
    Stage(
        id="stocks-lesson",
        label="Lesson Loop",
        description="Nightly post-mortem. Per-source / per-strategy attribution.",
        role_name="decision_reflector",
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
        role_name="crypto_scanner",
        count_query="""
            SELECT COUNT(*) FROM intel_events_crypto
            WHERE ingested_at >= datetime('now', '-1 day')
        """,
        count_label="events today",
        table_name="intel_events_crypto",
        icon="📡",
        kind="automated",
        blocked_note=(
            "tier-1 sources (whale_alert / etherscan / cryptopanic) "
            "skip silently when their API keys are unset"
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
        role_name="crypto_scanner",
        count_query="SELECT COUNT(*) FROM intel_candidates_crypto",
        count_label="candidates pooled",
        table_name="intel_candidates_crypto",
        icon="⚖",
        kind="automated",
    ),
    Stage(
        id="crypto-scout",
        label="Scout Debate",
        description="Two-call: skeptic + analyst → judge. Elevate or dismiss.",
        role_name="crypto_scanner",
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
        id="crypto-hold",
        label="Hold Debate",
        description="On position triggers: hold / tighten stop / flatten.",
        role_name="crypto_scanner",
        persona_ids=_CRYPTO_HOLD,
        count_query="""
            SELECT COUNT(*) FROM hold_debate_runs_crypto
            WHERE run_at >= datetime('now', '-1 day')
        """,
        count_label="debates today",
        table_name="hold_debate_runs_crypto",
        icon="🛡",
        kind="hold",
    ),
    Stage(
        id="crypto-lesson",
        label="Lesson Loop",
        description="Per-chain / per-funding-band / per-source attribution.",
        role_name="decision_reflector",
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
        blocked_note="dry-run mode (executor=None) until operator validates audit chain",
    ),
    Stage(
        id="options-state",
        label="Wheel State Machine",
        description="cash → CSP → assigned → CC → called away → cash",
        role_name="wheel_manage",
        count_query="""
            SELECT COUNT(*) FROM wheel_cycles_options
            WHERE ended_at IS NULL
        """,
        count_label="open cycles",
        table_name="wheel_cycles_options",
        icon="⚙",
        kind="state",
    ),
    Stage(
        id="options-lesson",
        label="Lesson Loop",
        description="Per-IV-rank / per-DTE / per-structure attribution.",
        role_name="decision_reflector",
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
