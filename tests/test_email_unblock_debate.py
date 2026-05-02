"""Tests for trading_bot.email_unblock_debate.

The render path is a pure function over a DebateEmailContext — easy to
test without SMTP. The send path is verified with a mocked sender.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trading_bot.email_unblock_debate import (
    DebateEmailContext, build_unblock_debate_email, send_debate_email,
)
from trading_bot.unblock_debate import UnblockVerdict


def _ctx(*, verdict: UnblockVerdict | None = None) -> DebateEmailContext:
    return DebateEmailContext(
        asset_class="wheel", symbol="MRNA",
        block_reason="options_cap (26.7% > 20.0%)",
        overage_ratio=0.34, candidate_score=10.0,
        proposal_summary="symbol: MRNA\nstrike: 40\nbid: 1.40",
        fundamentals="iv_rank: 100\nann_yield: 30%",
        operational_context="equity: 15000\nexisting_opt: 0",
        verdict=verdict,
    )


def _verdict(recommendation: str = "reject", confidence: str = "high") -> UnblockVerdict:
    return UnblockVerdict(
        recommendation=recommendation,
        confidence=confidence,
        reason="Aggressive case is generic; conservative case stands.",
        aggressive_text="MRNA has rich premium and post-earnings setup.",
        conservative_text="33% over cap is not borderline; sets bad precedent.",
        neutral_text="Conservative argument is more specific.",
    )


def test_subject_carries_verdict_and_symbol():
    out = build_unblock_debate_email(_ctx(verdict=_verdict()))
    assert "REJECT" in out.subject
    assert "MRNA" in out.subject
    assert "wheel" in out.subject


def test_subject_for_place_verdict():
    out = build_unblock_debate_email(_ctx(verdict=_verdict("place", "medium")))
    assert "PLACE" in out.subject


def test_subject_for_fail_closed():
    out = build_unblock_debate_email(_ctx(verdict=None))
    assert "FAIL_CLOSED" in out.subject


def test_html_body_includes_all_three_reviewer_texts():
    """Operator gets the FULL transcripts in the email so the verdict is auditable."""
    v = _verdict()
    out = build_unblock_debate_email(_ctx(verdict=v))
    body = out.html_body
    assert "rich premium and post-earnings setup" in body
    assert "33% over cap is not borderline" in body
    assert "Conservative argument is more specific" in body
    assert "Aggressive case is generic; conservative case stands." in body  # judge reason


def test_html_body_shows_summary_table_with_metadata():
    out = build_unblock_debate_email(_ctx(verdict=_verdict()))
    body = out.html_body
    assert "MRNA" in body
    assert "options_cap" in body
    assert "0.34" in body  # overage_ratio
    assert "10.00 / 10" in body  # candidate_score


def test_html_body_handles_fail_closed_gracefully():
    """When verdict is None (fail-closed), email still renders — explains
    that the gate stands and points at the daemon log."""
    out = build_unblock_debate_email(_ctx(verdict=None))
    body = out.html_body
    assert "fail-closed" in body.lower() or "fail_closed" in body.lower()
    assert "daemon stderr" in body


def test_html_escapes_user_content():
    """Defense-in-depth: even though we control the brief content, escape
    in case Alpaca symbols ever contain HTML-special chars."""
    ctx = DebateEmailContext(
        asset_class="wheel", symbol="<script>",
        block_reason="<img src=x>", overage_ratio=0.1,
        candidate_score=8.0,
        proposal_summary="bid > ask & spread", fundamentals="",
        operational_context="",
        verdict=_verdict(),
    )
    body = build_unblock_debate_email(ctx).html_body
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x>" not in body
    assert "&lt;img" in body
    assert "&amp;" in body


def test_send_debate_email_calls_send_logged():
    """send_debate_email should construct EmailSender + delegate to send_logged."""
    with patch(
        "trading_bot.email_unblock_debate.EmailSender"
    ) as mock_sender_cls, patch(
        "trading_bot.email_unblock_debate.send_logged"
    ) as mock_send_logged, patch(
        "trading_bot.email_unblock_debate.Settings"
    ) as mock_settings_cls, patch(
        "trading_bot.email_unblock_debate.load_config"
    ) as mock_load_config:
        s = MagicMock()
        s.gmail_user = "x@example.com"
        s.gmail_app_password = "x"
        mock_settings_cls.return_value = s
        cfg = MagicMock()
        cfg.email.to = "user@example.com"
        mock_load_config.return_value = cfg

        ok = send_debate_email(_ctx(verdict=_verdict()))

    assert ok is True
    mock_send_logged.assert_called_once()
    call = mock_send_logged.call_args
    assert call.kwargs["kind"] == "unblock_debate"
    assert call.kwargs["recipient"] == "user@example.com"
    assert "MRNA" in call.kwargs["subject"]


def test_send_debate_email_swallows_errors():
    """Email failures must NEVER break the scan loop."""
    with patch(
        "trading_bot.email_unblock_debate.Settings",
        side_effect=RuntimeError("env broken"),
    ):
        ok = send_debate_email(_ctx(verdict=_verdict()))
    assert ok is False  # Returned False — but no exception escaped.
