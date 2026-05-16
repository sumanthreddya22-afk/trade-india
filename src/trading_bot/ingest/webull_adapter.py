"""Webull OpenAPI adapter — implements ``BrokerAdapter`` for the
2026-06-12 live cutover per WS3 of the shakedown plan.

Authentication: HMAC-signed requests; session tokens rotate. The adapter
wraps every request with a token-refresh-on-401 retry. A shared
token-bucket throttle keeps us under Webull's 60 req/min trade limit.

Normalization: Webull returns ``BTCUSDT`` for crypto, ``stock`` for
equity, ``SUBMITTED/FILLED/CANCELLED`` for status. The kernel expects
``BTC/USD``, ``us_equity``, ``accepted/filled/canceled``. The
``_to_canonical_*`` helpers do the mapping.

Endpoints (from developer.webull.com/apis/docs/trade-api/*):
  * POST /trade/v1/order — submit
  * POST /trade/v1/order/query — lookup by client_order_id
  * GET  /trade/v1/account/positions
  * GET  /trade/v1/account/balance
  * GET  /trade/v1/instruments
  * GET  /market-data/v1/quote/bars

Operational gating: the broker callback inside ``execution.order_router``
already validates risk precheck + freshness before reaching this
adapter — the adapter MUST NOT add any business logic; it is the
transport boundary only.

NOTE: this module is shadow-only until ENABLE_SUBMIT=true. While
ENABLE_SUBMIT is unset/false, ``submit_order`` is stubbed to return
``{ok: False, error: "shadow_mode"}`` so the read paths can be
exercised against live Webull without placing real orders.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from trading_bot.ingest.broker_adapter import BrokerAdapter
from trading_bot.risk import broker_call_tracker

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.webull.com"
DEFAULT_TIMEOUT_S = 10.0
RATE_LIMIT_PER_MIN = 60
RATE_BUCKET_WINDOW_S = 60.0


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
_STATUS_CANON = {
    "submitted": "accepted",
    "accepted": "accepted",
    "pending": "accepted",
    "working": "accepted",
    "partial_filled": "accepted",
    "partially_filled": "accepted",
    "filled": "filled",
    "cancelled": "canceled",
    "canceled": "canceled",
    "rejected": "rejected",
    "expired": "canceled",
}

_ASSET_CLASS_CANON = {
    "stock": "us_equity",
    "equity": "us_equity",
    "us_equity": "us_equity",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "option": "us_option",
    "us_option": "us_option",
}


def _to_canonical_status(raw: str) -> str:
    return _STATUS_CANON.get((raw or "").lower(), (raw or "accepted").lower())


def _to_canonical_asset_class(raw: str) -> str:
    return _ASSET_CLASS_CANON.get((raw or "").lower(), "us_equity")


def _to_canonical_symbol(raw: str, asset_class: str) -> str:
    """Normalise symbol formats. Webull crypto uses ``BTCUSDT``;
    Alpaca-compatible canonical is ``BTC/USD``. Equity + options pass
    through unchanged."""
    if asset_class != "crypto":
        return raw
    s = (raw or "").upper()
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/USD"
    return s


def _to_webull_symbol(canonical: str, asset_class: str) -> str:
    """Inverse of ``_to_canonical_symbol`` — for outbound requests."""
    if asset_class != "crypto":
        return canonical
    return (canonical or "").replace("/USD", "USDT").replace("/USDT", "USDT")


# ---------------------------------------------------------------------------
# Creds
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WebullCreds:
    api_key: str
    api_secret: str
    account_id: str
    base_url: str = DEFAULT_BASE

    @classmethod
    def from_env(cls) -> "WebullCreds":
        key = os.environ.get("WEBULL_API_KEY", "").strip()
        secret = os.environ.get("WEBULL_API_SECRET", "").strip()
        account = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
        base = os.environ.get("WEBULL_BASE_URL", DEFAULT_BASE).strip()
        if not key or not secret or not account:
            raise RuntimeError(
                "WEBULL_API_KEY / WEBULL_API_SECRET / WEBULL_ACCOUNT_ID "
                "missing from environment. Add them to .env and restart."
            )
        return cls(api_key=key, api_secret=secret, account_id=account, base_url=base)


# ---------------------------------------------------------------------------
# Token bucket (thread-safe, shared across methods)
# ---------------------------------------------------------------------------
@dataclass
class _TokenBucket:
    capacity: int
    refill_rate_per_s: float
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)

    def acquire(self, count: int = 1, wait: bool = True) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.refill_rate_per_s,
            )
            self.last_refill = now
            if self.tokens >= count:
                self.tokens -= count
                return True
            if not wait:
                return False
            needed = count - self.tokens
            sleep_for = needed / self.refill_rate_per_s
        time.sleep(min(sleep_for, 5.0))
        return self.acquire(count, wait=False)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class WebullAdapter(BrokerAdapter):
    """Webull OpenAPI v1 adapter — implements ``BrokerAdapter``."""

    def __init__(
        self,
        creds: Optional[WebullCreds] = None,
        *,
        enable_submit: Optional[bool] = None,
    ) -> None:
        self.creds = creds or WebullCreds.from_env()
        if enable_submit is None:
            enable_submit = (
                os.environ.get("ENABLE_SUBMIT", "false").strip().lower()
                in ("1", "true", "yes")
            )
        self._enable_submit = bool(enable_submit)
        self._session_token: Optional[str] = None
        self._session_expiry: float = 0.0
        self._bucket = _TokenBucket(
            capacity=RATE_LIMIT_PER_MIN,
            refill_rate_per_s=RATE_LIMIT_PER_MIN / RATE_BUCKET_WINDOW_S,
        )

    # ---- HTTP plumbing ----
    def _sign(self, method: str, path: str, body: str, ts: str) -> str:
        msg = f"{method.upper()}\n{path}\n{body}\n{ts}".encode()
        digest = hmac.new(
            self.creds.api_secret.encode(), msg, hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode()

    def _ensure_session(self) -> None:
        if self._session_token and time.time() < self._session_expiry - 30:
            return
        self._refresh_session()

    def _refresh_session(self) -> None:
        """POST /auth/v1/token — returns ``{access_token, expires_in}``."""
        try:
            resp = self._raw_request(
                "POST", "/auth/v1/token", body={"api_key": self.creds.api_key},
                auth=False,
            )
            self._session_token = str(resp.get("access_token", "")) or None
            ttl = int(resp.get("expires_in", 3600) or 3600)
            self._session_expiry = time.time() + ttl
        except Exception as e:  # noqa: BLE001
            log.warning("webull: session refresh failed: %s", e)
            self._session_token = None
            self._session_expiry = 0.0

    def _raw_request(
        self, method: str, path: str, *,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        auth: bool = True,
        _refreshed: bool = False,
    ) -> dict:
        """Single HTTP call with HMAC signing. Handles one 401-refresh-retry
        per logical request via the ``_refreshed`` recursion sentinel.
        """
        self._bucket.acquire(1)
        url = self.creds.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(sorted(params.items()))
        payload = ""
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
        ts = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.creds.api_key,
            "X-TIMESTAMP": ts,
            "X-SIGNATURE": self._sign(method, path, payload, ts),
        }
        if auth and self._session_token:
            headers["Authorization"] = f"Bearer {self._session_token}"
        req = urllib.request.Request(
            url, method=method.upper(),
            data=payload.encode() if payload else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as r:
                raw = r.read().decode("utf-8") or "{}"
            # Only count business endpoints (skip /auth/v1/token refreshes).
            if path != "/auth/v1/token":
                broker_call_tracker.record_success()
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth and not _refreshed:
                self._refresh_session()
                return self._raw_request(
                    method, path, body=body, params=params,
                    auth=auth, _refreshed=True,
                )
            if path != "/auth/v1/token":
                broker_call_tracker.record_error()
            log.warning("webull: %s %s -> HTTP %s", method, path, e.code)
            raise
        except Exception:
            if path != "/auth/v1/token":
                broker_call_tracker.record_error()
            raise
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    def _request(self, method: str, path: str, **kw) -> dict:
        """Authed convenience wrapper."""
        self._ensure_session()
        return self._raw_request(method, path, **kw)

    # ---- BrokerAdapter implementations ----
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
        **_ignored,
    ) -> dict:
        if not self._enable_submit:
            return {
                "ok": False,
                "broker_order_id": None,
                "status": "rejected",
                "asset_class": _to_canonical_asset_class(asset_class),
                "error": "shadow_mode (ENABLE_SUBMIT=false)",
            }
        ac = _to_canonical_asset_class(asset_class)
        wb_symbol = _to_webull_symbol(symbol, ac)
        side_canon = (side or "").lower()
        body = {
            "account_id": self.creds.account_id,
            "client_order_id": client_order_id,
            "symbol": wb_symbol,
            "side": side_canon.upper(),
            "qty": qty,
            "order_type": order_type.upper(),
            "time_in_force": (
                "IOC" if ac == "crypto" else time_in_force.upper()
            ),
            "asset_class": ac,
        }
        if order_type == "limit" and limit_price is not None:
            body["limit_price"] = float(limit_price)
        try:
            resp = self._request("POST", "/trade/v1/order", body=body)
        except Exception as e:  # noqa: BLE001
            log.warning("webull: submit_order failed: %s", e)
            return {
                "ok": False, "broker_order_id": None,
                "status": "rejected", "asset_class": ac, "error": str(e),
            }
        return {
            "ok": True,
            "broker_order_id": str(resp.get("order_id") or resp.get("id", "")),
            "status": _to_canonical_status(str(resp.get("status", "submitted"))),
            "asset_class": ac,
        }

    def fetch_positions(self) -> list[dict]:
        try:
            resp = self._request(
                "GET", "/trade/v1/account/positions",
                params={"account_id": self.creds.account_id},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("webull: fetch_positions failed: %s", e)
            return []
        out: list[dict] = []
        for p in resp.get("positions", []) or []:
            try:
                ac = _to_canonical_asset_class(str(p.get("asset_class", "stock")))
                raw_sym = str(p.get("symbol", ""))
                out.append({
                    "symbol": _to_canonical_symbol(raw_sym, ac),
                    "qty": float(p.get("qty", 0) or 0),
                    "avg_entry_price": float(p.get("avg_entry_price", 0) or 0),
                    "market_price": float(p.get("market_price", 0) or 0),
                    "market_value": float(p.get("market_value", 0) or 0),
                    "asset_class": ac,
                    "classification": "external",
                })
            except Exception:
                continue
        return out

    def fetch_account(self) -> dict:
        try:
            resp = self._request(
                "GET", "/trade/v1/account/balance",
                params={"account_id": self.creds.account_id},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("webull: fetch_account failed: %s", e)
            return {}
        # Webull's field names map to our canonical schema. ``daytrade_count``
        # is critical for PDT enforcement — if missing, fail safe to a high
        # number so PDT halts new entries until we get a real value.
        return {
            "equity": float(resp.get("equity", 0) or 0),
            "cash": float(resp.get("cash", 0) or 0),
            "buying_power": float(resp.get("buying_power", 0) or 0),
            "options_buying_power": float(
                resp.get("options_buying_power", 0) or 0
            ),
            "options_trading_level": int(
                resp.get("options_trading_level", 0) or 0
            ),
            "daytrade_count": int(
                resp.get("daytrade_count", 99) if "daytrade_count" in resp else 99
            ),
            "pattern_day_trader": bool(resp.get("pattern_day_trader", False)),
            "status": str(resp.get("status", "active")),
        }

    def fetch_latest_bars(
        self, *, symbols: tuple[str, ...], timeframe: str = "1Min",
    ) -> dict:
        if not symbols:
            return {}
        # Map alpaca-style timeframes to Webull's vocabulary.
        tf_map = {"1Min": "1m", "1Day": "1d", "5Min": "5m", "15Min": "15m"}
        wb_tf = tf_map.get(timeframe, "1m")
        out: dict[str, dict] = {}
        for sym in symbols:
            ac = "crypto" if "/USD" in sym else "us_equity"
            try:
                resp = self._request(
                    "GET", "/market-data/v1/quote/bars",
                    params={
                        "symbol": _to_webull_symbol(sym, ac),
                        "timeframe": wb_tf,
                        "limit": 1,
                    },
                )
                bars = resp.get("bars", []) or []
                if not bars:
                    continue
                latest = bars[-1]
                out[sym] = {
                    "ts": latest.get("ts"),
                    "open": float(latest.get("open", 0) or 0),
                    "high": float(latest.get("high", 0) or 0),
                    "low": float(latest.get("low", 0) or 0),
                    "close": float(latest.get("close", 0) or 0),
                    "volume": float(latest.get("volume", 0) or 0),
                }
            except Exception as e:  # noqa: BLE001
                log.warning("webull: get_bars %s failed: %s", sym, e)
        return out

    def fetch_option_positions(self) -> list[dict]:
        positions = self.fetch_positions()
        return [
            p for p in positions
            if p.get("asset_class") == "us_option"
            or (
                len(p.get("symbol", "")) >= 15
                and any(c.isdigit() for c in p.get("symbol", ""))
            )
        ]

    def list_assets(self, asset_class: str = "us_equity"):
        from trading_bot.ingest.universe import AssetRecord
        try:
            resp = self._request(
                "GET", "/trade/v1/instruments",
                params={"asset_class": asset_class, "status": "ACTIVE"},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("webull: list_assets[%s] failed: %s", asset_class, e)
            return []
        records: list[AssetRecord] = []
        for a in resp.get("instruments", []) or []:
            try:
                tags_raw = a.get("tags", []) or a.get("attributes", []) or []
                tags = tuple({str(t).upper() for t in tags_raw})
                ac = _to_canonical_asset_class(
                    str(a.get("asset_class", asset_class))
                )
                records.append(AssetRecord(
                    symbol=_to_canonical_symbol(
                        str(a.get("symbol", "")), ac,
                    ),
                    asset_class=ac,
                    tradable=bool(a.get("tradable", True)),
                    fractionable=bool(a.get("fractionable", False)),
                    avg_daily_volume_usd=(
                        float(a["adv_usd"]) if a.get("adv_usd") is not None else None
                    ),
                    name=str(a.get("name", "") or "") or None,
                    attributes=tags,
                ))
            except Exception:
                continue
        return records

    def lookup_by_client_order_id(
        self, client_order_id: str,
    ) -> Optional[dict]:
        try:
            resp = self._request(
                "POST", "/trade/v1/order/query",
                body={
                    "account_id": self.creds.account_id,
                    "client_order_id": client_order_id,
                },
            )
        except Exception:
            return None
        if not resp or "order_id" not in resp:
            return None
        return {
            "broker_order_id": str(resp.get("order_id", "")),
            "status": _to_canonical_status(str(resp.get("status", ""))),
            "filled_qty": float(resp.get("filled_qty", 0) or 0),
            "filled_avg_price": float(resp.get("filled_avg_price", 0) or 0),
        }


__all__ = [
    "WebullAdapter", "WebullCreds",
    "_to_canonical_status", "_to_canonical_asset_class",
    "_to_canonical_symbol", "_to_webull_symbol",
]
