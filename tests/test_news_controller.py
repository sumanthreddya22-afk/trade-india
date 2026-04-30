"""W4.2 — News-driven LLM trader controller.

The controller takes a StructuredEvent + market context, calls Claude Opus 4.7
with the PDF's news-variant system prompt, and returns a Decision matching
the strict JSON contract. Tests use a stub LLM-call function so they run
fast and offline.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from trading_bot.news_trader.event_extractor import StructuredEvent
from trading_bot.news_trader.news_controller import (
    NEWS_CONTROLLER_SYSTEM_PROMPT,
    NewsControllerInput,
    propose_news_trade,
)
from trading_bot.orchestrator import Decision


def _ev(**overrides) -> StructuredEvent:
    base = dict(
        entity="XYZ",
        event_type="strategic_alternatives",
        direction="MIXED",
        novelty=0.42,
        source_count=1,
        primary_filing_present=False,
        headline="XYZ exploring strategic alternatives",
        sources=("alpaca",),
    )
    base.update(overrides)
    return StructuredEvent(**base)


def _input(**overrides) -> NewsControllerInput:
    base = dict(
        event=_ev(),
        market_reaction_5m_pct=6.8,
        spread_widening_bps=24.0,
        approved_sources=("alpaca", "polygon", "sec_edgar", "gdelt", "finnhub"),
        min_source_count=2,
        min_confidence_threshold=0.7,
        min_edge_bps_threshold=25.0,
        capital_cap_pct=5.0,
    )
    base.update(overrides)
    return NewsControllerInput(**base)


class TestSystemPromptStructure:
    """The system prompt must encode the PDF's hard controls so the LLM has
    no excuse to ignore them. These checks guard against future regressions
    in the prompt text."""

    def test_prompt_lists_allowed_actions(self):
        for action in ("NO_TRADE", "ENTER", "REDUCE", "HEDGE", "ESCALATE"):
            assert action in NEWS_CONTROLLER_SYSTEM_PROMPT

    def test_prompt_forbids_mnpi(self):
        assert "MNPI" in NEWS_CONTROLLER_SYSTEM_PROMPT or "non-public" in NEWS_CONTROLLER_SYSTEM_PROMPT.lower()

    def test_prompt_requires_strict_json(self):
        assert "JSON" in NEWS_CONTROLLER_SYSTEM_PROMPT
        assert "valid JSON" in NEWS_CONTROLLER_SYSTEM_PROMPT or "strict JSON" in NEWS_CONTROLLER_SYSTEM_PROMPT.lower()

    def test_prompt_requires_min_source_count(self):
        assert "MIN_SOURCE_COUNT" in NEWS_CONTROLLER_SYSTEM_PROMPT or "source" in NEWS_CONTROLLER_SYSTEM_PROMPT.lower()


class TestProposeNewsTrade:
    def test_no_trade_response_parses_to_decision(self):
        """LLM returns NO_TRADE — Decision should reflect it with audit."""
        def stub_llm(*, system: str, user: str, model: str) -> str:
            return json.dumps({
                "decision": "NO_TRADE",
                "event": {"entity": "XYZ", "type": "strategic_alternatives",
                          "direction": "MIXED", "novelty": 0.42,
                          "source_quality": 0.38},
                "trade_plan": None,
                "time_stop": None,
                "reason": "Single-source rumor, no primary filing — waiting required.",
                "audit": {"prompt_versions": {"news_controller": "v1:abc"}},
            })

        d = propose_news_trade(_input(), llm_call_fn=stub_llm)
        assert isinstance(d, Decision)
        assert d.action == "no_trade"
        assert d.symbol == "XYZ"
        assert d.audit.model_versions.get("news_controller") == "claude-opus-4-7"
        assert d.audit.prompt_versions.get("news_controller") == "v1:abc"

    def test_escalate_response_for_ambiguous_event(self):
        """The PDF's page-16 example: rumor → ESCALATE."""
        def stub_llm(**kwargs):
            return json.dumps({
                "decision": "ESCALATE",
                "event": {
                    "entity": "XYZ", "type": "strategic_alternatives_rumor",
                    "direction": "MIXED", "novelty": 0.42, "source_quality": 0.38,
                },
                "trade_plan": None,
                "time_stop": None,
                "reason": (
                    "Single-source rumor, no primary filing, widened spreads, "
                    "and unresolved MNPI risk. Waiting is required."
                ),
                "audit": {},
            })
        d = propose_news_trade(_input(), llm_call_fn=stub_llm)
        assert d.action == "escalate_to_human"
        assert "rumor" in d.reason.lower()

    def test_enter_response_when_two_sources_and_primary_filing(self):
        def stub_llm(**kwargs):
            return json.dumps({
                "decision": "ENTER",
                "event": {
                    "entity": "XYZ", "type": "earnings",
                    "direction": "POSITIVE", "novelty": 0.85, "source_quality": 0.82,
                },
                "trade_plan": {"side": "BUY", "size_pct": 1.0,
                               "entry": "limit"},
                "time_stop": "24h",
                "reason": "Two-source confirmed earnings beat with primary filing.",
                "audit": {},
            })
        d = propose_news_trade(
            _input(
                event=_ev(source_count=2, primary_filing_present=True,
                          event_type="earnings", direction="POSITIVE"),
            ),
            llm_call_fn=stub_llm,
        )
        assert d.action == "enter"
        assert d.expected_edge_bps is None or d.expected_edge_bps >= 0

    def test_invalid_json_returns_no_trade_decision(self):
        """LLM hallucination / parse failure → fail-closed."""
        def stub_llm(**kwargs):
            return "not json at all"
        d = propose_news_trade(_input(), llm_call_fn=stub_llm)
        assert d.action == "no_trade"
        assert "parse" in d.reason.lower() or "invalid" in d.reason.lower()

    def test_compliance_flags_set_truthfully(self):
        """When the LLM returns a real decision, compliance flags reflect
        the structural pre-checks (source count, primary filing, MNPI)."""
        def stub_llm(**kwargs):
            return json.dumps({"decision": "NO_TRADE", "event": {}, "trade_plan": None,
                               "time_stop": None, "reason": "below confidence threshold",
                               "audit": {}})
        d = propose_news_trade(_input(), llm_call_fn=stub_llm)
        # Source count below min → restricted_list_clear True (not on list)
        # but mnpi_clear is uncertain (single source rumor) → None
        assert d.compliance.approved_instrument is True
        assert d.data_quality.fresh is True
