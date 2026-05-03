"""Intel Ingestor — Tier 2 lab role.

Every 30 min during US market hours (1h after-hours), pulls news /
filings / social mentions from the wired sources and rolls them into
the ``intel_candidates`` table. The daemon's universe sources consult
that table FIRST on each scan, falling back to the existing screeners
when the pool is stale or empty (cold-start safety net).

The role is intentionally thin: each source has its own collector in
``trading_bot.intel.sources``, the aggregator owns the math. This role
just orchestrates them and logs per-source counts.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from trading_bot.intel import aggregator, sources as intel_sources
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun


class IntelIngestorRole(BaseRole):
    name = "intel_ingestor"
    tier = 2
    process = "lab"
    job_description = (
        "Continuously aggregates news, filings, social, and macro signals "
        "into intel_candidates. Daemon scans consume from this pool first."
    )
    sla_seconds = 5 * 60
    upstream_roles = ["sentiment_analyst", "vip_listener"]
    downstream_roles = ["stock_scanner", "crypto_scanner", "wheel_scanner"]

    def __init__(
        self,
        *,
        engine,
        settings=None,
        seed_symbols: Iterable[str] | None = None,
    ):
        super().__init__(engine=engine)
        self._settings = settings
        self._seed = list(seed_symbols or [])

    def _do_work(self, ctx) -> dict:
        seed = ctx.get("seed_symbols") or self._seed
        if not seed:
            seed = _default_seed_symbols()
        settings = self._settings or _build_settings()

        # 1. Pull from every wired source, write events.
        per_source = intel_sources.collect_all(
            self.engine, settings=settings, seed_symbols=seed,
        )

        # 2. Roll up intel_events → intel_candidates.
        roll = aggregator.roll_up(self.engine)

        # 3. Phase B — SEC 8-K override path. Re-elevate dismissed symbols
        #    when a fresh sec_8k event arrives within the last hour. Runs
        #    BEFORE scout debate so a re-eligible symbol can be re-debated
        #    in this same tick.
        override_summary: dict = {"overrode": [], "n_overrode": 0}
        # 4. Phase B — scout debate. Fires when there are new high-score
        #    candidates and the daily cap permits. Fail-soft: any error
        #    (no creds, budget halt, SDK exception) returns an empty
        #    result; downstream consumers see candidates unchanged.
        scout_summary: dict = {"verdicts": 0, "skipped_reason": "scout disabled"}
        scout_enabled = bool(getattr(settings, "scout_debate_enabled", True))
        if scout_enabled:
            try:
                from trading_bot.intel import scout_debate
                override_summary = scout_debate.override_dismissals_for_sec_8k(
                    self.engine,
                )
                threshold = float(
                    getattr(settings, "scout_debate_threshold",
                            scout_debate.DEFAULT_THRESHOLD)
                )
                batch_limit = int(
                    getattr(settings, "scout_debate_batch_limit",
                            scout_debate.DEFAULT_BATCH_LIMIT)
                )
                daily_cap = int(
                    getattr(settings, "scout_debate_daily_cap",
                            scout_debate.DEFAULT_DAILY_CAP)
                )
                today_count = scout_debate.count_todays_scout_debates(self.engine)
                if not scout_debate.should_scout_debate(
                    daily_debate_count=today_count, daily_cap=daily_cap,
                ):
                    scout_summary = {
                        "verdicts": 0,
                        "skipped_reason": f"daily_cap reached ({today_count}/{daily_cap})",
                    }
                else:
                    elevate_boost = float(
                        getattr(settings, "scout_elevate_boost",
                                scout_debate.DEFAULT_ELEVATE_BOOST)
                    )
                    dismiss_ttl = float(
                        getattr(settings, "scout_dismiss_ttl_hours",
                                scout_debate.DEFAULT_DISMISS_TTL_HOURS)
                    )
                    result = scout_debate.run_scout_debate(
                        self.engine,
                        threshold=threshold, batch_limit=batch_limit,
                        elevate_boost=elevate_boost,
                        dismiss_ttl_hours=dismiss_ttl,
                    )
                    scout_summary = {
                        "verdicts": len(result.verdicts),
                        "n_candidates": result.n_candidates_in_brief,
                        "skipped_reason": result.skipped_reason,
                    }
            except Exception as e:  # noqa: BLE001
                # Outermost guard — even import failure shouldn't break
                # the ingestor role. Phase B is best-effort.
                scout_summary = {"verdicts": 0, "error": str(e)}

        return {
            "per_source": per_source,
            "rolled_up": roll,
            "scout_debate": scout_summary,
            "scout_8k_override": override_summary,
            "n_seed_symbols": len(seed),
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "ingestor_runs",
            float(count),
            f"{count} intel-ingestor runs in last {lookback_days}d",
        )


def _default_seed_symbols() -> list[str]:
    """Seed for symbol-aware sources (Alpaca News, Finnhub). Order matters
    only insofar as some sources truncate at the API rate-limit boundary
    — we put liquidity-leaders first.

    Source preference for the seed:
      1. ``CORE_LIQUID_TICKERS`` (250 names) if available.
      2. Empty list (every source then degrades to its zero-arg behavior:
         insider filings + apewisdom + macro still work).
    """
    try:
        from trading_bot.universe import CORE_LIQUID_TICKERS
        return list(CORE_LIQUID_TICKERS)
    except Exception:
        return []


def _build_settings():
    try:
        from trading_bot.shared.config import Settings
        return Settings()
    except Exception:
        return None
