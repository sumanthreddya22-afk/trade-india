"""BrokerAdapter ABC — the single boundary between the v4 kernel and any
broker SDK.

Plan WS3: extract a 7-method ABC so the daemon can flip between Alpaca
paper and Webull live via the ``BROKER`` env var without any other code
change. Every concrete adapter (``AlpacaAdapter``, ``WebullAdapter``)
inherits from this class and normalises its broker-specific responses
into the canonical strings the rest of the codebase expects:

  * ``asset_class``: ``us_equity`` | ``crypto`` | ``us_option``
  * order ``status``: ``accepted`` | ``filled`` | ``canceled`` | ``rejected``
  * ``side``: ``buy`` | ``sell`` (sell-to-close inferred from held qty)

Methods that require network calls MUST catch + log exceptions and
return sentinel values (empty list / None / ``{ok: False}``) instead of
raising — the daemon must survive a brief outage without a crash loop.
"""
from __future__ import annotations

import abc
from typing import Optional


class BrokerAdapter(abc.ABC):
    """Abstract base for every broker integration.

    All concrete adapters MUST implement these 7 methods. The kernel
    only ever talks to a ``BrokerAdapter``; concrete brokers are
    selected by ``BROKER`` env var in ``cli.py``.
    """

    @abc.abstractmethod
    def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
        asset_class: str = "us_equity",
        **kwargs,
    ) -> dict:
        """Submit an order. Returns:

            {
              "ok": bool,
              "broker_order_id": str | None,
              "status": "accepted" | "filled" | "canceled" | "rejected",
              "asset_class": str,
              "error": str (only when ok=False)
            }

        Idempotency: callers pass the same ``client_order_id`` on retry;
        the broker's own dedupe is responsible for not double-filling.
        """

    @abc.abstractmethod
    def fetch_positions(self) -> list[dict]:
        """Return a list of position dicts. Each dict has keys:

            symbol, qty, avg_entry_price, market_price, market_value,
            asset_class, classification (default: ``external``)
        """

    @abc.abstractmethod
    def fetch_account(self) -> dict:
        """Return account state. Required keys:

            equity, cash, buying_power, options_buying_power,
            options_trading_level, daytrade_count, pattern_day_trader,
            status

        Returns ``{}`` on transient failure (daemon stays up; risk
        precheck treats missing equity as a hard halt).
        """

    @abc.abstractmethod
    def fetch_latest_bars(
        self,
        *,
        symbols: tuple[str, ...],
        timeframe: str = "1Min",
    ) -> dict:
        """Return ``{symbol: {ts, open, high, low, close, volume}}`` for
        the most recent bar at the requested timeframe.
        """

    @abc.abstractmethod
    def fetch_option_positions(self) -> list[dict]:
        """Subset of ``fetch_positions`` filtered to option contracts only."""

    @abc.abstractmethod
    def list_assets(self, asset_class: str = "us_equity"):
        """Return active tradable ``AssetRecord``s in ``asset_class``.
        Returns ``[]`` on transient failure.
        """

    @abc.abstractmethod
    def lookup_by_client_order_id(
        self, client_order_id: str,
    ) -> Optional[dict]:
        """Return ``{broker_order_id, status, filled_qty, filled_avg_price}``
        or None if the order is unknown to the broker.
        """


__all__ = ["BrokerAdapter"]
