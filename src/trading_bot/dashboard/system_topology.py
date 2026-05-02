"""Hand-coded topology for the system-pipeline view.

Why hand-coded: the set of automations changes rarely (every few weeks
when a new role lands), and an explicit list keeps node IDs, model
badges, and arrow coordinates trivially diff-reviewable. An auto-layout
library would be a footgun for a 35-node graph where most arrows are
hand-tuned to read well.

Structure:
* ``ZONES`` — ordered, with a human label.
* ``NODES`` — list of typed dicts with everything the renderer needs.
* ``EDGES`` — directional. Renderer draws arrows between node IDs.

Adding a new automation:
  1. Append a node dict.
  2. (Optional) wire edges from upstream sources or to downstream sinks.
  3. (Optional) add it to ``DRILLDOWN_HANDLERS`` in app.py if it should
     surface a custom panel beyond the default "last 10 events".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Zone = Literal[
    "intake", "discovery", "decision", "execution",
    "reconciliation", "learning", "llm_routing", "supervision",
]
Health = Literal["ok", "warn", "fail", "off"]


@dataclass(frozen=True)
class Node:
    """One pipeline box.

    * ``id`` — short, kebab-cased; used as DOM id and edge endpoint.
    * ``zone`` — controls which column/row the renderer places it in.
    * ``label`` — what the operator sees.
    * ``model_badge`` — set for LLM-using boxes; rendered as ``[Opus 4.7]``.
    * ``mailbox`` — True if calls route through the file-backed mailbox.
    * ``subscribes`` — bus event types whose arrival should flash this box.
    * ``role_name`` — when set, health is computed from ``role_runs`` rows.
    * ``cadence_label`` — short hint shown under the title (e.g. ``every 30m``).
    * ``passive`` — pure data sources (Alpaca News, FRED, …); rendered gray and uninteractive.
    """

    id: str
    zone: Zone
    label: str
    model_badge: str | None = None
    mailbox: bool = False
    subscribes: tuple[str, ...] = ()
    role_name: str | None = None
    cadence_label: str = ""
    passive: bool = False
    process: str = ""  # daemon | lab | supervisor | mailbox | dashboard


# Zone display order, top-to-bottom in the UI.
ZONES: tuple[tuple[Zone, str], ...] = (
    ("intake",         "Intake"),
    ("discovery",      "Discovery"),
    ("decision",       "Decision"),
    ("execution",      "Execution"),
    ("reconciliation", "Reconciliation"),
    ("learning",       "Learning"),
    ("llm_routing",    "LLM Routing"),
    ("supervision",    "Supervision"),
)


NODES: tuple[Node, ...] = (
    # ---------- INTAKE (passive data sources) -----------------------------
    Node("alpaca_trade_stream", "intake", "Alpaca Trade Stream",
         subscribes=("order.placed", "order.filled", "order.canceled",
                     "order.rejected", "order.partial_fill"),
         cadence_label="websocket", process="daemon"),
    Node("alpaca_account",     "intake", "Alpaca Account / REST", passive=True,
         cadence_label="REST"),
    Node("alpaca_news",        "intake", "Alpaca News",      passive=True),
    Node("apewisdom",          "intake", "ApeWisdom (WSB)",  passive=True),
    Node("vip_tweets",         "intake", "VIP Tweets",       passive=True),
    Node("sec_form4",          "intake", "SEC Form 4",       passive=True),
    Node("finnhub",            "intake", "Finnhub",          passive=True),
    Node("gdelt",              "intake", "GDELT",            passive=True),
    Node("fred",               "intake", "FRED Macro",       passive=True),
    Node("websearch",          "intake", "WebSearch",        passive=True),
    Node("market_data_stream", "intake", "Market Data Stream",
         cadence_label="OFF (Phase 8)", passive=True),

    # ---------- DISCOVERY -------------------------------------------------
    Node("universe_curator",   "discovery", "Universe Curator",
         role_name="universe_curator",
         cadence_label="06:30 / 07:30 / 12:00 ET", process="daemon"),
    Node("intel_ingestor",     "discovery", "Intel Ingestor",
         role_name="intel_ingestor", subscribes=("intel.updated", "role.completed"),
         cadence_label="every 30 min", process="lab"),
    Node("sentiment_analyst",  "discovery", "Sentiment Analyst",
         role_name="sentiment_analyst",
         cadence_label="07:00 + 12:00 ET", process="daemon"),
    Node("stock_scanner",      "discovery", "Stock Scanner",
         role_name="stock_scanner", subscribes=("scan.completed",),
         cadence_label="every 60 min RTH", process="daemon"),
    Node("crypto_scanner",     "discovery", "Crypto Scanner",
         role_name="crypto_scanner", subscribes=("scan.completed",),
         cadence_label="interval, 24/7", process="daemon"),
    Node("vip_scanner",        "discovery", "VIP Scanner",
         role_name="vip_listener",
         cadence_label="every Nmin RTH", process="daemon"),
    Node("wheel_scout",        "discovery", "Wheel Scout",
         subscribes=("scout.completed",),
         cadence_label="02:00 UTC nightly", process="daemon"),
    Node("wheel_universe_builder", "discovery", "Wheel Universe Builder",
         role_name="wheel_universe_build",
         cadence_label="21:30 ET nightly", process="daemon"),
    Node("iv_capture",         "discovery", "IV Capture",
         role_name="iv_capture",
         cadence_label="09:45 ET", process="daemon"),

    # ---------- DECISION --------------------------------------------------
    Node("orchestrator",   "decision", "Orchestrator",
         subscribes=("decision.created",),
         cadence_label="per scan", process="daemon"),
    Node("risk_gate",      "decision", "Risk Gate",
         subscribes=("decision.created",),
         cadence_label="deterministic", process="daemon"),
    Node("risk_debate",    "decision", "Risk Debate",
         model_badge="Opus 4.7",
         subscribes=("debate.risk.completed",),
         cadence_label="per high-risk decision", process="daemon"),
    Node("strategy_architect", "decision", "Strategy Architect",
         model_badge="Opus 4.7",
         cadence_label="weekly", process="lab"),
    Node("unblock_debate", "decision", "Unblock Debate",
         model_badge="Opus 4.7", mailbox=True,
         subscribes=("debate.unblock.completed",),
         cadence_label="on risk-gate rejection", process="daemon"),
    Node("decisions_store", "decision", "Decisions Store",
         subscribes=("decision.created",), cadence_label="append-only"),
    Node("wheel_runner",   "decision", "Wheel Runner",
         role_name="wheel_scan", subscribes=("wheel.cycle.changed",),
         cadence_label="10:15 + every 30/60min RTH", process="daemon"),

    # ---------- EXECUTION --------------------------------------------------
    Node("order_submitter",  "execution", "Order Submitter",
         subscribes=("order.submitted",), process="daemon"),
    Node("position_tracker", "execution", "Position Tracker",
         subscribes=("position.changed",), process="daemon"),
    Node("order_steward",    "execution", "Order Steward",
         role_name="verify_stops", cadence_label=":20 / :50 hourly",
         process="daemon"),
    Node("portfolio_monitor", "execution", "Portfolio Monitor",
         role_name="portfolio_watch", cadence_label="every Nmin RTH",
         process="daemon"),

    # ---------- RECONCILIATION --------------------------------------------
    Node("reconciler",       "reconciliation", "Reconciler",
         role_name="reconciler",
         cadence_label="16:05 + 21:55 ET", process="daemon"),
    Node("closed_trades_store", "reconciliation", "Closed Trades Store",
         subscribes=("trade.closed",), cadence_label="append-only"),
    Node("trade_journal",    "reconciliation", "Trade Journal",
         cadence_label="append-only"),

    # ---------- LEARNING --------------------------------------------------
    Node("decision_reflector", "learning", "Decision Reflector",
         model_badge="Opus 4.7", mailbox=True,
         subscribes=("lesson.created",),
         role_name="decision_reflector",
         cadence_label="nightly post-reconciler", process="lab"),
    Node("lessons_store",   "learning", "Lessons Store",
         subscribes=("lesson.created",), cadence_label="append-only"),
    Node("promotion_debate", "learning", "Promotion Debate",
         model_badge="Opus 4.7",
         subscribes=("debate.promotion.completed",),
         cadence_label="per promotion attempt", process="lab"),
    Node("lab_evolution",   "learning", "Lab Evolution",
         role_name="lab_evolution", cadence_label="nightly", process="lab"),
    Node("calibrator",      "learning", "Calibrator",
         role_name="calibrator", cadence_label="nightly", process="lab"),
    Node("threshold_tuner", "learning", "Threshold Tuner",
         role_name="threshold_tuner",
         subscribes=("threshold.updated",),
         cadence_label="nightly post-reconciler", process="lab"),
    Node("strategy_coach",  "learning", "Strategy Coach",
         role_name="strategy_coach",
         cadence_label="06:00 ET weekdays", process="daemon"),
    Node("hold_spy_coordinator", "learning", "Hold-SPY Coordinator",
         role_name="hold_spy_coordinator",
         cadence_label="15:55 ET weekdays", process="daemon"),

    # ---------- LLM ROUTING (bridge) --------------------------------------
    Node("mailbox_routine", "llm_routing", "LLM Mailbox Routine",
         subscribes=("mailbox.brief.completed", "mailbox.brief.failed",
                     "mailbox.brief.submitted"),
         cadence_label="every 30 min · via Claude Code", process="mailbox"),

    # ---------- SUPERVISION (collapsed by default) ------------------------
    Node("scheduler",       "supervision", "Scheduler",
         cadence_label="APScheduler", process="daemon"),
    Node("heartbeat",       "supervision", "Heartbeat",
         role_name="heartbeat", subscribes=("heartbeat.tick",),
         cadence_label="every 30s", process="daemon"),
    Node("stall_watchdog",  "supervision", "Stall Watchdog",
         subscribes=("role.stalled",),
         cadence_label="continuous", process="supervisor"),
    Node("email_digest",    "supervision", "Email Digest",
         role_name="daily_digest", cadence_label="16:30 ET", process="daemon"),
    Node("nightly_review",  "supervision", "Reporter / Nightly Review",
         role_name="nightly_review", cadence_label="17:00 daily", process="daemon"),
    Node("cost_tracker",    "supervision", "Cost Tracker / LLM Spend",
         cadence_label="per call"),
    Node("freshness_audit", "supervision", "Freshness Audit",
         cadence_label="per snapshot"),
    Node("regime_detector", "supervision", "Regime Detector",
         cadence_label="per snapshot"),
    Node("process_registry", "supervision", "Process Registry",
         cadence_label="ps poll"),
    Node("schedule_audit",  "supervision", "Schedule Audit",
         role_name="schedule_audit",
         cadence_label="21:55 ET", process="daemon"),
    Node("alert_drain",     "supervision", "Alert Drain",
         role_name="alert_drain",
         cadence_label="every 1 min", process="daemon"),
    Node("log_rotation",    "supervision", "Log Rotation",
         role_name="log_rotation",
         cadence_label="Sundays 03:00", process="daemon"),
    Node("event_bus_retention", "supervision", "Event Bus Retention",
         role_name="event_bus_retention",
         cadence_label="03:15 ET nightly", process="daemon"),
)


# Edges read top-to-bottom following data flow. The renderer draws an
# SVG arrow between the two named DOM nodes; CSS handles the shimmer
# animation when an event whose ``subscribes`` matches the destination
# node fires.
EDGES: tuple[tuple[str, str], ...] = (
    # Intake → Discovery
    ("alpaca_news",     "intel_ingestor"),
    ("apewisdom",       "intel_ingestor"),
    ("vip_tweets",      "intel_ingestor"),
    ("sec_form4",       "intel_ingestor"),
    ("finnhub",         "intel_ingestor"),
    ("gdelt",           "intel_ingestor"),
    ("websearch",       "wheel_scout"),
    ("alpaca_account",  "universe_curator"),
    ("fred",            "regime_detector"),
    ("intel_ingestor",  "stock_scanner"),
    ("intel_ingestor",  "crypto_scanner"),
    ("universe_curator", "stock_scanner"),
    ("wheel_scout",     "wheel_universe_builder"),
    ("wheel_universe_builder", "wheel_runner"),

    # Discovery → Decision
    ("stock_scanner",   "orchestrator"),
    ("crypto_scanner",  "orchestrator"),
    ("vip_scanner",     "orchestrator"),
    ("sentiment_analyst", "orchestrator"),
    ("orchestrator",    "risk_gate"),
    ("risk_gate",       "risk_debate"),
    ("risk_gate",       "unblock_debate"),
    ("risk_debate",     "decisions_store"),
    ("unblock_debate",  "decisions_store"),
    ("decisions_store", "order_submitter"),
    ("wheel_runner",    "order_submitter"),

    # Decision/Execution
    ("order_submitter",   "alpaca_trade_stream"),
    ("alpaca_trade_stream", "position_tracker"),
    ("position_tracker",  "reconciler"),
    ("order_steward",     "alpaca_trade_stream"),
    ("portfolio_monitor", "alpaca_trade_stream"),

    # Reconciliation → Learning
    ("reconciler",        "closed_trades_store"),
    ("reconciler",        "trade_journal"),
    ("closed_trades_store", "decision_reflector"),
    ("decision_reflector",  "lessons_store"),
    ("closed_trades_store", "calibrator"),
    ("closed_trades_store", "threshold_tuner"),
    ("lab_evolution",       "promotion_debate"),
    ("strategy_coach",      "decisions_store"),
    ("hold_spy_coordinator", "order_submitter"),

    # LLM routing bridge — every mailbox-routed node points at the routine.
    ("unblock_debate",      "mailbox_routine"),
    ("decision_reflector",  "mailbox_routine"),

    # Supervision wiring (mostly cross-cutting; we draw a few key ones).
    ("scheduler",      "stock_scanner"),
    ("scheduler",      "wheel_runner"),
    ("scheduler",      "reconciler"),
    ("heartbeat",      "stall_watchdog"),
)


def node_by_id(nid: str) -> Node | None:
    for n in NODES:
        if n.id == nid:
            return n
    return None


def nodes_in_zone(zone: Zone) -> list[Node]:
    return [n for n in NODES if n.zone == zone]
