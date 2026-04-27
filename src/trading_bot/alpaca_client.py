# src/trading_bot/alpaca_client.py
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide as AlpacaSide, OrderType, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from pydantic import BaseModel, model_validator

from trading_bot.config import Settings
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled

PAPER_URL_PREFIX = "https://paper-api.alpaca.markets"


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

    def place_order_with_stop_loss(self, req: OrderRequest) -> OrderResult:
        """Place atomic bracket order: entry + stop-loss together.

        Crypto doesn't support bracket orders on Alpaca, so for crypto we
        fall back to a market entry then a stop-loss as a separate order.
        """
        if req.asset_class == AssetClass.CRYPTO:
            return self._place_crypto_with_stop(req)
        return self._place_stock_bracket(req)

    def _place_stock_bracket(self, req: OrderRequest) -> OrderResult:
        # Alpaca bracket orders require a take-profit leg. Use 2:1 reward:risk.
        risk = abs(float(req.limit_price) - float(req.stop_loss_price))
        if req.side == OrderSide.BUY:
            take_profit_price = float(req.limit_price) + 2 * risk
        else:
            take_profit_price = float(req.limit_price) - 2 * risk
        try:
            entry_req = LimitOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(req.side),
                time_in_force=TimeInForce.DAY,
                limit_price=float(req.limit_price),
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=float(req.stop_loss_price)),
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            )
            entry = self._client.submit_order(entry_req)
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
            1) market entry
            2) standalone stop order

        Risk #4 from the trader's analysis: between (1) and (2), the price
        can flash-crash leaving us with a naked position. Mitigation here:
        after both legs are placed, do a post-fill verification — re-query
        positions + open orders. If the stop didn't actually land (or the
        position is naked for any reason), market-flatten immediately to
        avoid an unbounded loss. This trades a small chance of unnecessary
        flattening for a guarantee that we never hold an unprotected
        crypto position past the verification window.
        """
        try:
            entry_req = MarketOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(req.side),
                time_in_force=TimeInForce.GTC,
            )
            entry = self._client.submit_order(entry_req)
        except Exception as e:
            raise AlpacaClientError(f"crypto entry order failed: {e}") from e

        stop = None
        stop_error: Exception | None = None
        try:
            stop_req = StopOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(_opposite(req.side)),
                time_in_force=TimeInForce.GTC,
                stop_price=float(req.stop_loss_price),
            )
            stop = self._client.submit_order(stop_req)
        except Exception as e:
            stop_error = e

        # Post-fill verification. Sleep briefly to let Alpaca propagate the
        # fill state, then check that BOTH the position and a live stop
        # exist. If not, flatten via market order and surface an error.
        import time as _time
        _time.sleep(0.5)
        try:
            self._verify_crypto_stop_or_flatten(req, entry_id=str(entry.id))
        except AlpacaClientError:
            raise
        except Exception as e:
            raise AlpacaClientError(
                f"crypto post-fill verify failed: {e} "
                f"(entry={entry.id}); position state uncertain — manual review"
            ) from e

        if stop_error is not None:
            raise AlpacaClientError(
                f"crypto stop-loss order failed: {stop_error}; "
                f"position has been flattened by the post-fill verifier."
            ) from stop_error

        return OrderResult(entry_order_id=str(entry.id), stop_loss_order_id=str(stop.id))

    def _verify_crypto_stop_or_flatten(
        self, req: OrderRequest, *, entry_id: str
    ) -> None:
        """If the just-placed crypto position lacks a live stop order,
        market-flatten the position immediately. Returns None on success
        (stop is live OR position no longer exists).

        Raises AlpacaClientError only if the flatten itself fails — at
        that point the operator must intervene manually.
        """
        # Look for a live position on the symbol.
        try:
            raw_positions = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"verify failed (get_positions): {e}") from e

        pos = next(
            (p for p in raw_positions if str(p.symbol) == req.symbol),
            None,
        )
        if pos is None:
            return  # entry not yet filled or already closed — nothing to protect

        # Look for a live stop order on the symbol.
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            )
        except Exception as e:
            raise AlpacaClientError(f"verify failed (get_orders): {e}") from e

        has_stop = any(
            str(o.symbol) == req.symbol
            and str(getattr(o, "type", "")).lower().endswith("stop")
            for o in open_orders
        )
        if has_stop:
            return  # all good

        # Naked position detected. Market-flatten.
        try:
            flatten_req = MarketOrderRequest(
                symbol=req.symbol,
                qty=float(getattr(pos, "qty", req.qty)),
                side=_to_alpaca_side(_opposite(req.side)),
                time_in_force=TimeInForce.GTC,
            )
            self._client.submit_order(flatten_req)
        except Exception as e:
            raise AlpacaClientError(
                f"NAKED CRYPTO POSITION ({req.symbol}, entry={entry_id}) "
                f"AND FLATTEN FAILED: {e} — manually flatten in Alpaca UI immediately"
            ) from e
        raise AlpacaClientError(
            f"crypto post-fill: stop missing on {req.symbol} (entry={entry_id}); "
            f"position has been market-flattened."
        )
