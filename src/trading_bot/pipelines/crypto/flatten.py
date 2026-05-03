"""Crypto bracket-close helper (Phase 1C).

Crypto orders on Alpaca are NON-ATOMIC: the entry and the protective
stop are two separate orders. Closing a position requires:

  1. Cancel the standalone stop order (if it exists and is still open)
  2. Submit a market sell for the held quantity
  3. Verify position == 0 within a small window; on residue, retry once

This module wraps that sequence behind a single
``CryptoFlattener.flatten_position(symbol)`` call so the hold debate's
``executor.flatten_position`` callback can stay simple.

``replace_stop`` follows the same broker-error fail-soft contract:
cancel old stop, place new stop, log on either step's failure but
never raise.

The actual broker calls are made through ``shared/alpaca_client``
(reused; the crypto branch already exists there). The flatten helper
adds the verify-and-retry layer that's missing from the raw client.

Both methods conform to ``hold_debate.HoldActionExecutor`` so callers
can pass them directly into ``run_hold_debate(executor=...)``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Broker-call protocol — passed in so we can mock for tests
# ---------------------------------------------------------------------------


class CryptoBrokerOps(Protocol):
    """Subset of AlpacaClient methods the flattener needs."""

    def cancel_order(self, order_id: str) -> None: ...
    def get_position_qty(self, symbol: str) -> float: ...
    def get_open_stop_order_id(self, symbol: str) -> Optional[str]: ...
    def submit_market_sell(self, *, symbol: str, qty: float) -> str: ...
    def submit_stop_order(self, *, symbol: str, qty: float, stop_price: float) -> str: ...


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass
class FlattenOutcome:
    success: bool
    cancelled_stop_id: Optional[str] = None
    market_order_id: Optional[str] = None
    final_qty: float = 0.0
    error: Optional[str] = None


@dataclass
class ReplaceStopOutcome:
    success: bool
    cancelled_stop_id: Optional[str] = None
    new_stop_order_id: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# CryptoFlattener
# ---------------------------------------------------------------------------


class CryptoFlattener:
    """Encapsulates the cancel-stop + market-sell + verify pattern.

    Construct with a ``CryptoBrokerOps`` instance (your AlpacaClient
    wrapper). Call ``flatten_position(symbol)`` or ``replace_stop(...)``.
    Both methods conform to the ``HoldActionExecutor`` protocol so they
    can be passed straight into ``hold_debate.run_hold_debate``.
    """

    def __init__(
        self,
        broker: CryptoBrokerOps,
        *,
        verify_window_seconds: float = 5.0,
        verify_poll_interval_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.broker = broker
        self.verify_window = verify_window_seconds
        self.poll_interval = verify_poll_interval_seconds
        self._sleep = sleeper

    # ----- HoldActionExecutor surface --------------------------------

    def flatten_position(self, *, symbol: str) -> None:
        """HoldActionExecutor entry point. Wraps :meth:`flatten` and
        re-raises only on hard error so the audit row records the
        action_taken correctly.
        """
        outcome = self.flatten(symbol)
        if not outcome.success:
            raise RuntimeError(outcome.error or "flatten_position failed")

    def replace_stop(self, *, symbol: str, new_stop_price: float) -> None:
        """HoldActionExecutor entry point. Cancels the existing stop and
        submits a fresh one at ``new_stop_price``."""
        outcome = self.replace_stop_detailed(symbol=symbol, new_stop_price=new_stop_price)
        if not outcome.success:
            raise RuntimeError(outcome.error or "replace_stop failed")

    # ----- Detailed implementations ----------------------------------

    def flatten(self, symbol: str) -> FlattenOutcome:
        """Cancel stop + market-sell + verify-with-retry.

        On any broker error we log + return a non-success outcome rather
        than re-raising — the hold debate audit row records the failure
        and the operator gets an alert via the role's normal channels.
        """
        cancelled_stop_id: Optional[str] = None
        try:
            stop_id = self.broker.get_open_stop_order_id(symbol)
            if stop_id:
                self.broker.cancel_order(stop_id)
                cancelled_stop_id = stop_id
        except Exception as e:  # noqa: BLE001 — proceed even if cancel fails
            logger.warning("flatten %s: cancel stop failed: %s", symbol, e)
            cancelled_stop_id = None  # we don't know what happened; market-sell anyway

        try:
            qty = float(self.broker.get_position_qty(symbol))
        except Exception as e:  # noqa: BLE001
            logger.exception("flatten %s: position read failed: %s", symbol, e)
            return FlattenOutcome(success=False, cancelled_stop_id=cancelled_stop_id,
                                  error=f"position_read_failed:{e}")

        if qty == 0:
            # Already flat — nothing to do; still a "success" from the caller's POV.
            return FlattenOutcome(success=True, cancelled_stop_id=cancelled_stop_id,
                                  final_qty=0.0)

        try:
            order_id = self.broker.submit_market_sell(symbol=symbol, qty=abs(qty))
        except Exception as e:  # noqa: BLE001
            logger.exception("flatten %s: market sell failed: %s", symbol, e)
            return FlattenOutcome(success=False, cancelled_stop_id=cancelled_stop_id,
                                  error=f"market_sell_failed:{e}")

        # Verify position drained within the window. If not, retry once.
        deadline = time.monotonic() + self.verify_window
        residue = self._poll_until_flat(symbol, deadline)
        if abs(residue) > 1e-9:
            logger.warning("flatten %s: residue %s after first market sell; retrying",
                           symbol, residue)
            try:
                self.broker.submit_market_sell(symbol=symbol, qty=abs(residue))
            except Exception as e:  # noqa: BLE001
                logger.exception("flatten %s: retry market sell failed: %s", symbol, e)
                return FlattenOutcome(success=False, cancelled_stop_id=cancelled_stop_id,
                                      market_order_id=order_id,
                                      final_qty=residue,
                                      error=f"retry_failed:{e}")
            deadline2 = time.monotonic() + self.verify_window
            residue = self._poll_until_flat(symbol, deadline2)

        return FlattenOutcome(
            success=abs(residue) < 1e-9,
            cancelled_stop_id=cancelled_stop_id,
            market_order_id=order_id,
            final_qty=residue,
            error=None if abs(residue) < 1e-9 else f"residue_remained:{residue}",
        )

    def replace_stop_detailed(
        self, *, symbol: str, new_stop_price: float,
    ) -> ReplaceStopOutcome:
        cancelled_id: Optional[str] = None
        try:
            stop_id = self.broker.get_open_stop_order_id(symbol)
            if stop_id:
                self.broker.cancel_order(stop_id)
                cancelled_id = stop_id
        except Exception as e:  # noqa: BLE001
            logger.warning("replace_stop %s: cancel old stop failed: %s", symbol, e)
            return ReplaceStopOutcome(success=False, cancelled_stop_id=cancelled_id,
                                       error=f"cancel_old_failed:{e}")

        try:
            qty = float(self.broker.get_position_qty(symbol))
        except Exception as e:  # noqa: BLE001
            logger.exception("replace_stop %s: position read failed: %s", symbol, e)
            return ReplaceStopOutcome(success=False, cancelled_stop_id=cancelled_id,
                                       error=f"position_read_failed:{e}")

        if qty == 0:
            # No position to protect — cancelled the stop and we're done.
            return ReplaceStopOutcome(success=True, cancelled_stop_id=cancelled_id)

        try:
            new_stop_id = self.broker.submit_stop_order(
                symbol=symbol, qty=abs(qty), stop_price=new_stop_price,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("replace_stop %s: new stop submit failed: %s", symbol, e)
            return ReplaceStopOutcome(success=False, cancelled_stop_id=cancelled_id,
                                       error=f"new_stop_failed:{e}")

        return ReplaceStopOutcome(success=True, cancelled_stop_id=cancelled_id,
                                   new_stop_order_id=new_stop_id)

    # ----- internals --------------------------------------------------

    def _poll_until_flat(self, symbol: str, deadline: float) -> float:
        last_qty = 0.0
        while time.monotonic() < deadline:
            try:
                last_qty = float(self.broker.get_position_qty(symbol))
            except Exception as e:  # noqa: BLE001
                logger.warning("flatten %s: position poll failed: %s", symbol, e)
                self._sleep(self.poll_interval)
                continue
            if abs(last_qty) < 1e-9:
                return 0.0
            self._sleep(self.poll_interval)
        return last_qty
