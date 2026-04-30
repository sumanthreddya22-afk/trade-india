"""W2b — Compliance gates.

PDF prescribes per-decision compliance flags: ``approved_instrument``,
``approved_venue``, ``restricted_list_clear``, ``mnpi_clear``,
``market_abuse_clear``. This module supplies the deterministic checks; the
orchestrator runs them BEFORE the risk gate so a restricted-list match
short-circuits sizing/VaR calculation entirely.

Compliance gates fail-closed: if a source is unreachable, the gate reports
"can't verify" — we'd rather skip the trade than assume cleared.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trading_bot.compliance import (
    ALPACA_PAPER_BASE_URL,
    check_approved_venue,
    check_restricted,
    load_restricted_list,
)


class TestRestrictedList:
    def test_loads_empty_yaml(self, tmp_path: Path):
        p = tmp_path / "restricted.yaml"
        p.write_text("symbols: []\n")
        assert load_restricted_list(p) == set()

    def test_loads_symbols(self, tmp_path: Path):
        p = tmp_path / "restricted.yaml"
        p.write_text(yaml.safe_dump({"symbols": ["NVDA", "BTC/USD", "tsla"]}))
        out = load_restricted_list(p)
        # Case-normalized to upper
        assert "NVDA" in out
        assert "TSLA" in out
        assert "BTC/USD" in out

    def test_missing_file_returns_empty(self, tmp_path: Path):
        out = load_restricted_list(tmp_path / "nope.yaml")
        assert out == set()

    def test_check_clear_when_not_in_list(self):
        clear, reason = check_restricted("NVDA", restricted={"AAPL"})
        assert clear is True
        assert reason == ""

    def test_check_blocked_when_in_list(self):
        clear, reason = check_restricted("NVDA", restricted={"NVDA"})
        assert clear is False
        assert "restricted" in reason.lower() or "NVDA" in reason

    def test_check_is_case_insensitive(self):
        clear, _ = check_restricted("nvda", restricted={"NVDA"})
        assert clear is False
        clear, _ = check_restricted("NVDA", restricted={"nvda"})
        assert clear is False


class TestApprovedVenue:
    def test_paper_alpaca_is_approved(self):
        ok, reason = check_approved_venue(ALPACA_PAPER_BASE_URL)
        assert ok is True
        assert reason == ""

    def test_paper_alpaca_with_v2_suffix_is_approved(self):
        ok, _ = check_approved_venue(ALPACA_PAPER_BASE_URL + "/v2")
        assert ok is True

    def test_live_alpaca_is_rejected(self):
        ok, reason = check_approved_venue("https://api.alpaca.markets")
        assert ok is False
        assert "venue" in reason.lower() or "live" in reason.lower()

    def test_unknown_venue_is_rejected(self):
        ok, reason = check_approved_venue("https://malicious.example.com")
        assert ok is False
        assert reason
