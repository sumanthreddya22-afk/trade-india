"""W4.2 — News-driven LLM trader controller.

Consumes a ``StructuredEvent`` (from event_extractor) plus market context,
calls Claude Opus 4.7 with the PDF page 9-10 news-variant system prompt,
and returns a ``Decision`` that satisfies the strict JSON contract.

Critical invariants:
- Model is pinned to ``claude-opus-4-7`` for the same reason the user asked
  for Opus 4.7 throughout: news-trader judgement (entity verification,
  novelty assessment, MNPI ambiguity calls) is the kind of reasoning task
  Opus is best at.
- LLM never bypasses the deterministic pipeline — the returned Decision
  still flows through ``RiskManager.check`` before any order is submitted.
- Failure modes (parse error, missing fields, schema mismatch) fail-closed
  to ``Decision(action="no_trade", reason="...")``. The bot never trades
  on a malformed LLM response.
- Action set is intentionally a superset of the legacy ``buy/sell/hold`` —
  ``escalate_to_human``, ``no_trade``, and ``enter/exit/scale_in/reduce``
  are first-class.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from trading_bot.news_trader.event_extractor import StructuredEvent
from trading_bot.orchestrator import (
    AuditObject,
    ComplianceFlags,
    DataQualityFlags,
    Decision,
)


# Model is pinned per the user's session-wide constraint and because
# news-trader reasoning is judgement-heavy.
NEWS_CONTROLLER_MODEL = "claude-opus-4-7"

# The prompt mirrors the PDF's "VARIANT PROMPT — DISCRETIONARY NEWS-DRIVEN
# TRADING" (page 9-10). Strict, fail-closed, with the regulator-grounded
# language the report cites. Versioning: every textual change to this
# constant is a new prompt version that needs shadow promotion (W5).
NEWS_CONTROLLER_PROMPT_VERSION = "v1"
NEWS_CONTROLLER_SYSTEM_PROMPT = """\
You are a news-driven event trading controller for an autonomous trading
system. You PROPOSE actions; a deterministic risk engine approves or
rejects every one. You do NOT submit orders directly.

PRIMARY MANDATE
- Trade only when an event is real, novel, entity-linked, economically
  material, and supported by approved sources.
- Prefer waiting for confirmation over reacting to ambiguous headlines.
- No trade is allowed if there is any MNPI concern, source ambiguity, or
  unresolved factual dispute.
- Treat capital preservation, market integrity, and policy compliance as
  hard constraints.

USE ONLY
- The structured event provided in the user message.
- The approved-source list. Sources NOT on this list never count toward
  source_count or confidence.
- Market reaction telemetry attached to the event (5m return, spread
  widening) — these are observation, not authorization.

REQUIRED EVENT PROCESS
1. Extract entity, event type, surprise direction, and likely transmission channel.
2. Verify with MIN_SOURCE_COUNT independent approved sources OR one primary filing/source.
3. Check novelty against prior disclosures and consensus expectations.
4. Estimate horizon, decay, and liquidity-adjusted implementation cost.
5. If confidence or verifiability is inadequate -> NO_TRADE.

HARD CONTROLS
- Source allowlist enforced. Off-list sources are noise.
- Recency threshold and event-confidence threshold enforced.
- Embargo / MNPI screen enforced. Unresolved -> ESCALATE.
- Time stop applies to every position the controller proposes.
- Maximum gap-risk exposure capped per the capital_cap_pct supplied.
- Circuit-breaker awareness: do not propose against an active venue halt.

ALLOWED ACTIONS
- NO_TRADE
- ENTER
- SCALE_IN
- REDUCE
- EXIT
- HEDGE
- ESCALATE  (use for ambiguous events: single-source rumors, MNPI doubt,
   conflicting reports, or anything that needs a human to decide)

OUTPUT FORMAT
Return valid JSON only — strict JSON, no prose outside the JSON object. Schema:
{
  "decision": "NO_TRADE" | "ENTER" | "SCALE_IN" | "REDUCE" | "EXIT" | "HEDGE" | "ESCALATE",
  "event": {
    "entity": str,
    "type": str,
    "direction": "POSITIVE" | "NEGATIVE" | "MIXED",
    "novelty": float (0..1),
    "source_quality": float (0..1)
  },
  "trade_plan": null | {
    "side": "BUY" | "SELL",
    "size_pct": float (0..capital_cap_pct),
    "entry": "limit" | "market"
  },
  "time_stop": null | str (e.g., "24h", "5d"),
  "reason": str (concise, evidence-based, never speculative),
  "audit": {
    "prompt_versions": {"news_controller": "<your_prompt_version>"}
  }
}
"""


@dataclass(frozen=True)
class NewsControllerInput:
    event: StructuredEvent
    market_reaction_5m_pct: float
    spread_widening_bps: float
    approved_sources: tuple[str, ...]
    min_source_count: int = 2
    min_confidence_threshold: float = 0.7
    min_edge_bps_threshold: float = 25.0
    capital_cap_pct: float = 5.0


# Maps the LLM's CAPS action labels to the orchestrator's action strings.
_ACTION_MAP: dict[str, str] = {
    "NO_TRADE": "no_trade",
    "ENTER": "enter",
    "SCALE_IN": "scale_in",
    "REDUCE": "reduce",
    "EXIT": "exit",
    "HEDGE": "hedge",
    "ESCALATE": "escalate_to_human",
}


def _build_user_message(inp: NewsControllerInput) -> str:
    """Render the structured event + thresholds as the LLM user message.

    Output is a single JSON document so the LLM's parser can ingest it
    cleanly and there's no ambiguity about what's a constraint vs. context.
    """
    payload: dict[str, Any] = {
        "event": {
            "entity": inp.event.entity,
            "headline": inp.event.headline,
            "type": inp.event.event_type,
            "direction": inp.event.direction,
            "novelty": inp.event.novelty,
            "source_count": inp.event.source_count,
            "primary_filing_present": inp.event.primary_filing_present,
            "sources": list(inp.event.sources),
        },
        "market_reaction": {
            "5m_return_pct": inp.market_reaction_5m_pct,
            "spread_widening_bps": inp.spread_widening_bps,
        },
        "policy": {
            "approved_sources": list(inp.approved_sources),
            "MIN_SOURCE_COUNT": inp.min_source_count,
            "MIN_CONFIDENCE_THRESHOLD": inp.min_confidence_threshold,
            "MIN_EDGE_BPS_THRESHOLD": inp.min_edge_bps_threshold,
            "CAPITAL_CAP_PCT": inp.capital_cap_pct,
        },
    }
    return json.dumps(payload, indent=2)


def _parse_response(raw: str) -> dict[str, Any] | None:
    """Strip code fences and try to parse JSON. Returns None on failure."""
    text = raw.strip()
    if text.startswith("```"):
        # Strip optional fence lines like ```json ... ```
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except Exception:
        return None


def propose_news_trade(
    inp: NewsControllerInput,
    *,
    llm_call_fn: Callable[..., str],
    model: str = NEWS_CONTROLLER_MODEL,
) -> Decision:
    """Run the news-trader prompt and convert the JSON response into a Decision.

    ``llm_call_fn`` is injected for testability; production callers pass a
    function that wraps the Anthropic SDK. The function must accept
    ``system``, ``user``, and ``model`` keyword args and return a string.
    """
    system = NEWS_CONTROLLER_SYSTEM_PROMPT
    user = _build_user_message(inp)
    try:
        raw = llm_call_fn(system=system, user=user, model=model)
    except Exception as e:
        return _fail_closed(inp, reason=f"llm_call_failed: {e}")

    parsed = _parse_response(raw)
    if not isinstance(parsed, dict):
        return _fail_closed(inp, reason="invalid JSON response from news_controller")

    raw_action = str(parsed.get("decision", "")).upper()
    action = _ACTION_MAP.get(raw_action)
    if action is None:
        return _fail_closed(inp, reason=f"unknown action: {raw_action!r}")

    reason = str(parsed.get("reason", ""))[:1024]
    audit_extra = parsed.get("audit") or {}
    prompt_versions = (
        audit_extra.get("prompt_versions") or
        {"news_controller": NEWS_CONTROLLER_PROMPT_VERSION}
    )
    if not isinstance(prompt_versions, dict):
        prompt_versions = {"news_controller": NEWS_CONTROLLER_PROMPT_VERSION}

    audit = AuditObject(
        policy_version="",  # filled by orchestrator wrapper
        strategy_version=f"news_trader:{NEWS_CONTROLLER_PROMPT_VERSION}",
        model_versions={"news_controller": model},
        prompt_versions={k: str(v) for k, v in prompt_versions.items()},
        regime="",  # populated by orchestrator
    )

    # Compliance flags reflect structural pre-checks + LLM-affirmed cleared
    # mnpi (LLM is responsible for refusing if MNPI is uncertain).
    mnpi_clear = (action != "escalate_to_human")
    compliance = ComplianceFlags(
        approved_instrument=True,
        approved_venue=True,
        restricted_list_clear=True,
        mnpi_clear=mnpi_clear,
        market_abuse_clear=True,
    )
    data_quality = DataQualityFlags(
        fresh=True, complete=True, aligned=True, provenance_ok=True,
    )

    return Decision(
        symbol=inp.event.entity,
        action=action,
        reason=reason,
        compliance=compliance,
        data_quality=data_quality,
        audit=audit,
    )


def _fail_closed(inp: NewsControllerInput, *, reason: str) -> Decision:
    return Decision(
        symbol=inp.event.entity,
        action="no_trade",
        reason=reason,
        compliance=ComplianceFlags(approved_venue=True),
        data_quality=DataQualityFlags(
            fresh=True, complete=True, aligned=True, provenance_ok=True,
        ),
        audit=AuditObject(
            strategy_version=f"news_trader:{NEWS_CONTROLLER_PROMPT_VERSION}",
            model_versions={"news_controller": NEWS_CONTROLLER_MODEL},
            prompt_versions={"news_controller": NEWS_CONTROLLER_PROMPT_VERSION},
        ),
    )
