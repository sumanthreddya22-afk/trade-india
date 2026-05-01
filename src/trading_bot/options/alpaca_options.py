"""OptionAlpacaClient — wraps OptionHistoricalDataClient (chain + Greeks via the
free indicative feed) and the TradingClient for option order submission.
Paper-only: rejects any non-paper base_url at construction.

Hardening (2026-05-01): every options-chain call is wrapped in a per-attempt
30s timeout + 2 retries. On 2026-04-30/05-01 the wheel_scan job hung for
50 minutes / 3+ hours respectively because Alpaca's options-chain endpoint
went unresponsive without any client-side bound. The thread-pool wrapper
guarantees the role unblocks even when the upstream endpoint stalls.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
from decimal import Decimal

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide, OrderType, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from trading_bot.alpaca_client import PAPER_URL_PREFIX, _audit_order_submitted
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled
from trading_bot.options.chain import ChainContract
from trading_bot.options.symbols import parse_occ


log = logging.getLogger(__name__)

# Defense-in-depth around Alpaca's options-chain endpoint, which has no
# native client-side timeout. Per-attempt cap + bounded retries; total
# worst-case wall time ≈ (retries+1) × timeout = 90s.
_OPTION_CHAIN_TIMEOUT_S = 30.0
_OPTION_CHAIN_RETRIES = 2

# Module-level executor: shared across all OptionAlpacaClient instances so
# we don't churn threads. Threads that outlive a timeout (Python can't
# actually kill a blocking syscall) keep running to completion in the
# background — harmless, since the result is discarded.
_options_chain_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="alpaca-options-chain",
)


def _call_with_timeout(fn, *, timeout_s: float, retries: int, label: str):
    """Run fn() with a wall-clock timeout and bounded retries.

    Raises AlpacaClientError after exhausting attempts. Non-retryable
    errors (e.g. malformed request) propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 2):  # retries=2 → 3 attempts
        future = _options_chain_executor.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as e:
            last_exc = e
            future.cancel()  # advisory; may not interrupt the network call
            log.warning(
                "%s timed out after %.0fs (attempt %d/%d)",
                label, timeout_s, attempt, retries + 1,
            )
            continue
        except Exception as e:
            msg = str(e).lower()
            retryable = (
                "rate" in msg
                or "timeout" in msg
                or "connection" in msg
                or "503" in msg
                or "504" in msg
                or "502" in msg
            )
            if not retryable:
                raise
            last_exc = e
            log.warning(
                "%s failed (attempt %d/%d): %s",
                label, attempt, retries + 1, e,
            )
            continue
    raise AlpacaClientError(
        f"{label} exhausted {retries + 1} attempts; last error: {last_exc}"
    )


class OptionAlpacaClient:
    def __init__(self, settings) -> None:
        if not settings.alpaca_base_url.startswith(PAPER_URL_PREFIX):
            raise LiveModeDisabled()
        self._data = OptionHistoricalDataClient(
            api_key=settings.alpaca_api_key, secret_key=settings.alpaca_api_secret,
        )
        self._trading = TradingClient(
            api_key=settings.alpaca_api_key, secret_key=settings.alpaca_api_secret, paper=True,
        )

    def get_chain(
        self, underlying: str, *,
        expiration_gte: dt.date, expiration_lte: dt.date,
    ) -> list[ChainContract]:
        req = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=expiration_gte,
            expiration_date_lte=expiration_lte,
        )
        try:
            snap_map = _call_with_timeout(
                lambda: self._data.get_option_chain(req),
                timeout_s=_OPTION_CHAIN_TIMEOUT_S,
                retries=_OPTION_CHAIN_RETRIES,
                label=f"get_option_chain({underlying})",
            )
        except AlpacaClientError:
            raise  # already wrapped by _call_with_timeout
        except Exception as e:
            raise AlpacaClientError(f"get_option_chain {underlying}: {e}") from e

        out: list[ChainContract] = []
        for symbol, snap in (snap_map or {}).items():
            try:
                meta = parse_occ(symbol)
            except ValueError:
                continue
            q = getattr(snap, "latest_quote", None)
            t = getattr(snap, "latest_trade", None)
            g = getattr(snap, "greeks", None)
            iv = getattr(snap, "implied_volatility", None)
            if q is None or g is None or iv is None:
                continue  # incomplete row — skip
            bid = float(getattr(q, "bid_price", 0.0) or 0.0)
            ask = float(getattr(q, "ask_price", 0.0) or 0.0)
            last = float(getattr(t, "price", 0.0) or 0.0)
            delta = float(getattr(g, "delta", 0.0) or 0.0)
            out.append(ChainContract(
                contract_symbol=symbol, underlying=meta.underlying,
                expiration=meta.expiration, kind=meta.kind, strike=meta.strike,
                bid=bid, ask=ask, last=last, volume=int(getattr(t, "size", 0) or 0),
                open_interest=int(getattr(snap, "open_interest", 0) or 0),
                implied_volatility=float(iv), delta=delta,
            ))
        return out

    def sell_to_open(
        self, *, contract_symbol: str, qty: int, limit_price: Decimal,
    ) -> str:
        return self._submit(contract_symbol, qty, limit_price, AlpacaSide.SELL,
                            source="option_sell_to_open")

    def buy_to_close(
        self, *, contract_symbol: str, qty: int, limit_price: Decimal,
    ) -> str:
        return self._submit(contract_symbol, qty, limit_price, AlpacaSide.BUY,
                            source="option_buy_to_close")

    def _submit(
        self, contract_symbol: str, qty: int, limit_price: Decimal, side: AlpacaSide,
        *, source: str = "option",
    ) -> str:
        if qty <= 0:
            raise ValueError("qty must be positive integer")
        try:
            req = LimitOrderRequest(
                symbol=contract_symbol, qty=qty, side=side,
                time_in_force=TimeInForce.DAY, limit_price=float(limit_price),
                type=OrderType.LIMIT,
            )
            order = self._trading.submit_order(req)
            _audit_order_submitted(
                source=source, symbol=contract_symbol,
                side="buy" if side == AlpacaSide.BUY else "sell",
                qty=qty, asset_class="option",
                order_id=str(order.id), order_type="limit",
                limit_price=limit_price,
            )
            return str(order.id)
        except Exception as e:
            raise AlpacaClientError(f"option order {side} {contract_symbol}: {e}") from e

    def get_option_positions(self) -> list:
        try:
            return [p for p in self._trading.get_all_positions()
                    if str(p.asset_class).lower() == "us_option"]
        except Exception as e:
            raise AlpacaClientError(f"get_option_positions: {e}") from e

    def list_optionable_us_equities(self) -> set[str]:
        from alpaca.trading.requests import GetAssetsRequest
        try:
            assets = self._trading.get_all_assets(
                GetAssetsRequest(asset_class="us_equity", status="active"))
        except Exception as e:
            raise AlpacaClientError(f"list_optionable: {e}") from e
        out: set[str] = set()
        for a in assets:
            attrs = getattr(a, "attributes", None) or []
            has_options = "has_options" in attrs or getattr(a, "options_enabled", False)
            if has_options and a.tradable:
                out.add(str(a.symbol))
        return out

    def get_recent_option_orders(self, symbol_or_contract: str, lookback_days: int = 30):
        try:
            req = GetOrdersRequest(
                status="closed",
                after=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days),
                symbols=[symbol_or_contract],
            )
            return self._trading.get_orders(filter=req)
        except Exception as e:
            raise AlpacaClientError(f"get_recent_option_orders: {e}") from e

    def snapshot_for_contract(self, contract_symbol: str) -> ChainContract:
        """Single-contract snapshot. Returns a ChainContract via the chain endpoint
        filtered to this expiration."""
        meta = parse_occ(contract_symbol)
        chain = self.get_chain(meta.underlying,
                               expiration_gte=meta.expiration,
                               expiration_lte=meta.expiration)
        for c in chain:
            if c.contract_symbol == contract_symbol:
                return c
        raise AlpacaClientError(f"contract not in chain: {contract_symbol}")
