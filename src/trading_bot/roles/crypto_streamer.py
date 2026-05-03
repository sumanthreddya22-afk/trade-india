"""CryptoStreamerRole — Tier 1 lab role (Phase 1G follow-on).

Wraps ``pipelines/crypto/streams.poll_all_streams`` + the express-lane
dispatcher so the daemon scheduler runs them on a tight cadence
(default 60s) and routes any new stream events into either the express
hold debate (for held positions) or the express scout debate (for
newly-flagged candidates) within ~60 seconds of arrival.

This is the operational glue that makes Phase 1G's express-lane real
in production. Dev / test invocations can call
``poll_all_streams`` + ``dispatch_pending`` directly without going
through the role.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from trading_bot.roles.runner import BaseRole

logger = logging.getLogger(__name__)


class CryptoStreamerRole(BaseRole):
    name = "crypto_streamer"
    tier = 1
    process = "lab"
    job_description = (
        "Polls crypto stream sources (Whale Alert, DefiLlama TVL, "
        "Etherscan whale wallets, Binance funding) every minute, "
        "ingests new events into intel_stream_events_crypto, then "
        "dispatches express scout/hold debates for any unprocessed "
        "events (per-symbol; held positions trigger hold debate, "
        "newly-flagged candidates trigger scout)."
    )
    sla_seconds = 90
    upstream_roles: list[str] = []
    downstream_roles = ["crypto_scanner", "position_monitor"]

    def __init__(
        self,
        *,
        engine,
        settings: Any = None,
        broker: Optional[Any] = None,
        only_streams: Optional[List[str]] = None,
        skip_streams: Optional[List[str]] = None,
    ) -> None:
        super().__init__(engine=engine)
        self._settings = settings
        self._broker = broker
        self._only = only_streams
        self._skip = skip_streams

    def _do_work(self, ctx) -> dict:
        """Single tick: poll → dispatch."""
        from trading_bot.pipelines.crypto.streams import poll_all_streams
        from trading_bot.pipelines.crypto.event_streamer import dispatch_pending
        from trading_bot.state_fallback import is_fallback_active

        if is_fallback_active(self.engine):
            return {"skipped": True, "reason": "fallback_active"}

        settings = self._settings or _build_settings()

        # Step 1: Poll every wired stream and ingest new rows.
        ingest_result = poll_all_streams(
            self.engine, settings=settings,
            skip=self._skip, only=self._only,
        )

        # Step 2: Dispatch unprocessed stream events through the express lane.
        held_symbols = self._held_crypto_symbols()
        dispatch_result = dispatch_pending(
            self.engine,
            held_symbols=held_symbols,
            on_hold_trigger=self._on_hold_trigger,
            on_scout_trigger=self._on_scout_trigger,
        )

        return {
            "ingested_per_source": ingest_result,
            "dispatched": [
                {"symbol": r.symbol, "action": r.action, "reason": r.reason}
                for r in dispatch_result
            ],
            "held_symbols": list(held_symbols),
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("express_dispatch_count", 0.0, "Phase 1G KPI — placeholder")

    # ----- internals --------------------------------------------------

    def _held_crypto_symbols(self) -> List[str]:
        """Return current crypto positions from the broker. Returns an
        empty list if the broker is unavailable — the express dispatcher
        treats that as "no held positions" and routes everything as
        scout candidates, which is the correct fail-soft behaviour.
        """
        if self._broker is None:
            try:
                from trading_bot.shared.alpaca_client import AlpacaClient
                from trading_bot.shared.config import Settings
                self._broker = AlpacaClient(Settings())
            except Exception as e:  # noqa: BLE001
                logger.warning("crypto_streamer: broker unavailable (%s)", e)
                return []
        try:
            positions = self._broker.list_positions()
        except Exception as e:  # noqa: BLE001
            logger.warning("crypto_streamer: list_positions failed: %s", e)
            return []
        return [
            p.symbol for p in positions
            if str(getattr(p, "asset_class", "")).lower() == "crypto"
        ]

    def _on_hold_trigger(self, symbol: str, event) -> None:
        """Express-lane hold debate kickoff. Builds a HeldCryptoPosition
        + TriggerContext from current state and runs the hold debate."""
        from trading_bot.pipelines.crypto.position_monitor import (
            HeldCryptoPosition, monitor_positions,
        )

        position = self._build_held_position_from_broker(symbol)
        if position is None:
            logger.warning("crypto_streamer: cannot build position for %s", symbol)
            return
        try:
            monitor_positions(self.engine, positions=[position])
        except Exception as e:  # noqa: BLE001
            logger.exception("crypto_streamer hold-trigger failed for %s: %s", symbol, e)

    def _on_scout_trigger(self, symbol: str, event) -> None:
        """Express-lane scout debate kickoff. Re-runs the scout debate
        immediately (without waiting for the regular ingestor tick)."""
        from trading_bot.pipelines.crypto.scout_debate import run_scout_debate

        try:
            run_scout_debate(self.engine, threshold=0.0, batch_limit=5)
        except Exception as e:  # noqa: BLE001
            logger.exception("crypto_streamer scout-trigger failed for %s: %s", symbol, e)

    def _build_held_position_from_broker(self, symbol: str):
        """Best-effort HeldCryptoPosition from broker state. Returns None if
        the symbol isn't actually held (race with regular position monitor)."""
        from trading_bot.pipelines.crypto.position_monitor import HeldCryptoPosition

        if self._broker is None:
            return None
        try:
            positions = self._broker.list_positions()
        except Exception as e:  # noqa: BLE001
            logger.warning("crypto_streamer: list_positions failed: %s", e)
            return None
        for p in positions:
            if p.symbol.upper() == symbol.upper():
                return HeldCryptoPosition(
                    symbol=symbol,
                    side="long",  # crypto is long-only on Alpaca paper
                    qty=float(getattr(p, "qty", 0)),
                    entry_price=float(getattr(p, "avg_entry_price", 0)),
                    current_price=float(getattr(p, "current_price",
                                                getattr(p, "market_price", 0))),
                    pnl_pct=float(getattr(p, "unrealized_plpc", 0)) * 100,
                    has_stablecoin_exposure="USD" in symbol.upper(),
                )
        return None


def _build_settings():
    """Lazy Settings build — keeps the role import cheap for unit tests."""
    from trading_bot.shared.config import Settings
    return Settings()
