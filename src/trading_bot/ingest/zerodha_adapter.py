"""Zerodha Kite Connect broker adapter — India (NSE/BSE).

Single object that exposes the callbacks the daemon needs:
  * ``submit_order``              → broker_submit for execution.order_router
  * ``fetch_positions``           → positions_fetcher for snapshot + reconciliation
  * ``fetch_account``             → account equity / margins
  * ``fetch_latest_bars``         → bars_fetcher for ingest.zerodha_writer
  * ``fetch_option_positions``    → F&O positions subset
  * ``list_assets``               → universe discovery
  * ``lookup_by_client_order_id`` → broker_lookup for orphan_loop

Authentication:
  Kite Connect uses a two-step OAuth flow:
    1. Redirect user to ``https://kite.zerodha.com/connect/login?api_key=<key>``
    2. User is redirected back to your app with a ``request_token``
    3. POST to ``/session/token`` to exchange request_token → access_token
  Access tokens expire at 06:00 IST every day. The daemon must refresh
  the token daily before market open (a cron job or a watchdog is expected
  to write the fresh ``ZERODHA_ACCESS_TOKEN`` to the environment / .env).

Asset classes (Zerodha / NSE):
  * ``nse_equity``  — NSE cash equities and ETFs
  * ``bse_equity``  — BSE cash equities
  * ``nse_fo``      — NSE Futures & Options (F&O)
  * ``crypto``      — Not natively supported by Zerodha; handled separately
                       via a CoinDCX/WazirX adapter if the crypto lane is live.

Exchange codes:
  NSE   — National Stock Exchange
  BSE   — Bombay Stock Exchange
  NFO   — NSE Futures & Options

Order types (Kite):
  MARKET, LIMIT, SL (stop-loss), SL-M (stop-loss market)

Products (Kite):
  CNC    — Cash and Carry (delivery, no intraday margin)
  MIS    — Margin Intraday Square-off (intraday, auto-squared at 15:15)
  NRML   — Normal (F&O positions, held overnight)

Transaction types:
  BUY, SELL

Validity:
  DAY   — Valid for the day
  IOC   — Immediate or Cancel
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Optional

from trading_bot.ingest.broker_adapter import BrokerAdapter
from trading_bot.risk import broker_call_tracker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZerodhaCreds:
    api_key: str
    api_secret: str
    access_token: str

    @classmethod
    def from_env(cls) -> "ZerodhaCreds":
        key = os.environ.get("ZERODHA_API_KEY", "").strip()
        secret = os.environ.get("ZERODHA_API_SECRET", "").strip()
        token = os.environ.get("ZERODHA_ACCESS_TOKEN", "").strip()
        if not key or not secret:
            raise RuntimeError(
                "ZERODHA_API_KEY / ZERODHA_API_SECRET missing from environment. "
                "Add them to .env and restart the daemon."
            )
        if not token:
            raise RuntimeError(
                "ZERODHA_ACCESS_TOKEN missing. "
                "Complete the Kite Connect OAuth flow and set this in .env. "
                "Tokens expire at 06:00 IST daily — refresh before market open."
            )
        return cls(api_key=key, api_secret=secret, access_token=token)


class ZerodhaAdapter(BrokerAdapter):
    """Thin wrapper around kiteconnect (Zerodha Kite Connect API v3).

    Methods that require network calls catch and log exceptions, returning
    sentinel values (empty list / None) instead of raising — the daemon
    must survive a brief outage without a crash loop.

    Kite Connect docs: https://kite.trade/docs/connect/v3/
    """

    def __init__(self, creds: Optional[ZerodhaCreds] = None) -> None:
        self.creds = creds or ZerodhaCreds.from_env()
        self._kite = None

    # ---- lazy client ----
    @property
    def kite(self):
        if self._kite is None:
            from kiteconnect import KiteConnect  # type: ignore[import]
            self._kite = KiteConnect(api_key=self.creds.api_key)
            self._kite.set_access_token(self.creds.access_token)
        return self._kite

    # ---- positions ----
    def fetch_positions(self) -> list[dict]:
        try:
            data = self.kite.positions()
            broker_call_tracker.record_success()
        except Exception as e:  # noqa: BLE001
            broker_call_tracker.record_error()
            log.warning("zerodha: positions() failed: %s", e)
            return []
        out = []
        # Kite returns {"net": [...], "day": [...]}; we use "net" for
        # reconciliation — it includes both intraday and delivery.
        for p in data.get("net", []):
            qty = float(p.get("quantity", 0) or 0)
            if qty == 0:
                continue
            exchange = p.get("exchange", "NSE")
            product = p.get("product", "CNC")
            inst_type = p.get("instrument_token", "")
            # Classify asset class from exchange + product
            if exchange in ("NFO", "BFO"):
                asset_class = "nse_fo"
            elif product == "CNC":
                asset_class = "nse_equity"
            else:
                asset_class = "nse_equity"
            out.append({
                "symbol": p.get("tradingsymbol", ""),
                "qty": qty,
                "avg_entry_price": float(p.get("average_price", 0) or 0),
                "market_price": float(p.get("last_price", 0) or 0),
                "market_value": float(p.get("value", 0) or 0),
                "asset_class": asset_class,
                "exchange": exchange,
                "product": product,
                "classification": "external",  # re-tagged by position_classifier
            })
        return out

    def fetch_account(self) -> dict:
        try:
            margins = self.kite.margins()  # {"equity": {...}, "commodity": {...}}
            broker_call_tracker.record_success()
        except Exception as e:  # noqa: BLE001
            broker_call_tracker.record_error()
            log.warning("zerodha: margins() failed: %s", e)
            return {}
        eq = margins.get("equity", {})
        net = eq.get("net", 0)
        available = eq.get("available", {})
        cash = float(available.get("cash", 0) or 0)
        collateral = float(available.get("collateral", 0) or 0)
        intraday_payin = float(available.get("intraday_payin", 0) or 0)
        buying_power = cash + collateral + intraday_payin
        utilised = eq.get("utilised", {})
        return {
            "equity": float(net or 0),
            "cash": cash,
            "buying_power": buying_power,
            "options_buying_power": buying_power,  # Kite does not separate
            "options_trading_level": 1,             # F&O enabled by default for Zerodha
            "daytrade_count": 0,                    # No PDT rule in India
            "pattern_day_trader": False,            # No PDT rule in India
            "status": "active",
        }

    # ---- bars ----
    def fetch_latest_bars(
        self, *, symbols: tuple[str, ...], timeframe: str = "minute",
    ) -> dict:
        """Return ``{symbol: {ts, open, high, low, close, volume}}`` for
        the most recent bar. Falls back to empty dict on any error.

        Kite intervals: minute, 3minute, 5minute, 15minute, 30minute,
        60minute, day.
        """
        if not symbols:
            return {}
        # Map generic timeframe strings to Kite's interval param.
        interval_map = {
            "1Min": "minute", "1min": "minute", "minute": "minute",
            "5Min": "5minute", "15Min": "15minute",
            "1Day": "day", "Day": "day", "daily": "day",
        }
        interval = interval_map.get(timeframe, "minute")
        out: dict[str, dict] = {}
        to_dt = dt.datetime.now(dt.timezone.utc)
        from_dt = to_dt - dt.timedelta(minutes=10) if interval == "minute" \
            else to_dt - dt.timedelta(days=5)
        for sym in symbols:
            try:
                # Kite requires instrument_token for historical data.
                # Use quote() as a lightweight proxy for the latest price.
                quote_data = self.kite.quote(f"NSE:{sym}")
                broker_call_tracker.record_success()
                q = quote_data.get(f"NSE:{sym}", {})
                ohlc = q.get("ohlc", {})
                out[sym] = {
                    "ts": dt.datetime.now(dt.timezone.utc),
                    "open": float(ohlc.get("open", 0) or 0),
                    "high": float(ohlc.get("high", 0) or 0),
                    "low": float(ohlc.get("low", 0) or 0),
                    "close": float(q.get("last_price", ohlc.get("close", 0)) or 0),
                    "volume": float(q.get("volume", 0) or 0),
                }
            except Exception as e:  # noqa: BLE001
                log.warning("zerodha: quote(%s) failed: %s", sym, e)
                continue
        return out

    # ---- order submission ----
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
        asset_class: str = "nse_equity",
        exchange: str = "NSE",
        product: str = "CNC",
        **_ignored,
    ) -> dict:
        """Single entry point for equities and F&O.

        product:
          CNC  — delivery (equity, held overnight)
          MIS  — intraday (auto-squared at 15:15 IST)
          NRML — F&O overnight

        exchange:
          NSE  — equities / ETFs
          BSE  — equities / ETFs
          NFO  — NSE F&O (index/stock options and futures)
        """
        try:
            from kiteconnect import KiteConnect  # noqa: F401 — confirms import

            transaction_type = (
                self.kite.TRANSACTION_TYPE_BUY
                if side.lower() == "buy"
                else self.kite.TRANSACTION_TYPE_SELL
            )

            otype_map = {
                "market": self.kite.ORDER_TYPE_MARKET,
                "limit": self.kite.ORDER_TYPE_LIMIT,
                "sl": self.kite.ORDER_TYPE_SL,
                "sl-m": self.kite.ORDER_TYPE_SLM,
            }
            kite_order_type = otype_map.get(order_type.lower(), self.kite.ORDER_TYPE_MARKET)

            validity_map = {
                "day": self.kite.VALIDITY_DAY,
                "ioc": self.kite.VALIDITY_IOC,
            }
            kite_validity = validity_map.get(time_in_force.lower(), self.kite.VALIDITY_DAY)

            # Map asset_class → exchange + product defaults
            if asset_class == "nse_fo":
                exchange = exchange or "NFO"
                product = product or "NRML"
            else:
                exchange = exchange or "NSE"
                product = product or "CNC"

            kwargs = dict(
                tradingsymbol=symbol,
                exchange=exchange,
                transaction_type=transaction_type,
                quantity=int(qty),
                order_type=kite_order_type,
                product=product,
                validity=kite_validity,
                tag=client_order_id[:20],  # Kite tag field max 20 chars
            )
            if order_type.lower() == "limit" and limit_price is not None:
                kwargs["price"] = limit_price

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                **kwargs,
            )
            broker_call_tracker.record_success()
            return {
                "ok": True,
                "broker_order_id": str(order_id),
                "status": "accepted",
                "asset_class": asset_class,
            }
        except Exception as e:  # noqa: BLE001
            broker_call_tracker.record_error()
            log.warning("zerodha: place_order failed: %s", e)
            return {"ok": False, "broker_order_id": None, "error": str(e)}

    def fetch_option_positions(self) -> list[dict]:
        """F&O rows from positions, normalised."""
        positions = self.fetch_positions()
        return [p for p in positions if p.get("asset_class") == "nse_fo"]

    def list_assets(self, asset_class: str = "nse_equity"):
        """Return active tradable ``AssetRecord``s in ``asset_class``.

        Kite provides a full instrument dump at
        https://api.kite.trade/instruments — we download and filter.
        Returns ``[]`` on transient failures.
        """
        from trading_bot.ingest.universe import AssetRecord
        try:
            exchange_map = {
                "nse_equity": "NSE",
                "bse_equity": "BSE",
                "nse_fo": "NFO",
            }
            exchange = exchange_map.get(asset_class, "NSE")
            instruments = self.kite.instruments(exchange=exchange)
            broker_call_tracker.record_success()
        except Exception as e:  # noqa: BLE001
            log.warning("zerodha: instruments(%s) failed: %s", asset_class, e)
            return []
        records: list[AssetRecord] = []
        for inst in instruments or []:
            try:
                itype = str(inst.get("instrument_type", "")).upper()
                # Classify attributes
                tags: list[str] = [itype]
                if itype == "EQ":
                    tags.append("EQUITY")
                elif itype in ("CE", "PE"):
                    tags.append("OPTION")
                elif itype in ("FUT",):
                    tags.append("FUTURE")
                # NSE ETFs are listed as EQ with segment "NSE-EQ"
                # and names ending with "ETF" or "BEES"
                name = str(inst.get("name", "") or "")
                if "ETF" in name.upper() or name.upper().endswith("BEES"):
                    tags.append("ETF")
                records.append(AssetRecord(
                    symbol=str(inst.get("tradingsymbol", "")),
                    asset_class=asset_class,
                    tradable=True,
                    fractionable=False,
                    avg_daily_volume_usd=None,
                    name=name or None,
                    attributes=tuple(set(tags)),
                ))
            except Exception:  # noqa: BLE001
                continue
        return records

    def lookup_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        """Kite does not support lookup-by-tag directly; we scan today's
        order book and match on the tag field."""
        try:
            orders = self.kite.orders()
        except Exception:
            return None
        for order in orders or []:
            tag = str(order.get("tag", "") or "")
            # client_order_id is stored in tag (first 20 chars).
            if tag and client_order_id.startswith(tag):
                status_raw = str(order.get("status", "")).lower()
                status_map = {
                    "complete": "filled",
                    "cancelled": "canceled",
                    "rejected": "rejected",
                    "open": "accepted",
                    "pending": "accepted",
                }
                return {
                    "broker_order_id": str(order.get("order_id", "")),
                    "status": status_map.get(status_raw, status_raw),
                    "filled_qty": float(order.get("filled_quantity", 0) or 0),
                    "filled_avg_price": float(order.get("average_price", 0) or 0),
                }
        return None


__all__ = ["ZerodhaAdapter", "ZerodhaCreds"]
