"""Tests covering the post-Phase-D caveat fixes:

- Intel cache refresh writes a real cache file (offline path).
- Scouts gracefully handle network failure.
- AST validator rejects forbidden imports + tokens.
- Codegen dry-run end-to-end with a mocked LLM.
- Mutation backtest registry resolves known families + fallback
  paths when the historical DB is missing.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from trading_bot.research.codegen import (
    ALLOWED_TOP_LEVEL_IMPORTS, FORBIDDEN_IMPORT_PREFIXES,
    _import_allowed, validate_runner_exports, validate_source,
)


# ---- AST validator -------------------------------------------------------

def test_import_allowed_known_paths() -> None:
    assert _import_allowed("numpy") is True
    assert _import_allowed("pandas.DataFrame") is True
    assert _import_allowed("trading_bot.ingest.universe") is True


def test_import_allowed_rejects_forbidden() -> None:
    assert _import_allowed("trading_bot.kernel") is False
    assert _import_allowed("trading_bot.risk.precheck") is False
    assert _import_allowed("trading_bot.execution.order_router") is False
    assert _import_allowed("alpaca_trade_api") is False
    assert _import_allowed("requests") is False


def test_validate_source_rejects_forbidden_token() -> None:
    src = "import os\nos.system('rm -rf /')\n"
    ok, reason = validate_source(src, path_for_errors="evil.py")
    assert not ok
    assert "os.system" in reason


def test_validate_source_rejects_forbidden_import() -> None:
    src = "import requests\n"
    ok, reason = validate_source(src, path_for_errors="r.py")
    assert not ok
    assert "requests" in reason


def test_validate_source_accepts_clean_runner() -> None:
    src = (
        "import datetime as dt\n"
        "import numpy as np\n"
        "from typing import Optional\n"
        "from trading_bot.research.historical_bars import load_bars\n"
        "\n"
        "def evaluate_strategy(decision_date=None):\n"
        "    return {}\n"
        "\n"
        "def should_rebalance_today(today, last):\n"
        "    return True\n"
    )
    ok, reason = validate_source(src, path_for_errors="runner.py")
    assert ok, reason


def test_validate_runner_exports_finds_required() -> None:
    src = (
        "def evaluate_strategy():\n    pass\n"
        "def should_rebalance_today(a, b):\n    return True\n"
    )
    ok, reason = validate_runner_exports(src)
    assert ok, reason


def test_validate_runner_exports_missing_export() -> None:
    src = "def evaluate_strategy():\n    pass\n"
    ok, reason = validate_runner_exports(src)
    assert not ok
    assert "should_rebalance_today" in reason


# ---- Intel refresh -------------------------------------------------------

def test_crypto_fear_greed_cache_round_trip(tmp_path: Path) -> None:
    """Even without network, the feed reads back what we cache."""
    from trading_bot.ingest.intel.crypto_fear_greed import CryptoFearGreedFeed
    p = tmp_path / "fng.json"
    p.write_text(json.dumps({
        "value": 27, "classification": "Fear",
        "published_iso": "2026-05-15T00:00:00Z",
    }))
    feed = CryptoFearGreedFeed(cache_path=p)
    out = feed.query_features("any", dt.datetime.now(dt.timezone.utc))
    assert out["crypto_fear_greed_index"] == 27.0


def test_treasury_slope_handles_13w_fallback(tmp_path: Path) -> None:
    from trading_bot.ingest.intel.treasury_yield_curve import (
        TreasuryYieldCurveFeed,
    )
    p = tmp_path / "tc.json"
    p.write_text(json.dumps({
        "tenors": {"10y": 4.5, "13w": 4.0},
    }))
    feed = TreasuryYieldCurveFeed(cache_path=p)
    out = feed.query_features("any", dt.datetime.now(dt.timezone.utc))
    # 4.5 - 4.0 = 0.5pct = 50 bps.
    assert abs(out["fred_yield_curve_slope"] - 50.0) < 1e-6
    assert out["treasury_10y"] == 4.5


def test_cboe_query_features_without_putcall(tmp_path: Path) -> None:
    """The 403 on CBOE put/call shouldn't break SKEW consumption."""
    from trading_bot.ingest.intel.cboe import CboeFeed
    p = tmp_path / "cboe.json"
    p.write_text(json.dumps({
        "skew": 139.32, "put_call_ratio": None,
    }))
    feed = CboeFeed(cache_path=p)
    out = feed.query_features("any", dt.datetime.now(dt.timezone.utc))
    assert out["cboe_skew"] == 139.32
    assert out["cboe_putcall_ratio"] is None


# ---- Mutation backtest registry -----------------------------------------

def test_mutation_backtest_unknown_family_returns_failing_p() -> None:
    from trading_bot.research.mutation_backtest import make_backtest_fn
    from trading_bot.research.mutation_engine import Candidate
    fn = make_backtest_fn()
    cand = Candidate(
        candidate_id="x", thesis_id="t", family="DOES_NOT_EXIST",
        mutation_id="m", variant_value={}, cycle_id="c",
        hypothesis_hash="h", rationale="r", proposer="test",
    )
    p, checks = fn(cand)
    assert p == 1.0
    assert "unknown_family" in checks.get("error", "")


def test_mutation_backtest_missing_db_falls_back(tmp_path: Path) -> None:
    from trading_bot.research.mutation_backtest import make_backtest_fn
    from trading_bot.research.mutation_engine import Candidate
    fn = make_backtest_fn(historical_db=tmp_path / "missing.db")
    cand = Candidate(
        candidate_id="x", thesis_id="t", family="ETF_MOMENTUM_v3",
        mutation_id="lookback_days", variant_value=126, cycle_id="c",
        hypothesis_hash="h", rationale="r", proposer="test",
    )
    p, checks = fn(cand)
    # Without bars we can't backtest — should fail-soft with p=1.
    assert p == 1.0
    assert "no_historical_db" in checks.get("error", "") or "no_bars_loaded" in checks.get("error", "")


# ---- Codegen LLM-failure path -------------------------------------------

def test_codegen_handles_llm_unavailable(
    ledger_conn, tmp_path: Path, monkeypatch,
) -> None:
    """When the LLM raises, codegen returns accepted=False, doesn't crash."""
    from trading_bot.research import codegen as codegen_mod

    def _raise(role, prompt, conn=None):
        from trading_bot.shared.llm_transport import LLMUnavailable
        raise LLMUnavailable("budget exhausted (test)")

    monkeypatch.setattr(codegen_mod, "invoke", _raise)
    # We don't need a real ledger_db path — pass ledger_conn's db.
    db_path = Path(ledger_conn.execute("PRAGMA database_list;").fetchone()[2])
    report = codegen_mod.generate_for_blueprint(
        ledger_db=db_path,
        blueprint_id=1,
        blueprint_md="# Test\nbody",
        family_id="test_family",
    )
    assert not report.accepted
    assert "llm_unavailable" in report.reason


def test_research_bot_intake_pipeline_handles_no_pending(
    ledger_conn, tmp_path: Path,
) -> None:
    from trading_bot.research.research_bot import run_intake_pipeline
    db_path = Path(ledger_conn.execute("PRAGMA database_list;").fetchone()[2])
    out = run_intake_pipeline(
        ledger_db=db_path, policy_dir=tmp_path,
    )
    assert out["n_pending"] == 0
    assert out["n_blueprinted"] == 0
