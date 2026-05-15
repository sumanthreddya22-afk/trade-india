"""Alpaca paper-trading adapter — v4-clean, no legacy imports.

Single object that exposes the three callbacks the daemon needs:
  * ``submit_order``         → broker_submit for execution.order_router
  * ``fetch_positions``      → positions_fetcher for snapshot + reconciliation
  * ``fetch_latest_bars``    → bars_fetcher for ingest.alpaca_writer
  * ``lookup_by_client_order_id`` → broker_lookup for orphan_loop

The adapter is constructed once at daemon startup from env vars (.env
loaded by pydantic_settings). It is *the* boundary between the v4 kernel
and any third-party SDK; nothing else in src/trading_bot/{kernel,risk,
ledger,execution} imports alpaca-py directly.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlpacaCreds:
    api_key: str
    api_secret: str
    paper: bool = True

    @classmethod
    def from_env(cls) -> "AlpacaCreds":
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_API_SECRET", "").strip()
        if not key or not secret:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_API_SECRET missing from environment. "
                "Add them to .env and restart the daemon."
            )
        # Paper-only by construction: v4 §0 forbids live until Phase 9 signed.
        return cls(api_key=key, api_secret=secret, paper=True)


class AlpacaAdapter:
    """Thin wrapper around alpaca-py.

    Methods that require network calls catch and log exceptions, returning
    sentinel values (empty list / None) instead of raising — the daemon
    must survive a brief outage without a crash loop.
    """

    def __init__(self, creds: Optional[AlpacaCreds] = None) -> None:
        self.creds = creds or AlpacaCreds.from_env()
        self._trading = None
        self._data = None

    # ---- lazy clients ----
    @property
    def trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient
            self._trading = TradingClient(
                api_key=self.creds.api_key,
                secret_key=self.creds.api_secret,
                paper=self.creds.paper,
            )
        return self._trading

    @property
    def data(self):
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._data = StockHistoricalDataClient(
                api_key=self.creds.api_key,
                secret_key=self.creds.api_secret,
            )
        return self._data

    # ---- positions ----
    def fetch_positions(self) -> list[dict]:
        try:
            raw = self.trading.get_all_positions()
        except Exception as e:  # noqa: BLE001
            log.warning("alpaca: get_all_positions failed: %s", e)
            return []
        out = []
        for p in raw:
            out.append({
                "symbol": getattr(p, "symbol", ""),
                "qty": float(getattr(p, "qty", 0) or 0),
                "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                "market_price": float(getattr(p, "current_price", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
                "asset_class": str(getattr(p, "asset_class", "us_equity")).lower(),
                "classification": "external",  # v4: every Alpaca position
                                                 # is classified at startup;
                                                 # bot-owned rows are re-tagged
                                                 # by position_classifier.
            })
        return out

    def fetch_account(self) -> dict:
        try:
            acct = self.trading.get_account()
        except Exception as e:  # noqa: BLE001
            log.warning("alpaca: get_account failed: %s", e)
            return {}
        return {
            "equity": float(getattr(acct, "equity", 0) or 0),
            "cash": float(getattr(acct, "cash", 0) or 0),
            "buying_power": float(getattr(acct, "buying_power", 0) or 0),
            "daytrade_count": int(getattr(acct, "daytrade_count", 0) or 0),
            "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
            "status": str(getattr(acct, "status", "")),
        }

    # ---- bars ----
    def fetch_latest_bars(
        self, *, symbols: tuple[str, ...], timeframe: str = "1Min",
    ) -> dict:
        """Return ``{symbol: {ts, open, high, low, close, volume}}`` for
        the most recent bar. Falls back to empty dict on any error.
        """
        if not symbols:
            return {}
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            # alpaca-py's TimeFrame minimal mapping for v4's two used cadences.
            tf = TimeFrame(1, TimeFrameUnit.Minute)
            if timeframe.endswith("Day"):
                tf = TimeFrame(1, TimeFrameUnit.Day)
            req = StockBarsRequest(
                symbol_or_symbols=list(symbols),
                timeframe=tf,
                limit=1,
            )
            bs = self.data.get_stock_bars(req)
        except Exception as e:  # noqa: BLE001
            log.warning("alpaca: get_stock_bars failed: %s", e)
            return {}
        out: dict[str, dict] = {}
        try:
            for sym in symbols:
                series = bs[sym] if sym in bs.data else None
                if not series:
                    continue
                latest = series[-1]
                out[sym] = {
                    "ts": getattr(latest, "timestamp", dt.datetime.now(dt.timezone.utc)),
                    "open": float(getattr(latest, "open", 0) or 0),
                    "high": float(getattr(latest, "high", 0) or 0),
                    "low": float(getattr(latest, "low", 0) or 0),
                    "close": float(getattr(latest, "close", 0) or 0),
                    "volume": float(getattr(latest, "volume", 0) or 0),
                }
        except Exception:
            log.exception("alpaca: bars parse failure")
        return out

    # ---- order submission (callback for execution.order_router) ----
    def submit_order(
        self, *, client_order_id: str, symbol: str, qty: float, side: str,
        order_type: str = "market", limit_price: Optional[float] = None,
        time_in_force: str = "day", asset_class: str = "us_equity",
        **_ignored,
    ) -> dict:
        """Single entry point for stocks, crypto, options.

        Routes by asset_class:
          * us_equity / equity → standard equity order (TIF=DAY default)
          * crypto → IOC required for crypto market orders
          * us_option / option → OCC-symbol order. Crucially the
            "sell" side becomes "sell_to_open" if we have no long
            position in this contract — Alpaca infers position_intent
            from the held qty.
        """
        try:
            from alpaca.trading.enums import (
                AssetClass, OrderSide, TimeInForce,
            )
            from alpaca.trading.requests import (
                LimitOrderRequest, MarketOrderRequest,
            )
            side_lower = side.lower()
            side_enum = OrderSide.BUY if side_lower == "buy" else OrderSide.SELL
            ac = (asset_class or "us_equity").lower()

            # Time-in-force: crypto requires IOC for market orders;
            # options + equity default DAY.
            if ac == "crypto":
                tif_enum = TimeInForce.IOC
            else:
                # Allow operator override via param; fall back to DAY.
                try:
                    tif_enum = TimeInForce[time_in_force.upper()]
                except KeyError:
                    tif_enum = TimeInForce.DAY

            kwargs = dict(
                symbol=symbol, qty=qty, side=side_enum,
                time_in_force=tif_enum,
                client_order_id=client_order_id,
            )
            if order_type == "limit" and limit_price is not None:
                req = LimitOrderRequest(limit_price=limit_price, **kwargs)
            else:
                req = MarketOrderRequest(**kwargs)
            resp = self.trading.submit_order(req)
            return {
                "ok": True,
                "broker_order_id": str(getattr(resp, "id", "")),
                "status": str(getattr(resp, "status", "accepted")),
                "asset_class": ac,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("alpaca: submit_order failed: %s", e)
            return {"ok": False, "broker_order_id": None, "error": str(e)}

    def fetch_option_positions(self) -> list[dict]:
        """Just options rows from get_all_positions, normalised."""
        positions = self.fetch_positions()
        out = []
        for p in positions:
            sym = p.get("symbol", "")
            # OCC symbols are SPY250516P00450000 style — 15-char min.
            if len(sym) >= 15 and any(c.isdigit() for c in sym):
                out.append(p)
        return out

    def lookup_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        try:
            order = self.trading.get_order_by_client_id(client_order_id)
        except Exception:
            return None
        if not order:
            return None
        return {
            "broker_order_id": str(getattr(order, "id", "")),
            "status": str(getattr(order, "status", "")),
            "filled_qty": float(getattr(order, "filled_qty", 0) or 0),
            "filled_avg_price": float(getattr(order, "filled_avg_price", 0) or 0),
        }


__all__ = ["AlpacaAdapter", "AlpacaCreds"]
