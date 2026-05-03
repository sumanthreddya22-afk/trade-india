"""Role → persona mapping for the system dashboard.

Every background job ("role") in the daemon's APScheduler has one or
more named **operators** — the persona(s) responsible for the LLM
judgment calls that fire inside that job. This module is the single
source of truth for that mapping; the dashboard's Background Jobs card
reads it to render human names alongside each cron-job row.

Why a static map (rather than scanning prompt_versions): some roles
don't fire LLM calls at all (data fetchers, reconcilers, file watchers)
yet still deserve a labelled "owner". This map covers both cases.

The map keys are role_name strings as they appear in
``role_runs.role_name`` (i.e. the job names registered in
``shared/daemon._load_runners``). Values are lists of persona ``id``
strings. The display helper below resolves each id to the
``{full_name, role_title, pipeline}`` payload for the template.

Conventions
-----------
- Roles that run no LLM (file watchers, retention, log rotation) get
  an empty list and render as "automated · no LLM" in the UI.
- Multi-persona roles list operators in ``[reviewer, reviewer, judge]``
  order so the badge sequence is visually consistent.
- One persona may staff multiple roles (e.g. Diane Pereira chairs all
  three crypto debates) — that's expected and intentional.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# role_name → list of persona id strings
# ---------------------------------------------------------------------------


_STOCKS_SCOUT = [
    "stocks_scout_skeptic_v1",
    "stocks_scout_analyst_v1",
    "stocks_scout_judge_v1",
]
_STOCKS_HOLD = [
    "stocks_hold_aggressive_v1",
    "stocks_hold_conservative_v1",
    "stocks_hold_neutral_v1",
    "stocks_hold_judge_v1",
]
_CRYPTO_SCOUT = [
    "crypto_scout_skeptic_v1",
    "crypto_scout_analyst_v1",
    "crypto_scout_judge_v1",
]
_CRYPTO_HOLD = [
    "crypto_hold_aggressive_v1",
    "crypto_hold_conservative_v1",
    "crypto_hold_neutral_v1",
    "crypto_hold_judge_v1",
]
_CRYPTO_ENTRY = [
    "crypto_entry_aggressive_v1",
    "crypto_entry_conservative_v1",
    "crypto_entry_neutral_v1",
    "crypto_entry_judge_v1",
]
_OPTIONS_WHEEL = [
    "options_wheel_aggressive_v1",
    "options_wheel_conservative_v1",
    "options_wheel_neutral_v1",
    "options_wheel_judge_v1",
]
_OPTIONS_SCOUT = [
    "options_scout_skeptic_v1",
    "options_scout_analyst_v1",
    "options_scout_judge_v1",
]
_LESSON_ANALYSTS = [
    "stocks_lesson_analyst_v1",
    "crypto_lesson_analyst_v1",
    "options_lesson_analyst_v1",
]


# The map covers BOTH names that show up in role_runs:
#   - APScheduler cron-job keys (intel_scan, crypto_scan, …)
#   - Role-class instance names that get persisted into role_runs.role_name
#     by ``Role.safe_run`` (stock_scanner, crypto_scanner, intel_ingestor, …).
# Both are listed so the dashboard can label whichever name surfaces.
ROLE_OPERATORS: Dict[str, List[str]] = {
    # ── Stocks pipeline ──
    "intel_scan": _STOCKS_SCOUT,
    "stock_scanner": _STOCKS_SCOUT,
    "intel_ingestor": _STOCKS_SCOUT,
    "portfolio_watch": _STOCKS_HOLD,
    "portfolio_monitor": _STOCKS_HOLD,
    "premarket_rank": [],
    "midday_rerank": [],
    "massive_refresh": [],
    "vip_scan": [],
    "vip_listener": [],

    # ── Crypto pipeline ──
    "crypto_scan": _CRYPTO_SCOUT,
    "crypto_scanner": _CRYPTO_SCOUT,
    # The crypto pipeline runs hold + entry debates from the same
    # scanner role; we list scout here because that's the dominant
    # workload and the table renders one set of operators per row.

    # ── Options pipeline ──
    "wheel_scan": _OPTIONS_WHEEL,
    "wheel_manage": _OPTIONS_WHEEL,
    "wheel_entry_debate": _OPTIONS_WHEEL,
    "iv_capture": [],
    "wheel_universe_build": [],
    "options_scout": _OPTIONS_SCOUT,
    "options_scanner": _OPTIONS_SCOUT,

    # ── Cross-pipeline coordinators / non-LLM ──
    "verify_stops": [],
    "order_steward": [],
    "news_warm": [],
    "sentiment_analyst": [],
    "midday_snapshot": [],
    "daily_digest": [],
    "reporter": [],
    "log_rotation": [],
    "event_bus_retention": [],
    "reconciler": [],
    "schedule_audit": [],
    "alert_drain": [],
    "heartbeat": [],
    "health_pulse": [],
    "watchdog": [],
    "account_sentinel": [],
    "calibrator": [],
    "promoter": [],
    "param_optimizer": [],
    "backtest_engineer": [],
    "universe_curator": [],
    "hold_spy_coordinator": [],

    # Strategy / threshold tuners are LLM-driven but use single-prompt
    # legacy code (no PERSONA dict). Leave operators empty for now;
    # surface them as "automated · no LLM" until they get persona files.
    "strategy_coach": [],
    "strategy_architect": [],
    "threshold_tuner": [],
    "decision_reflector": _LESSON_ANALYSTS,

    # ── Lesson loops (LLM-driven, slower cadence) ──
    "nightly_review": _LESSON_ANALYSTS,
}


# ---------------------------------------------------------------------------
# Persona registry — built lazily on first access
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorInfo:
    persona_id: str
    full_name: str
    role_title: str
    pipeline: str
    debate_role: str


_PERSONA_REGISTRY: Optional[Dict[str, OperatorInfo]] = None


def _build_registry() -> Dict[str, OperatorInfo]:
    """Walk every persona module and index by persona id.

    Done once per process and cached. A persona file that fails to
    import is logged and skipped — the dashboard must keep rendering
    even when one persona module has a syntax error.
    """
    registry: Dict[str, OperatorInfo] = {}

    persona_packages = (
        "trading_bot.personas",                      # legacy stocks (still source of truth)
        "trading_bot.shared.personas",
        "trading_bot.pipelines.crypto.personas",
        "trading_bot.pipelines.options.personas",
    )

    import pkgutil

    for pkg_name in persona_packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except Exception as e:
            logger.warning("role_persona_map: %s import failed: %s", pkg_name, e)
            continue
        for module_info in pkgutil.iter_modules(
            getattr(pkg, "__path__", []), prefix=f"{pkg_name}.",
        ):
            short = module_info.name.rsplit(".", 1)[-1]
            if short.startswith("_"):
                continue
            try:
                mod = importlib.import_module(module_info.name)
            except Exception as e:
                logger.debug("role_persona_map: %s skipped (%s)", module_info.name, e)
                continue
            persona = getattr(mod, "PERSONA", None)
            if not isinstance(persona, dict):
                continue
            persona_id = persona.get("id")
            if not persona_id:
                continue
            registry[str(persona_id)] = OperatorInfo(
                persona_id=str(persona_id),
                full_name=str(persona.get("full_name", "")),
                role_title=str(persona.get("role_title", "")),
                pipeline=str(persona.get("pipeline", "")),
                debate_role=str(persona.get("debate_role", "")),
            )
    return registry


def _registry() -> Dict[str, OperatorInfo]:
    global _PERSONA_REGISTRY
    if _PERSONA_REGISTRY is None:
        _PERSONA_REGISTRY = _build_registry()
    return _PERSONA_REGISTRY


# ---------------------------------------------------------------------------
# Public lookup
# ---------------------------------------------------------------------------


def operators_for_role(role_name: str) -> List[OperatorInfo]:
    """Return the named operators for a background-job role.

    Returns an empty list when the role isn't mapped (or has no LLM
    operators — data jobs etc.). Caller renders that as "automated"
    in the UI.
    """
    ids = ROLE_OPERATORS.get(role_name) or []
    if not ids:
        return []
    reg = _registry()
    out: List[OperatorInfo] = []
    for pid in ids:
        info = reg.get(pid)
        if info is not None:
            out.append(info)
    return out


def operators_payload(role_name: str) -> List[Dict[str, str]]:
    """Template-friendly form of ``operators_for_role``.

    Each item is ``{"name": "Diane Pereira", "title": "...", "pipeline": "crypto",
    "debate_role": "scout_judge"}`` — a flat dict the Jinja template can
    iterate without further unpacking.
    """
    return [
        {
            "persona_id": op.persona_id,
            "name": op.full_name,
            "title": op.role_title,
            "pipeline": op.pipeline,
            "debate_role": op.debate_role,
        }
        for op in operators_for_role(role_name)
    ]
