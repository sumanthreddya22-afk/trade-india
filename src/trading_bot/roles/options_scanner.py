"""Options Scanner — Phase 3 wiring for the options pipeline.

Runs on a slower cadence than the equities/crypto scanners (the wheel
moves on monthly cycles, not minute-to-minute). Three sequential
phases per tick, each fail-soft so one bad upstream signal cannot
block the rest:

  1. Poll options intel sources (earnings_calendar over a watchlist,
     cboe_skew daily). Writes IntelEventOptions rows.
  2. Roll up events into IntelCandidateOptions (per-underlying score
     + earnings_in_dte_window flag).
  3. Run the scout debate (Hank Marquez + Sofia Stevens → Marcus
     Whitfield) over the top candidates and write back elevate /
     dismiss verdicts.

The wheel-entry debate (Aurelio + Beatrice + Yusuf → Catherine) is
NOT fired from this role — the wheel-entry runner needs chain data +
proposal scoring (delta selection, strike, DTE) that comes from the
existing legacy ``wheel_runner``. Phase 3+ will fuse the two paths.

Universe selection: reads the wheel allowlist file (path from
``config.wheel.allowlist_path``) when present, else falls back to a
small built-in default list. Keeping the universe small avoids
hammering yfinance on every tick.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from trading_bot.roles.runner import BaseRole

logger = logging.getLogger(__name__)


_DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "AMZN", "META",
    "AMD", "JPM", "XOM",
]


def _load_universe(allowlist_path: Optional[str]) -> List[str]:
    """Read the wheel allowlist YAML (one symbol per row under ``symbols:``)."""
    if not allowlist_path:
        return list(_DEFAULT_UNIVERSE)
    path = Path(allowlist_path)
    if not path.exists():
        return list(_DEFAULT_UNIVERSE)
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:  # noqa: BLE001 — fail-soft to defaults
        logger.warning("options_scanner: allowlist read failed: %s", e)
        return list(_DEFAULT_UNIVERSE)
    syms = data.get("symbols") or []
    return [str(s).upper().strip() for s in syms if s]


class OptionsScannerRole(BaseRole):
    name = "options_scanner"
    tier = 2
    process = "daemon"
    job_description = (
        "Run options-scan at the slower wheel cadence (~daily). Three phases: "
        "poll earnings_calendar + cboe_skew sources, roll events into "
        "intel_candidates_options, run the scout debate (Hank/Sofia → Marcus). "
        "Phase 3 wiring; wheel-entry debate stays on the legacy runner until "
        "the chain proposal builder is fused with the new debate path."
    )
    sla_seconds = 300  # 5 min — yfinance + LLM reviewer + judge calls
    upstream_roles: list[str] = []
    downstream_roles = ["risk_officer", "trade_executor"]

    def _do_work(self, ctx: dict) -> dict:
        from trading_bot.pipelines.options import aggregator as opt_aggregator
        from trading_bot.pipelines.options import circuit_breaker as opt_breaker
        from trading_bot.pipelines.options.scout_debate import run_scout_debate
        from trading_bot.pipelines.options.sources.cboe_skew import poll_cboe_skew
        from trading_bot.pipelines.options.sources.earnings_calendar import (
            poll_earnings_calendar,
        )

        out: dict = {}

        # 0. Skip everything when the options breaker is hard-tripped.
        try:
            active = opt_breaker.is_tripped(self.engine)
            if active is not None and active.severity == "hard":
                out["skipped"] = True
                out["reason"] = f"options breaker tripped: {active.reason}"
                return out
        except Exception as e:
            logger.warning("options_scanner: breaker check failed: %s", e)

        # 1. Poll sources (universe = wheel allowlist).
        allowlist_path = (ctx or {}).get("allowlist_path")
        universe = _load_universe(allowlist_path)
        try:
            r1 = poll_earnings_calendar(self.engine, symbols=universe)
            out["earnings_calendar"] = r1.as_dict()
        except Exception as e:
            logger.warning("options_scanner: earnings_calendar failed: %s", e)
            out["earnings_calendar"] = {"source": "earnings_calendar", "error": str(e)}
        try:
            r2 = poll_cboe_skew(self.engine)
            out["cboe_skew"] = r2.as_dict()
        except Exception as e:
            logger.warning("options_scanner: cboe_skew failed: %s", e)
            out["cboe_skew"] = {"source": "cboe_skew", "error": str(e)}

        # 2. Roll up events into intel_candidates_options.
        try:
            roll = opt_aggregator.roll_up(self.engine)
            out["roll_up"] = roll
        except Exception as e:
            logger.warning("options_scanner: roll_up failed: %s", e)
            out["roll_up"] = {"error": str(e)}

        # 3. Run the scout debate over fresh candidates.
        try:
            result = run_scout_debate(self.engine)
            out["scout_debate"] = {
                "debated": result.debated,
                "elevated": result.elevated,
                "dismissed": result.dismissed,
                "skipped": result.skipped,
                "error": result.error,
            }
        except Exception as e:
            logger.exception("options_scanner: scout_debate failed: %s", e)
            out["scout_debate"] = {"error": str(e)}

        out["completed"] = True
        return out

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("options_candidates_elevated", 0.0, "Phase 3 KPI — placeholder")
