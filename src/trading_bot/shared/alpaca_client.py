# src/trading_bot/alpaca_client.py
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide as AlpacaSide, OrderStatus, OrderType, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from pydantic import BaseModel, model_validator

from trading_bot.shared.config import Settings
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled
from trading_bot.log_structured import StructuredLogger

PAPER_URL_PREFIX = "https://paper-api.alpaca.markets"

_audit_log = StructuredLogger(role="alpaca")

# Alpaca crypto symbols come in two forms: "BTC/USD" (slash) and "BTCUSD" (no slash).
# Market orders for crypto require IOC time_in_force (GTC/DAY are rejected by the venue).
_CRYPTO_QUOTE_CURRENCIES = ("USD", "USDT", "USDC", "EUR", "BTC")


def _is_crypto_symbol(symbol: str) -> bool:
    """Return True for both slash-form (BTC/USD) and no-slash (BTCUSD) crypto symbols."""
    sym = symbol.upper()
    for q in _CRYPTO_QUOTE_CURRENCIES:
        if sym.endswith("/" + q) or (sym.endswith(q) and len(sym) > len(q)):
            return True
    return False


def _audit_order_submitted(
    *, source: str, symbol: str, side: str, qty, asset_class: str,
    order_id: str, order_type: str, limit_price=None, stop_price=None,
) -> None:
    """Single audit event per order that successfully reached Alpaca.

    Emitted post-submit_order so we never log phantom orders that threw
    before reaching the venue. Powers the Phase 5 learning loop and the
    Phase 7 dashboard.
    """
    try:
        kwargs = dict(
            source=source, symbol=symbol, side=side, qty=str(qty),
            asset_class=asset_class, order_id=order_id, order_type=order_type,
        )
        if limit_price is not None:
            kwargs["limit_price"] = str(limit_price)
        if stop_price is not None:
            kwargs["stop_price"] = str(stop_price)
        _audit_log.event("order_submitted", **kwargs)
    except Exception:
        # Audit must never break the trade path.
        pass
    # Real-time bus emit (Phase 2). The Alpaca trade stream will emit
    # the matching ``order.placed`` once the venue confirms; this event
    # captures the *submit* side of the round-trip so the dashboard can
    # see in-flight orders even if the websocket is briefly down.
    try:
        from trading_bot.event_bus import bus as _bus
        _bus.emit(
            "order.submitted",
            {
                "source": source, "symbol": symbol, "side": side,
                "qty": str(qty), "asset_class": asset_class,
                "order_id": order_id, "order_type": order_type,
                "limit_price": str(limit_price) if limit_price is not None else None,
                "stop_price": str(stop_price) if stop_price is not None else None,
            },
            source="alpaca_client",
        )
    except Exception:
        pass

# Alpaca crypto rejects plain stop orders, so we use stop-limits. The limit
# is offset 5% past the trigger so the order has room to fill in fast moves
# while still capping how bad a fill we'll accept (vs. a market exit).
CRYPTO_STOP_LIMIT_BUFFER_PCT = 0.05


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    market_value: Decimal
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pl: Decimal
    asset_class: str


@dataclass(frozen=True)
class TradableAsset:
    symbol: str
    name: str
    exchange: str
    asset_class: str
    tradable: bool
    fractionable: bool


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"


class OrderRequest(BaseModel):
    symbol: str
    qty: Decimal
    side: OrderSide
    asset_class: AssetClass
    limit_price: Decimal
    stop_loss_price: Decimal | None

    @model_validator(mode="after")
    def require_stop_loss(self) -> "OrderRequest":
        if self.stop_loss_price is None:
            raise ValueError("stop_loss_price is required — every position must have a stop")
        return self


@dataclass(frozen=True)
class OrderResult:
    entry_order_id: str
    stop_loss_order_id: str


def _to_alpaca_side(s: OrderSide) -> AlpacaSide:
    return AlpacaSide.BUY if s == OrderSide.BUY else AlpacaSide.SELL


def _opposite(s: OrderSide) -> OrderSide:
    return OrderSide.SELL if s == OrderSide.BUY else OrderSide.BUY


def _to_orderable_symbol(symbol: str, asset_class: AssetClass) -> str:
    """Position-form → orderable-form symbol.

    Alpaca's REST surface returns crypto symbols differently between
    endpoints: get_all_positions → 'DOTUSD', orders/bars → 'DOT/USD'.
    All Alpaca crypto pairs settle in USD, so we insert the slash before
    the trailing 'USD'.
    """
    if asset_class != AssetClass.CRYPTO:
        return symbol
    if "/" in symbol:
        return symbol
    if symbol.endswith("USD"):
        return f"{symbol[:-3]}/USD"
    return symbol


class AlpacaClient:
    """Wrapper around alpaca-py TradingClient. Paper-only by construction."""

    def __init__(self, settings: Settings) -> None:
        if not settings.alpaca_base_url.startswith(PAPER_URL_PREFIX):
            raise LiveModeDisabled()
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        # Execution policy is optional. When set via set_execution_policy(),
        # both bracket-stock and crypto-entry paths consult the live top-of-
        # book before submitting: trades whose quoted spread exceeds
        # `_max_spread_pct` are refused, and limit prices are nudged past
        # the touch by `_marketable_buffer_pct` so they actually fill.
        # When unset, behaviour is unchanged.
        self._md: "Any | None" = None
        self._marketable_buffer_pct: float = 0.0
        self._max_spread_pct: float = 1.0  # effectively disabled

    def set_execution_policy(
        self, *, market_data: "Any",
        marketable_buffer_pct: float, max_spread_pct: float,
    ) -> None:
        self._md = market_data
        self._marketable_buffer_pct = float(marketable_buffer_pct)
        self._max_spread_pct = float(max_spread_pct)

    def _quote_or_refuse(self, symbol: str):
        """Returns the live Quote when the execution policy is active and
        the spread is within tolerance. Raises AlpacaClientError when the
        policy is active but the quote is missing or the spread is too
        wide. Returns None when no policy is set (caller falls through to
        legacy pricing)."""
        if self._md is None:
            return None
        quote = self._md.get_latest_quote(symbol)
        if quote is None:
            raise AlpacaClientError(
                f"no live quote for {symbol} — refusing to submit without "
                "spread/reprice gate (would fill blind)"
            )
        if quote.spread_pct > self._max_spread_pct:
            raise AlpacaClientError(
                f"quoted spread too wide for {symbol}: "
                f"{quote.spread_pct*100:.2f}% > {self._max_spread_pct*100:.2f}% "
                f"(bid={quote.bid} ask={quote.ask})"
            )
        return quote

    def _cancel_open_orders_for_symbol(self, symbol: str) -> None:
        """Cancel all open orders for *symbol* before placing a new entry.

        Prevents Alpaca's wash-trade rejection (code 40310000) when a stale
        order from a prior cycle is still on the books on the opposite side.
        Fails soft: logs each cancelled/failed order but never aborts the
        incoming trade.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orderable = _to_orderable_symbol(symbol, AssetClass.CRYPTO) if "/" not in symbol and symbol.endswith("USD") else symbol
        symbols_to_match = {symbol, orderable}
        try:
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
            )
        except Exception as e:
            _audit_log.event("pre_entry_cancel_skipped", symbol=symbol, error=str(e))
            return
        for o in open_orders:
            if str(o.symbol) not in symbols_to_match:
                continue
            try:
                self._client.cancel_order_by_id(str(o.id))
                _audit_log.event(
                    "pre_entry_cancel_ok", symbol=symbol,
                    cancelled_order_id=str(o.id),
                    order_type=str(getattr(o, "type", "")),
                    side=str(getattr(o, "side", "")),
                )
            except Exception as ce:
                _audit_log.event(
                    "pre_entry_cancel_failed", symbol=symbol,
                    order_id=str(o.id), error=str(ce),
                )

    def get_account(self) -> AccountSnapshot:
        try:
            a = self._client.get_account()
        except Exception as e:
            raise AlpacaClientError(f"get_account failed: {e}") from e
        return AccountSnapshot(
            equity=Decimal(str(a.equity)),
            cash=Decimal(str(a.cash)),
            buying_power=Decimal(str(a.buying_power)),
            portfolio_value=Decimal(str(a.portfolio_value)),
        )

    def get_positions(self) -> list[Position]:
        try:
            raw = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"get_all_positions failed: {e}") from e
        return [
            Position(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                market_value=Decimal(str(p.market_value)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                current_price=Decimal(str(p.current_price)),
                unrealized_pl=Decimal(str(p.unrealized_pl)),
                asset_class=str(p.asset_class),
            )
            for p in raw
        ]

    def get_open_order_symbols(self) -> set[str]:
        """Symbols with non-terminal open orders (used to prevent duplicate entries)."""
        try:
            orders = self._client.get_orders()
        except Exception as e:
            raise AlpacaClientError(f"get_orders failed: {e}") from e
        return {str(o.symbol) for o in orders}

    def get_order_bracket_levels(self) -> dict[str, tuple[float | None, float | None]]:
        """Return {symbol: (stop_price, take_profit_price)} from open bracket legs.

        Makes one get_orders call and extracts stop and limit prices per symbol.
        For an active long position: the open SELL STOP is the stop-loss, the open
        SELL LIMIT is the take-profit leg (entry limit already filled and closed).
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        try:
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
            )
        except Exception as e:
            raise AlpacaClientError(f"get_order_bracket_levels failed: {e}") from e

        stops: dict[str, float] = {}
        take_profits: dict[str, float] = {}
        for o in open_orders:
            sym = str(getattr(o, "symbol", "") or "")
            order_type = str(getattr(o, "type", "")).lower()
            if "stop" in order_type:
                sp = getattr(o, "stop_price", None)
                if sp is not None:
                    try:
                        stops[sym] = float(sp)
                    except (TypeError, ValueError):
                        pass
            elif "limit" in order_type:
                lp = getattr(o, "limit_price", None)
                if lp is not None:
                    try:
                        take_profits[sym] = float(lp)
                    except (TypeError, ValueError):
                        pass

        result: dict[str, tuple[float | None, float | None]] = {}
        for sym in set(list(stops) + list(take_profits)):
            pair = (stops.get(sym), take_profits.get(sym))
            result[sym] = pair
            # Crypto orders use "BTC/USD" but positions API returns "BTCUSD".
            # Store both so callers can look up by either form.
            if "/" in sym:
                no_slash = sym.replace("/", "")
                result[no_slash] = pair
            elif sym.endswith("USD") and len(sym) > 4:
                with_slash = f"{sym[:-3]}/USD"
                result[with_slash] = pair
        return result

    def get_active_assets(self, asset_class: str) -> list[TradableAsset]:
        """List all active+tradable assets for the given asset_class.

        asset_class: "us_equity" or "crypto"
        """
        from alpaca.trading.requests import GetAssetsRequest
        try:
            raw = self._client.get_all_assets(
                GetAssetsRequest(asset_class=asset_class, status="active")
            )
        except Exception as e:
            raise AlpacaClientError(f"get_all_assets failed: {e}") from e
        return [
            TradableAsset(
                symbol=str(a.symbol),
                name=str(a.name or ""),
                exchange=str(a.exchange or ""),
                asset_class=str(a.asset_class),
                tradable=bool(a.tradable),
                fractionable=bool(getattr(a, "fractionable", False)),
            )
            for a in raw
            if a.tradable
        ]

    def place_market_order(
        self, *, symbol: str, qty: float, side: OrderSide, asset_class: AssetClass
    ) -> str:
        """Place a plain market order without a stop-loss leg.

        Used by Hold-SPY Coordinator (Phase 4) for the 5-day exit/reverse
        transitions, where positions are passively wound down/up over days
        rather than risk-managed by per-position stops. Returns the Alpaca
        order id.
        """
        try:
            mkt_req = MarketOrderRequest(
                symbol=_to_orderable_symbol(symbol, asset_class),
                qty=float(qty),
                side=_to_alpaca_side(side),
                time_in_force=TimeInForce.GTC if asset_class == AssetClass.CRYPTO else TimeInForce.DAY,
            )
            order = self._client.submit_order(mkt_req)
            _audit_order_submitted(
                source="market", symbol=symbol, side=side.value, qty=qty,
                asset_class=asset_class.value, order_id=str(order.id),
                order_type="market",
            )
            return str(order.id)
        except Exception as e:
            raise AlpacaClientError(f"market order failed for {symbol}: {e}") from e

    def place_protective_stop(
        self,
        *,
        symbol: str,
        qty: Decimal,
        position_side: OrderSide,
        asset_class: AssetClass,
        stop_price: Decimal,
    ) -> str:
        """Place a standalone protective stop on an existing position.

        `position_side` is the side of the position being protected (BUY=long,
        SELL=short). The stop order takes the opposite side. Stocks use plain
        stop; crypto uses stop-limit (Alpaca rejects plain stops on crypto).
        Returns the Alpaca order id.
        """
        stop_side = _opposite(position_side)
        orderable_symbol = _to_orderable_symbol(symbol, asset_class)
        try:
            if asset_class == AssetClass.CRYPTO:
                if stop_side == OrderSide.SELL:
                    limit = float(stop_price) * (1.0 - CRYPTO_STOP_LIMIT_BUFFER_PCT)
                else:
                    limit = float(stop_price) * (1.0 + CRYPTO_STOP_LIMIT_BUFFER_PCT)
                req = StopLimitOrderRequest(
                    symbol=orderable_symbol,
                    qty=float(qty),
                    side=_to_alpaca_side(stop_side),
                    time_in_force=TimeInForce.GTC,
                    stop_price=float(stop_price),
                    limit_price=round(limit, 6),
                )
            else:
                req = StopOrderRequest(
                    symbol=orderable_symbol,
                    qty=float(qty),
                    side=_to_alpaca_side(stop_side),
                    time_in_force=TimeInForce.GTC,
                    stop_price=float(stop_price),
                )
            order = self._client.submit_order(req)
            _audit_order_submitted(
                source="protective_stop", symbol=symbol, side=stop_side.value,
                qty=qty, asset_class=asset_class.value, order_id=str(order.id),
                order_type="stop_limit" if asset_class == AssetClass.CRYPTO else "stop",
                stop_price=stop_price,
            )
            return str(order.id)
        except Exception as e:
            raise AlpacaClientError(
                f"protective stop failed for {symbol}: {e}"
            ) from e

    def place_order_with_stop_loss(self, req: OrderRequest) -> OrderResult:
        """Place atomic bracket order: entry + stop-loss together.

        Crypto doesn't support bracket orders on Alpaca, so for crypto we
        fall back to a market entry then a stop-loss as a separate order.
        """
        if req.asset_class == AssetClass.CRYPTO:
            return self._place_crypto_with_stop(req)
        return self._place_stock_bracket(req)

    def _place_stock_bracket(self, req: OrderRequest) -> OrderResult:
        self._cancel_open_orders_for_symbol(req.symbol)
        # Marketable-limit + spread gate. The signal's entry_price often
        # leaves the bracket limit unfilled once price has drifted by
        # submission time. With the execution policy wired, reprice the
        # entry to ask × (1 + buffer) for BUY (bid × (1 - buffer) for SELL)
        # so the order is marketable; the SL is left at the strategy's
        # absolute stop level (independent of fill price), and the TP is
        # rebuilt from the new entry to preserve the 2:1 reward:risk shape.
        # When the policy is unset, behaviour is identical to before.
        limit_price = float(req.limit_price)
        quote = self._quote_or_refuse(req.symbol)
        if quote is not None:
            if req.side == OrderSide.BUY:
                limit_price = round(quote.ask * (1.0 + self._marketable_buffer_pct), 2)
            else:
                limit_price = round(quote.bid * (1.0 - self._marketable_buffer_pct), 2)
        # Alpaca bracket orders require a take-profit leg. Use 2:1 reward:risk.
        risk = abs(limit_price - float(req.stop_loss_price))
        if req.side == OrderSide.BUY:
            take_profit_price = limit_price + 2 * risk
        else:
            take_profit_price = limit_price - 2 * risk
        try:
            entry_req = LimitOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(req.side),
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=float(req.stop_loss_price)),
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            )
            entry = self._client.submit_order(entry_req)
            _audit_order_submitted(
                source="bracket_entry", symbol=req.symbol, side=req.side.value,
                qty=req.qty, asset_class=req.asset_class.value,
                order_id=str(entry.id), order_type="limit_bracket",
                limit_price=req.limit_price, stop_price=req.stop_loss_price,
            )
        except Exception as e:
            raise AlpacaClientError(f"bracket order failed: {e}") from e

        # Bracket orders return the parent (entry) order with `legs` containing
        # the stop-loss + take-profit legs. Find the stop leg.
        stop_id = ""
        legs = getattr(entry, "legs", None) or []
        for leg in legs:
            if str(getattr(leg, "type", "")).lower().endswith("stop"):
                stop_id = str(leg.id)
                break
        if not stop_id and legs:
            stop_id = str(legs[0].id)
        return OrderResult(entry_order_id=str(entry.id), stop_loss_order_id=stop_id)

    def _place_crypto_with_stop(self, req: OrderRequest) -> OrderResult:
        """Crypto orders are placed in two non-atomic legs:
        0) cancel any stale open orders for this symbol (prevents wash-trade
           rejection on the stop-limit leg)
            1) market entry
            2) standalone stop-limit order

        Risk #4 from the trader's analysis: between (1) and (2), the price
        can flash-crash leaving the position unprotected. Mitigation here:
        after both legs are placed, do a post-fill verification — re-query
        positions + open orders. If the stop didn't actually land:
          - if the entry has filled → market-flatten the unprotected position
          - if the entry is still pending → cancel the entry so it can't
            fill later without a stop
        Either way we never hold an unprotected crypto position past the
        verification window.
        """
        self._cancel_open_orders_for_symbol(req.symbol)
        orderable_symbol = _to_orderable_symbol(req.symbol, req.asset_class)
        # Marketable-limit + spread gate. The previous true-market path on
        # crypto could eat the entire visible book on illiquid alts. With
        # the execution policy wired, we submit a LimitOrderRequest priced
        # past the touch by `_marketable_buffer_pct` so we still get an
        # essentially-immediate fill but with a hard slippage cap; trades
        # where the quoted spread is too wide are refused entirely. When
        # the policy is unset, behaviour falls back to the legacy market
        # order so unit tests + tooling that bypass set_execution_policy
        # keep working.
        try:
            quote = self._quote_or_refuse(orderable_symbol)
        except AlpacaClientError:
            # Re-raise so the caller (orchestrator) records this as a
            # clean `api_error` decision rather than a generic broker fault.
            raise
        try:
            if quote is not None:
                if req.side == OrderSide.BUY:
                    limit_price = round(quote.ask * (1.0 + self._marketable_buffer_pct), 6)
                else:
                    limit_price = round(quote.bid * (1.0 - self._marketable_buffer_pct), 6)
                entry_req = LimitOrderRequest(
                    symbol=orderable_symbol,
                    qty=float(req.qty),
                    side=_to_alpaca_side(req.side),
                    time_in_force=TimeInForce.IOC,
                    limit_price=limit_price,
                )
                entry = self._client.submit_order(entry_req)
                _audit_order_submitted(
                    source="crypto_entry", symbol=req.symbol, side=req.side.value,
                    qty=req.qty, asset_class=req.asset_class.value,
                    order_id=str(entry.id), order_type="marketable_limit_ioc",
                    limit_price=limit_price,
                )
            else:
                entry_req = MarketOrderRequest(
                    symbol=orderable_symbol,
                    qty=float(req.qty),
                    side=_to_alpaca_side(req.side),
                    time_in_force=TimeInForce.GTC,
                )
                entry = self._client.submit_order(entry_req)
                _audit_order_submitted(
                    source="crypto_entry", symbol=req.symbol, side=req.side.value,
                    qty=req.qty, asset_class=req.asset_class.value,
                    order_id=str(entry.id), order_type="market",
                )
        except Exception as e:
            raise AlpacaClientError(f"crypto entry order failed: {e}") from e

        # Crypto market orders frequently fill at slightly LESS than the
        # requested qty (rounding to the venue's increment). The stop-limit
        # leg must use the ACTUAL filled qty — using req.qty would request
        # more than the available balance and the stop would fail with
        # "insufficient balance", leaving the position unprotected. Re-query the
        # entry order to get filled_qty, briefly polling so the fill state
        # has time to propagate.
        # Wait for the entry to reach "filled" status before placing the stop.
        # Placing the stop while the entry is still open (pending/partial) triggers
        # Alpaca's wash-trade check (code 40310000) because the open BUY entry
        # and the SELL stop are on opposite sides of the same symbol.
        # If the entry never fills within the window, cancel it and abort — an
        # unfilled entry must not linger on the books without a paired stop.
        actual_qty = float(req.qty)
        import time as _t
        entry_filled = False
        try:
            for _ in range(40):  # up to 20 seconds
                refreshed = self._client.get_order_by_id(entry.id)
                fq = float(getattr(refreshed, "filled_qty", 0) or 0)
                if getattr(refreshed, "status", None) == OrderStatus.FILLED:
                    actual_qty = fq or float(req.qty)
                    entry_filled = True
                    break
                _t.sleep(0.5)
        except Exception:
            pass

        if not entry_filled:
            # Entry didn't fill in time. Cancel it so it can't fill later
            # without a stop, then surface a clean error.
            cancel_msg = ""
            try:
                self._client.cancel_order_by_id(str(entry.id))
                cancel_msg = "pending entry cancelled (never filled within 20 s)"
            except Exception as ce:
                cancel_msg = f"entry cancel FAILED: {ce} — manually cancel {entry.id}"
            raise AlpacaClientError(
                f"crypto entry order did not fill within 20 s; {cancel_msg}"
            )

        # After confirming the fill via order-status, wait for the position
        # to appear in get_all_positions() before placing the stop-limit.
        # Alpaca's internal order book settles asynchronously — placing a
        # SELL stop while the just-filled BUY is still "active" triggers
        # wash-trade rejection (code 40310000). Polling the positions ledger
        # is more reliable than a fixed sleep because it confirms the state
        # transition the stop placement depends on.
        _pos_confirmed = False
        _sym_noSlash = orderable_symbol.replace("/", "")
        for _ in range(20):  # up to 10 seconds
            try:
                _pos_list = self._client.get_all_positions()
                if any(
                    str(p.symbol).replace("/", "") == _sym_noSlash
                    for p in _pos_list
                ):
                    _pos_confirmed = True
                    break
            except Exception:
                pass
            _t.sleep(0.5)
        if not _pos_confirmed:
            # Position not yet visible — add a final safety sleep so the
            # stop doesn't fire into a still-settling order book.
            _t.sleep(1.0)

        stop_side = _opposite(req.side)
        stop_trigger = float(req.stop_loss_price)
        # Sell-stop: limit must be ≤ trigger so the order is valid below the
        # trigger price. Buy-stop (covering a short): limit must be ≥ trigger.
        if stop_side == OrderSide.SELL:
            stop_limit = stop_trigger * (1.0 - CRYPTO_STOP_LIMIT_BUFFER_PCT)
        else:
            stop_limit = stop_trigger * (1.0 + CRYPTO_STOP_LIMIT_BUFFER_PCT)

        stop = None
        stop_error: Exception | None = None
        try:
            stop_req = StopLimitOrderRequest(
                symbol=orderable_symbol,
                qty=actual_qty,
                side=_to_alpaca_side(stop_side),
                time_in_force=TimeInForce.GTC,
                stop_price=stop_trigger,
                limit_price=round(stop_limit, 6),
            )
            stop = self._client.submit_order(stop_req)
            _audit_order_submitted(
                source="crypto_stop", symbol=req.symbol, side=stop_side.value,
                qty=actual_qty, asset_class=req.asset_class.value,
                order_id=str(stop.id), order_type="stop_limit",
                stop_price=stop_trigger,
            )
        except Exception as e:
            stop_error = e

        # Post-fill verification. Sleep briefly to let Alpaca propagate the
        # fill state, then check that BOTH the position and a live stop
        # exist. If not, flatten via market order and surface an error.
        import time as _time
        _time.sleep(0.5)
        try:
            verify_action = self._verify_crypto_stop_or_flatten(
                req, entry_id=str(entry.id)
            )
        except AlpacaClientError:
            raise
        except Exception as e:
            raise AlpacaClientError(
                f"crypto post-fill verify failed: {e} "
                f"(entry={entry.id}); position state uncertain — manual review"
            ) from e

        if stop_error is not None:
            # Stop submission threw. If the entry hasn't filled yet, cancel
            # it so it can't fill later unprotected. If it had filled, the
            # verifier would have raised already (flatten path).
            if verify_action == "no_position":
                try:
                    self._client.cancel_order_by_id(str(entry.id))
                    recovery_msg = "pending entry has been cancelled"
                except Exception as ce:
                    recovery_msg = (
                        f"pending entry cancel FAILED: {ce} — "
                        f"manually cancel entry {entry.id} in Alpaca UI"
                    )
            else:
                # verify_action == "has_stop": a stop is on the books even
                # though our submission threw. Likely a transient network
                # error after the request was accepted. Position is protected.
                recovery_msg = "a stop order is already live for this symbol"
            raise AlpacaClientError(
                f"crypto stop-loss order failed: {stop_error}; {recovery_msg}."
            ) from stop_error

        return OrderResult(entry_order_id=str(entry.id), stop_loss_order_id=str(stop.id))

    def _verify_crypto_stop_or_flatten(
        self, req: OrderRequest, *, entry_id: str
    ) -> str:
        """Verify the just-placed crypto position is protected by a live stop.

        Returns:
            "no_position" — the entry hasn't filled (or already closed).
                The caller must decide whether to cancel the pending entry
                (e.g. when the stop submission also failed).
            "has_stop"    — the position is protected by a live stop or
                stop-limit order. Caller can proceed.

        Raises AlpacaClientError if the position is unprotected. In the
        unprotected case, this method market-flattens the position; the raise is the
        signal back to the caller that something went wrong AND the
        recovery has already been performed.
        """
        try:
            raw_positions = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"verify failed (get_positions): {e}") from e

        _req_sym_norm = req.symbol.replace("/", "")
        pos = next(
            (p for p in raw_positions if str(p.symbol).replace("/", "") == _req_sym_norm),
            None,
        )
        if pos is None:
            return "no_position"

        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            )
        except Exception as e:
            raise AlpacaClientError(f"verify failed (get_orders): {e}") from e

        # Recognise both `stop` and `stop_limit` order types as protective.
        # Alpaca surfaces them as enum reprs like "OrderType.STOP" or
        # "OrderType.STOP_LIMIT", which lower-case to "...stop" / "...stop_limit".
        has_stop = any(
            str(o.symbol) == req.symbol
            and str(getattr(o, "type", "")).lower().endswith(("stop", "stop_limit"))
            for o in open_orders
        )
        if has_stop:
            return "has_stop"

        # Unprotected position detected. Market-flatten.
        try:
            flatten_req = MarketOrderRequest(
                symbol=_to_orderable_symbol(req.symbol, req.asset_class),
                qty=float(getattr(pos, "qty", req.qty)),
                side=_to_alpaca_side(_opposite(req.side)),
                time_in_force=TimeInForce.GTC,
            )
            flatten_order = self._client.submit_order(flatten_req)
            _audit_order_submitted(
                source="crypto_flatten_emergency", symbol=req.symbol,
                side=_opposite(req.side).value,
                qty=getattr(pos, "qty", req.qty),
                asset_class=req.asset_class.value,
                order_id=str(flatten_order.id), order_type="market",
            )
        except Exception as e:
            raise AlpacaClientError(
                f"UNPROTECTED CRYPTO POSITION ({req.symbol}, entry={entry_id}) "
                f"AND FLATTEN FAILED: {e} — manually flatten in Alpaca UI immediately"
            ) from e
        raise AlpacaClientError(
            f"crypto post-fill: stop missing on {req.symbol} (entry={entry_id}); "
            f"position has been market-flattened."
        )

    # ---------------------------------------------------------------------
    # Phase C — hold-debate action helpers
    # ---------------------------------------------------------------------

    def replace_stop(self, *, symbol: str, new_stop_price: float) -> "ReplaceStopResult":
        """Cancel the existing protective stop for ``symbol`` and submit
        a new stop-loss at ``new_stop_price``. Used by the hold-debate
        ``tighten_stop`` verdict to defend gains without exiting.

        Returns a result with the cancelled and new order IDs. Raises
        ``AlpacaClientError`` on:
          - position not found
          - no open stop order for the symbol (caller should consider
            ``flatten_position`` instead since there's no protection to
            tighten)
          - submit / cancel failure
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        # Find the position so we know the qty for the new stop
        try:
            raw_positions = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"replace_stop get_positions failed: {e}") from e
        pos = next((p for p in raw_positions if str(p.symbol) == symbol), None)
        if pos is None:
            raise AlpacaClientError(
                f"replace_stop: no open position for {symbol}"
            )
        pos_qty = abs(float(getattr(pos, "qty", 0) or 0))
        pos_side = (
            OrderSide.SELL if float(getattr(pos, "qty", 0)) > 0 else OrderSide.BUY
        )  # the SIDE of the protective stop is opposite the position side

        # Find the open stop order(s) for this symbol
        try:
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
            )
        except Exception as e:
            raise AlpacaClientError(f"replace_stop get_orders failed: {e}") from e
        stop_orders = [
            o for o in open_orders
            if str(o.symbol) == symbol
            and str(getattr(o, "type", "")).lower().endswith(("stop", "stop_limit"))
        ]
        if not stop_orders:
            raise AlpacaClientError(
                f"replace_stop: no open stop order for {symbol} — consider flatten_position"
            )

        # Cancel ALL of them (defensive — usually exactly one)
        cancelled_ids: list[str] = []
        for o in stop_orders:
            try:
                self._client.cancel_order_by_id(str(o.id))
                cancelled_ids.append(str(o.id))
            except Exception as e:
                raise AlpacaClientError(
                    f"replace_stop cancel failed for {o.id}: {e}"
                ) from e

        # Submit the new stop-loss order
        try:
            new_stop_req = StopOrderRequest(
                symbol=symbol,
                qty=pos_qty,
                side=_to_alpaca_side(pos_side),
                time_in_force=TimeInForce.GTC,
                stop_price=float(new_stop_price),
            )
            new_stop = self._client.submit_order(new_stop_req)
            _audit_order_submitted(
                source="hold_debate_replace_stop", symbol=symbol,
                side=pos_side.value, qty=pos_qty, asset_class="stock",
                order_id=str(new_stop.id), order_type="stop",
                stop_price=new_stop_price,
            )
        except Exception as e:
            raise AlpacaClientError(
                f"replace_stop submit failed for {symbol} "
                f"(cancelled prior stops {cancelled_ids}): {e}"
            ) from e

        return ReplaceStopResult(
            symbol=symbol,
            cancelled_order_ids=cancelled_ids,
            new_stop_order_id=str(new_stop.id),
            new_stop_price=float(new_stop_price),
        )

    def flatten_position(self, *, symbol: str) -> "FlattenResult":
        """Cancel all open child orders for ``symbol`` (stop + take-profit)
        and submit a market order in the opposite direction to close the
        position. Used by the hold-debate ``exit_now`` verdict.

        Returns a result with the flatten order ID and cancelled child IDs.
        Raises ``AlpacaClientError`` on:
          - position not found (nothing to flatten)
          - flatten submit failure
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        # Get the position
        try:
            raw_positions = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"flatten_position get_positions failed: {e}") from e
        pos = next((p for p in raw_positions if str(p.symbol) == symbol), None)
        if pos is None:
            raise AlpacaClientError(
                f"flatten_position: no open position for {symbol}"
            )
        pos_qty = abs(float(getattr(pos, "qty", 0) or 0))
        is_long = float(getattr(pos, "qty", 0)) > 0
        flatten_side = OrderSide.SELL if is_long else OrderSide.BUY

        # Cancel all open orders for this symbol (stop, TP, anything else)
        # so the flatten doesn't race against a stop or TP.
        cancelled_ids: list[str] = []
        try:
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
            )
        except Exception as e:
            raise AlpacaClientError(
                f"flatten_position get_orders failed: {e}"
            ) from e
        for o in open_orders:
            if str(o.symbol) != symbol:
                continue
            try:
                self._client.cancel_order_by_id(str(o.id))
                cancelled_ids.append(str(o.id))
            except Exception:
                # Best-effort — continue trying to flatten even if a
                # specific cancel failed (the flatten itself is the
                # critical part).
                pass

        # Submit the flatten order
        try:
            asset_class_str = "crypto" if _is_crypto_symbol(symbol) else "stock"
            # Alpaca crypto market orders require IOC; equity uses DAY.
            tif = TimeInForce.IOC if asset_class_str == "crypto" else TimeInForce.DAY
            flatten_req = MarketOrderRequest(
                symbol=symbol,
                qty=pos_qty,
                side=_to_alpaca_side(flatten_side),
                time_in_force=tif,
            )
            flatten_order = self._client.submit_order(flatten_req)
            _audit_order_submitted(
                source="hold_debate_flatten", symbol=symbol,
                side=flatten_side.value, qty=pos_qty,
                asset_class=asset_class_str,
                order_id=str(flatten_order.id), order_type="market",
            )
        except Exception as e:
            raise AlpacaClientError(
                f"flatten_position submit failed for {symbol}: {e}"
            ) from e

        return FlattenResult(
            symbol=symbol,
            flatten_order_id=str(flatten_order.id),
            cancelled_child_order_ids=cancelled_ids,
            qty=pos_qty,
        )


@dataclass(frozen=True)
class ReplaceStopResult:
    symbol: str
    cancelled_order_ids: list[str]
    new_stop_order_id: str
    new_stop_price: float


@dataclass(frozen=True)
class FlattenResult:
    symbol: str
    flatten_order_id: str
    cancelled_child_order_ids: list[str]
    qty: float
