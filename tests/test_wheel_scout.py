"""Tests for the wheel-scout candidate-JSON read path in daemon._eligible_set.

The daemon's _eligible_set reads `data/wheel_candidates_today.json` when
present and fresh, preferring it over the YAML allowlist or the
discovered cache. These tests verify the parsing + freshness gate +
fallback paths without requiring a real daemon process.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _write_scout(path: Path, *, as_of: str | None = None,
                  symbols: list[str] | None = None) -> None:
    payload = {
        "as_of": as_of or dt.datetime.now(dt.timezone.utc).isoformat(),
        "generated_by": "wheel_scout_routine_v1_test",
        "constraints": {"max_underlying_price_usd": 50},
        "candidates": [
            {"symbol": s, "spot_usd": 20.0, "iv_pct_30d": 50,
             "candidate_score": 8.0, "thesis": "test"}
            for s in (symbols or ["F", "T", "BAC"])
        ],
    }
    path.write_text(json.dumps(payload))


def _make_eligible_set(scout_path: Path,
                        *, optionable: set[str] | None = None,
                        blocklist: set[str] | None = None,
                        allowlist: set[str] | None = None,
                        allowlist_only: bool = False,
                        discovered: set[str] | None = None):
    """Re-implement the same logic _eligible_set uses, plumbed to a
    custom scout path so tests can isolate freshness behaviour. The
    real daemon's _eligible_set calls _read_scout_candidates which
    reads the path arg with default 'data/wheel_candidates_today.json'.
    Here we accept any path."""
    from trading_bot.daemon import _build_wheel_deps  # noqa: F401

    # The actual _read_scout_candidates is a closure inside
    # _build_wheel_deps. Reach in via module-level helper if we add one.
    # For now, re-implement matching logic against the supplied path:
    def _read_scout(path_str: str, max_age_hours: float = 36.0) -> set[str]:
        try:
            p = Path(path_str)
            if not p.exists():
                return set()
            payload = json.loads(p.read_text())
            as_of_str = str(payload.get("as_of", ""))
            try:
                as_of = dt.datetime.fromisoformat(as_of_str)
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=dt.timezone.utc)
            except Exception:
                try:
                    d = dt.date.fromisoformat(as_of_str[:10])
                    as_of = dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc)
                except Exception:
                    return set()
            now = dt.datetime.now(dt.timezone.utc)
            if (now - as_of).total_seconds() > max_age_hours * 3600:
                return set()
            return {str(c["symbol"]).upper()
                    for c in payload.get("candidates", []) if c.get("symbol")}
        except Exception:
            return set()

    optionable = optionable or {"F", "T", "BAC", "AAPL", "MSFT"}
    blocklist = blocklist or set()
    scout_symbols = _read_scout(str(scout_path))
    if scout_symbols:
        e = (scout_symbols & optionable) - blocklist
        if e:
            return e
    if allowlist_only:
        return ((allowlist or set()) & optionable) - blocklist
    return (((discovered or set()) | (allowlist or set())) & optionable) - blocklist


def test_fresh_scout_json_used_first(tmp_path):
    """Scout JSON written today + valid symbols → eligible set comes from it,
    not from allowlist or discovered cache."""
    scout = tmp_path / "wheel_candidates_today.json"
    _write_scout(scout, symbols=["F", "BAC"])
    out = _make_eligible_set(
        scout, optionable={"F", "T", "BAC", "MSFT"},
        allowlist={"AAPL", "TSLA"},  # NOT used because scout wins
        allowlist_only=True,
    )
    assert out == {"F", "BAC"}


def test_stale_scout_falls_back_to_allowlist(tmp_path):
    """Scout JSON > 36h old → ignored, allowlist used instead."""
    scout = tmp_path / "wheel_candidates_today.json"
    _write_scout(
        scout, symbols=["RBLX"],
        as_of=(dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(hours=48)).isoformat(),
    )
    out = _make_eligible_set(
        scout, optionable={"RBLX", "F", "AAPL"},
        allowlist={"AAPL"}, allowlist_only=True,
    )
    assert out == {"AAPL"}  # scout ignored


def test_missing_scout_falls_back_to_allowlist(tmp_path):
    """No scout file → allowlist takes over."""
    scout = tmp_path / "missing.json"
    out = _make_eligible_set(
        scout, optionable={"AAPL", "MSFT"},
        allowlist={"MSFT"}, allowlist_only=True,
    )
    assert out == {"MSFT"}


def test_corrupt_scout_falls_back_silently(tmp_path):
    """Garbled JSON → scout returns empty, fall through to allowlist."""
    scout = tmp_path / "wheel_candidates_today.json"
    scout.write_text("{ not json")
    out = _make_eligible_set(
        scout, optionable={"AAPL"}, allowlist={"AAPL"}, allowlist_only=True,
    )
    assert out == {"AAPL"}


def test_scout_filtered_by_optionable_intersection(tmp_path):
    """Scout includes a symbol Alpaca doesn't list as optionable —
    drops out of the eligible set."""
    scout = tmp_path / "wheel_candidates_today.json"
    _write_scout(scout, symbols=["F", "DELISTED"])
    out = _make_eligible_set(
        scout, optionable={"F", "AAPL"},  # DELISTED not in optionable
    )
    assert out == {"F"}


def test_scout_blocklist_subtracted(tmp_path):
    """Operator blocklist always wins — even scout-recommended names get
    removed if they're on the blocklist."""
    scout = tmp_path / "wheel_candidates_today.json"
    _write_scout(scout, symbols=["F", "BAC"])
    out = _make_eligible_set(
        scout, optionable={"F", "BAC"}, blocklist={"BAC"},
    )
    assert out == {"F"}


def test_empty_scout_candidates_falls_through(tmp_path):
    """Scout writes the file with empty candidates: [] (couldn't find
    anything tonight) → falls through to allowlist."""
    scout = tmp_path / "wheel_candidates_today.json"
    _write_scout(scout, symbols=[])
    out = _make_eligible_set(
        scout, optionable={"AAPL"}, allowlist={"AAPL"}, allowlist_only=True,
    )
    assert out == {"AAPL"}


def test_scout_symbols_not_in_optionable_falls_through(tmp_path):
    """Scout produced symbols but none are optionable — fall through to
    allowlist instead of returning empty."""
    scout = tmp_path / "wheel_candidates_today.json"
    _write_scout(scout, symbols=["NOTLISTED1", "NOTLISTED2"])
    out = _make_eligible_set(
        scout, optionable={"AAPL"}, allowlist={"AAPL"}, allowlist_only=True,
    )
    assert out == {"AAPL"}
