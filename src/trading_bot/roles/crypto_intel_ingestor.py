"""Crypto Intel Ingestor — Tier 2 daemon role.

Closes the orphan: ``pipelines/crypto/sources`` was built as the per-
pipeline crypto intel collector but no production role called it. The
existing ``crypto_scanner`` only runs the legacy momentum orchestrator,
which never writes to ``intel_events_crypto``. Result before this role
landed: zero crypto events, zero candidates, zero debates — every crypto
warning on the system page was a real broken-pipeline signal.

Per tick (every ``crypto_intel_minutes`` minutes, default 30):
  1. Run every wired crypto source sequentially
     (``pipelines.crypto.sources.collect_all``). Sources whose API keys
     aren't set (whale_alert / etherscan / cryptopanic) skip cleanly;
     RSS + public-API sources (coindesk / cointelegraph / apewisdom /
     binance_funding / defillama_tvl / snapshot_governance) proceed.
  2. Roll up events into ``intel_candidates_crypto`` via the crypto
     aggregator (per-symbol score + cross-source bonus + adversarial
     flags context).
  3. Run the crypto scout debate (Sasha / Lena → Diane) over fresh
     candidates above the threshold. Verdicts persisted to
     ``scout_debate_runs_crypto`` and applied to candidate rows.

Every step is fail-soft per ADR 0003. A bad source can't stop the
roll-up; a roll-up failure can't stop the debate.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from trading_bot.roles.runner import BaseRole

logger = logging.getLogger(__name__)


class CryptoIntelIngestorRole(BaseRole):
    name = "crypto_intel_ingestor"
    tier = 2
    process = "daemon"
    job_description = (
        "Run crypto intel sources → roll-up → scout debate every 30 min, "
        "24/7. Writes to intel_events_crypto / intel_candidates_crypto / "
        "scout_debate_runs_crypto. Replaces the orphan in the pipeline "
        "where pipelines/crypto/sources was built but never called from "
        "any production role."
    )
    sla_seconds = 300
    upstream_roles: list[str] = []
    downstream_roles = ["crypto_scanner", "crypto_streamer"]

    def _do_work(self, ctx: dict) -> Dict[str, Any]:
        from trading_bot.pipelines.crypto import aggregator as crypto_aggregator
        from trading_bot.pipelines.crypto import circuit_breaker as crypto_breaker
        from trading_bot.pipelines.crypto.scout_debate import run_scout_debate
        from trading_bot.pipelines.crypto.sources import collect_all
        from trading_bot.shared.config import Settings

        out: Dict[str, Any] = {}

        # 0. Skip everything when the crypto breaker is hard-tripped.
        try:
            active = crypto_breaker.is_tripped(self.engine)
            if active is not None and active.severity == "hard":
                out["skipped"] = True
                out["reason"] = f"crypto breaker tripped: {active.reason}"
                return out
        except Exception as e:
            logger.warning("crypto_intel_ingestor: breaker check failed: %s", e)

        # 1. Collect from every wired source. fail-soft per source.
        try:
            settings = Settings()
            results = collect_all(self.engine, settings=settings)
            out["per_source"] = results
            out["events_written"] = sum(int(r.get("written") or 0) for r in results)
        except Exception as e:
            logger.exception("crypto_intel_ingestor: collect_all failed: %s", e)
            out["per_source"] = []
            out["events_written"] = 0

        # 2. Roll events into intel_candidates_crypto.
        try:
            roll = crypto_aggregator.roll_up(self.engine)
            out["roll_up"] = roll
        except Exception as e:
            logger.warning("crypto_intel_ingestor: roll_up failed: %s", e)
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
            logger.exception("crypto_intel_ingestor: scout_debate failed: %s", e)
            out["scout_debate"] = {"error": str(e)}

        out["completed"] = True
        return out

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("crypto_events_per_tick", 0.0, "Phase 3 KPI — placeholder")
