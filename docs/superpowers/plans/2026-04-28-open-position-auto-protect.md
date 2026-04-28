# Open Position Auto-Protect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the alert-only `verify-stops` sweep with auto-action — place a protective stop or market-flatten the position, depending on health — and rename the email subject to "Open Positions".

**Architecture:** A new pure-ish module `position_protection.py` decides protect-vs-flatten per unprotected position, calls a new `AlpacaClient.place_protective_stop` (plain `StopOrderRequest` for stocks, `StopLimitOrderRequest` for crypto) or the existing `place_market_order`, and returns a list of action records. `cli.py:verify_stops` shrinks to glue: pull positions/orders → filter unprotected → call the module → render summary email via a new `build_open_positions_email_html`. The old `build_naked_stops_email_html` is deleted along with its tests.

**Tech Stack:** Python 3.9, alpaca-py SDK, pydantic, pytest with `unittest.mock` (fixture pattern from `tests/test_alpaca_client.py`).

**Spec:** [`docs/superpowers/specs/2026-04-28-open-position-auto-protect-design.md`](../specs/2026-04-28-open-position-auto-protect-design.md)

**File map:**

| File | Change |
|---|---|
| `strategy/config.yaml` | Add `risk.unprotected_stop_pct: 0.05` |
| `src/trading_bot/config.py` | Add `unprotected_stop_pct` field on `RiskConfig` |
| `src/trading_bot/alpaca_client.py` | Add `place_protective_stop` method + `_orderable_symbol` helper |
| `src/trading_bot/position_protection.py` | NEW — `_decide`, `ProtectionAction`, `evaluate_and_act` |
| `src/trading_bot/reports.py` | Add `build_open_positions_email_html`; remove `build_naked_stops_email_html` and update module docstring/`__all__` list |
| `src/trading_bot/cli.py` | Rewrite `verify_stops` to delegate to `position_protection` and use the new email builder |
| `src/trading_bot/dashboard/templates/architecture.html` | Update prose: "naked-position alert" → "open-position protector"; "if any are naked" → wording reflecting auto-action |
| `tests/test_position_protection.py` | NEW — covers `_decide` and `evaluate_and_act` |
| `tests/test_reports.py` | Replace naked-stops tests with open-positions tests |
| `tests/test_cli.py` | Update any `verify_stops` tests to reflect new behavior (if present) |
| `tests/test_alpaca_client.py` | Add tests for `place_protective_stop` |
| `tests/test_config.py` | Add test that `unprotected_stop_pct` loads with default |

---

## Task 1: Add `unprotected_stop_pct` config field

**Files:**
- Modify: `src/trading_bot/config.py:36-42` (RiskConfig)
- Modify: `strategy/config.yaml:3-9` (risk block)
- Test: `tests/test_config.py`

- [ ] **Step 1: Read existing config test patterns**

Run: `grep -n "RiskConfig\|risk_config\|daily_loss" /Users/bharathkandala/Trading/tests/test_config.py | head -10`

Read the matching test(s) to understand the loader pattern. The new test must follow whatever pattern is already in use (likely `load_config(path)` then assert on the parsed object).

- [ ] **Step 2: Write failing test for default value**

Add to `tests/test_config.py`:

```python
def test_risk_config_unprotected_stop_pct_default():
    """Defaults to 0.05 (5%) when not present in yaml — matches MomentumStrategy default."""
    from trading_bot.config import RiskConfig
    cfg = RiskConfig(
        daily_loss_limit_pct=2.0,
        weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0,
        max_position_pct=10.0,
        max_symbol_concentration_pct=5.0,
        max_consecutive_losing_days=3,
    )
    assert cfg.unprotected_stop_pct == 0.05


def test_risk_config_unprotected_stop_pct_override():
    from trading_bot.config import RiskConfig
    cfg = RiskConfig(
        daily_loss_limit_pct=2.0,
        weekly_loss_limit_pct=5.0,
        per_trade_risk_pct=1.0,
        max_position_pct=10.0,
        max_symbol_concentration_pct=5.0,
        max_consecutive_losing_days=3,
        unprotected_stop_pct=0.03,
    )
    assert cfg.unprotected_stop_pct == 0.03
```

- [ ] **Step 3: Run tests, expect failure**

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && pytest tests/test_config.py::test_risk_config_unprotected_stop_pct_default -v`

Expected: FAIL with pydantic `ValidationError` or `AttributeError` mentioning `unprotected_stop_pct`.

- [ ] **Step 4: Add field to RiskConfig**

In `src/trading_bot/config.py`, modify the `RiskConfig` class:

```python
class RiskConfig(BaseModel):
    daily_loss_limit_pct: float = Field(gt=0, le=10)
    weekly_loss_limit_pct: float = Field(gt=0, le=20)
    per_trade_risk_pct: float = Field(gt=0, le=5)
    max_position_pct: float = Field(gt=0, le=25)
    max_symbol_concentration_pct: float = Field(gt=0, le=25)
    max_consecutive_losing_days: int = Field(gt=0, le=10)
    unprotected_stop_pct: float = Field(default=0.05, gt=0, le=0.5)
```

- [ ] **Step 5: Add yaml entry**

In `strategy/config.yaml`, modify the `risk:` block:

```yaml
risk:
  daily_loss_limit_pct: 2.0
  weekly_loss_limit_pct: 5.0
  per_trade_risk_pct: 1.0
  max_position_pct: 10.0
  max_symbol_concentration_pct: 5.0
  max_consecutive_losing_days: 3
  unprotected_stop_pct: 0.05            # protective stop for unprotected positions
```

- [ ] **Step 6: Run tests, expect pass**

Run: `pytest tests/test_config.py -v -k unprotected`

Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/config.py strategy/config.yaml tests/test_config.py
git commit -m "feat(config): add risk.unprotected_stop_pct (default 5%)"
```

---

## Task 2: Add `place_protective_stop` to AlpacaClient

**Files:**
- Modify: `src/trading_bot/alpaca_client.py` — new method + `StopOrderRequest` import
- Test: `tests/test_alpaca_client.py`

- [ ] **Step 1: Write failing test for stock stop placement**

Add to `tests/test_alpaca_client.py`:

```python
def test_place_protective_stop_stock_long(fake_settings):
    """Long stock: places plain StopOrderRequest with side=SELL, GTC."""
    from decimal import Decimal
    from alpaca.trading.requests import StopOrderRequest
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-123")
        client = AlpacaClient(fake_settings)
        order_id = client.place_protective_stop(
            symbol="AAPL",
            qty=Decimal("10"),
            position_side=OrderSide.BUY,  # long
            asset_class=AssetClass.STOCK,
            stop_price=Decimal("180.00"),
        )

    assert order_id == "stop-123"
    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert isinstance(call_arg, StopOrderRequest)
    assert call_arg.symbol == "AAPL"
    assert float(call_arg.qty) == 10.0
    assert str(call_arg.side).lower().endswith("sell")
    assert float(call_arg.stop_price) == 180.00


def test_place_protective_stop_crypto_long_uses_stop_limit(fake_settings):
    """Crypto long: places StopLimitOrderRequest because Alpaca rejects plain stops on crypto.
    Symbol is rewritten 'DOTUSD' → 'DOT/USD' for orders."""
    from decimal import Decimal
    from alpaca.trading.requests import StopLimitOrderRequest
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-c1")
        client = AlpacaClient(fake_settings)
        order_id = client.place_protective_stop(
            symbol="DOTUSD",  # position-form symbol
            qty=Decimal("100"),
            position_side=OrderSide.BUY,  # long
            asset_class=AssetClass.CRYPTO,
            stop_price=Decimal("5.00"),
        )

    assert order_id == "stop-c1"
    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert isinstance(call_arg, StopLimitOrderRequest)
    assert call_arg.symbol == "DOT/USD"
    assert float(call_arg.stop_price) == 5.00
    # Sell-stop limit must be ≤ trigger; existing CRYPTO_STOP_LIMIT_BUFFER_PCT = 5%.
    assert float(call_arg.limit_price) <= 5.00


def test_place_protective_stop_short_uses_buy_side(fake_settings):
    """Short position (rare but supported): protective stop is a BUY stop above current."""
    from decimal import Decimal
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.return_value = MagicMock(id="stop-s")
        client = AlpacaClient(fake_settings)
        client.place_protective_stop(
            symbol="AAPL",
            qty=Decimal("5"),
            position_side=OrderSide.SELL,  # short
            asset_class=AssetClass.STOCK,
            stop_price=Decimal("200.00"),
        )

    call_arg = MockTC.return_value.submit_order.call_args[0][0]
    assert str(call_arg.side).lower().endswith("buy")


def test_place_protective_stop_propagates_alpaca_errors(fake_settings):
    from decimal import Decimal
    from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderSide
    from trading_bot.exceptions import AlpacaClientError

    with patch("trading_bot.alpaca_client.TradingClient") as MockTC:
        MockTC.return_value.submit_order.side_effect = RuntimeError("rejected")
        client = AlpacaClient(fake_settings)
        with pytest.raises(AlpacaClientError, match="protective stop"):
            client.place_protective_stop(
                symbol="AAPL", qty=Decimal("1"), position_side=OrderSide.BUY,
                asset_class=AssetClass.STOCK, stop_price=Decimal("100"),
            )
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_alpaca_client.py -v -k protective_stop`

Expected: FAIL with `AttributeError: 'AlpacaClient' object has no attribute 'place_protective_stop'`.

- [ ] **Step 3: Add `StopOrderRequest` to imports**

In `src/trading_bot/alpaca_client.py`, modify the import block at the top:

```python
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
```

- [ ] **Step 4: Add helper + method**

In `src/trading_bot/alpaca_client.py`, add two pieces.

First, a module-level helper near the existing `_to_alpaca_side` / `_opposite` (around line 95):

```python
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
```

Then add a method on `AlpacaClient`, placed right after `place_market_order` (around alpaca_client.py:191):

```python
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
            return str(order.id)
        except Exception as e:
            raise AlpacaClientError(
                f"protective stop failed for {symbol}: {e}"
            ) from e
```

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest tests/test_alpaca_client.py -v -k protective_stop`

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/alpaca_client.py tests/test_alpaca_client.py
git commit -m "feat(alpaca): add place_protective_stop for standalone stops"
```

---

## Task 3: Create `position_protection.py` — `_decide` pure function

**Files:**
- Create: `src/trading_bot/position_protection.py`
- Create: `tests/test_position_protection.py`

- [ ] **Step 1: Write failing tests for `_decide`**

Create `tests/test_position_protection.py`:

```python
"""Tests for src/trading_bot/position_protection.py — open-position auto-protect."""
from __future__ import annotations

from decimal import Decimal

import pytest


def test_decide_protect_when_stop_below_current():
    """Stop level computed via max(EMA20, last_close*(1-stop_pct)) is below
    current price → place a stop, don't flatten."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=100.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "protect"
    # stop = max(95, 100*0.95) = max(95, 95) = 95 — equality goes to PROTECT here
    # because the comparison is stop < current (95 < 100 → True).
    assert stop == pytest.approx(95.0)


def test_decide_flatten_when_ema_above_current():
    """Price below EMA-20 → strategy stop sits above current → flatten."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=90.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "flatten"
    assert stop == pytest.approx(95.0)


def test_decide_pct_stop_wins_when_ema_far_below():
    """When EMA-20 is well below the % floor, the % floor is the stop."""
    from trading_bot.position_protection import _decide
    decision, stop = _decide(
        current_price=100.0, ema_20=50.0, stop_pct=Decimal("0.05"),
    )
    assert decision == "protect"
    assert stop == pytest.approx(95.0)  # 100 * 0.95


def test_decide_boundary_equality_goes_to_flatten():
    """Spec: boundary case (stop == current) is FLATTEN. The check is `stop < current`."""
    from trading_bot.position_protection import _decide
    decision, _stop = _decide(
        current_price=95.0, ema_20=95.0, stop_pct=Decimal("0.05"),
    )
    # 95*(1-0.05)=90.25; max(95, 90.25)=95 == current → flatten
    assert decision == "flatten"
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_position_protection.py -v -k decide`

Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.position_protection'`.

- [ ] **Step 3: Implement `_decide`**

Create `src/trading_bot/position_protection.py`:

```python
"""Open-position auto-protect — decides whether to place a protective stop or
market-flatten an unprotected open position, then carries out the action.

Triggered from cli.py:verify_stops every :20 / :50 of every hour.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


def _decide(
    *, current_price: float, ema_20: float, stop_pct: Decimal,
) -> tuple[Literal["protect", "flatten"], float]:
    """Compute strategy-aligned protective stop and decide the action.

    Mirrors MomentumStrategy.evaluate's stop math:
        stop = max(ema_20, last_close * (1 - stop_pct))

    Returns ('protect', stop_level) when stop < current_price (position is
    above its protective floor), or ('flatten', stop_level) when the floor
    has already been crossed.
    """
    pct_stop = current_price * (1.0 - float(stop_pct))
    stop = max(ema_20, pct_stop)
    decision: Literal["protect", "flatten"] = (
        "protect" if stop < current_price else "flatten"
    )
    return decision, stop
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_position_protection.py -v -k decide`

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/position_protection.py tests/test_position_protection.py
git commit -m "feat(position-protection): pure _decide function"
```

---

## Task 4: Add `ProtectionAction` and `evaluate_and_act`

**Files:**
- Modify: `src/trading_bot/position_protection.py`
- Modify: `tests/test_position_protection.py`

- [ ] **Step 1: Write failing tests for `evaluate_and_act`**

Append to `tests/test_position_protection.py`:

```python
# ----------------------------------------------------------------------
# evaluate_and_act
# ----------------------------------------------------------------------

import pandas as pd
from unittest.mock import MagicMock


def _bars_with_close_and_ema(*, last_close: float, ema_20: float) -> pd.DataFrame:
    """Return a 30-bar DataFrame so compute_indicators won't be called by tests
    (we patch it). The DataFrame just has to be non-empty."""
    return pd.DataFrame({"close": [last_close] * 30})


def _stub_indicators(*, last_close: float, ema_20: float):
    from trading_bot.market_data import Indicators
    return Indicators(
        last_close=last_close, rsi_14=50.0, macd=0.0, macd_signal=0.0,
        ema_20=ema_20, return_5d=0.0,
    )


def _make_position(symbol: str, qty: str, asset_class: str = "us_equity"):
    from trading_bot.alpaca_client import Position
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        market_value=Decimal("1000"),
        avg_entry_price=Decimal("100"),
        unrealized_pl=Decimal("0"),
        asset_class=asset_class,
    )


def test_evaluate_and_act_long_stock_healthy_places_stop(monkeypatch):
    """Healthy long stock during RTH → place stop, no flatten."""
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )

    client = MagicMock()
    client.place_protective_stop.return_value = "stop-1"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert len(actions) == 1
    a = actions[0]
    assert a.symbol == "AAPL"
    assert a.outcome == "stop_placed"
    assert a.stop_price == pytest.approx(95.0)
    assert a.current_price == pytest.approx(100.0)

    client.place_protective_stop.assert_called_once()
    kw = client.place_protective_stop.call_args.kwargs
    assert kw["symbol"] == "AAPL"
    assert kw["qty"] == Decimal("10")
    assert kw["position_side"] == OrderSide.BUY
    assert kw["asset_class"] == AssetClass.STOCK
    client.place_market_order.assert_not_called()


def test_evaluate_and_act_long_stock_broken_during_rth_flattens(monkeypatch):
    """Broken long stock during RTH → market-flatten."""
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=90.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=90.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_market_order.return_value = "flat-1"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert actions[0].outcome == "flattened"
    client.place_market_order.assert_called_once_with(
        symbol="AAPL", qty=10.0, side=OrderSide.SELL,
        asset_class=AssetClass.STOCK,
    )
    client.place_protective_stop.assert_not_called()


def test_evaluate_and_act_long_stock_broken_off_hours_defers(monkeypatch):
    """Broken stock outside RTH → defer (Alpaca rejects market sell off-hours)."""
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=90.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=90.0, ema_20=95.0,
    )
    client = MagicMock()

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=False,
    )

    assert actions[0].outcome == "deferred_off_hours"
    client.place_market_order.assert_not_called()
    client.place_protective_stop.assert_not_called()


def test_evaluate_and_act_long_stock_healthy_off_hours_places_stop(monkeypatch):
    """Healthy stock off-hours → still place stop (GTC rests into next session)."""
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_protective_stop.return_value = "stop-x"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=False,
    )

    assert actions[0].outcome == "stop_placed"
    client.place_protective_stop.assert_called_once()


def test_evaluate_and_act_crypto_off_hours_still_flattens(monkeypatch):
    """Crypto trades 24/7 — broken crypto outside RTH still gets flattened."""
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=4.0, ema_20=5.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=4.0, ema_20=5.0,
    )
    client = MagicMock()
    client.place_market_order.return_value = "flat-c"

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("DOTUSD", "100", asset_class="crypto")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=False,
    )

    assert actions[0].outcome == "flattened"
    client.place_market_order.assert_called_once_with(
        symbol="DOTUSD", qty=100.0, side=OrderSide.SELL,
        asset_class=AssetClass.CRYPTO,
    )


def test_evaluate_and_act_alpaca_failure_records_failed(monkeypatch):
    """Alpaca exception during order submit → outcome=failed, loop continues."""
    from trading_bot import position_protection as pp
    from trading_bot.exceptions import AlpacaClientError

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_protective_stop.side_effect = AlpacaClientError("rate limit")

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[
            _make_position("AAPL", "10"),
            _make_position("MSFT", "5"),
        ],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert len(actions) == 2
    assert all(a.outcome == "failed" for a in actions)
    assert "rate limit" in actions[0].error
    # Both positions attempted — failure on first did not abort the loop.
    assert client.place_protective_stop.call_count == 2


def test_evaluate_and_act_market_data_failure_records_failed(monkeypatch):
    """get_daily_bars raises → outcome=failed, no order submitted."""
    from trading_bot import position_protection as pp
    from trading_bot.exceptions import AlpacaClientError

    md = MagicMock()
    md.get_daily_bars.side_effect = AlpacaClientError("bars unavailable")
    client = MagicMock()

    actions = pp.evaluate_and_act(
        client=client, market_data=md,
        unprotected=[_make_position("AAPL", "10")],
        stop_pct=Decimal("0.05"),
        now_in_market_hours=True,
    )

    assert actions[0].outcome == "failed"
    assert "bars unavailable" in actions[0].error
    client.place_protective_stop.assert_not_called()
    client.place_market_order.assert_not_called()


def test_evaluate_and_act_short_position_uses_buy_actions(monkeypatch):
    """Short position (qty < 0): protective action takes the BUY side."""
    from trading_bot.alpaca_client import OrderSide
    from trading_bot import position_protection as pp

    monkeypatch.setattr(
        pp, "compute_indicators",
        lambda bars: _stub_indicators(last_close=100.0, ema_20=95.0),
    )

    md = MagicMock()
    md.get_daily_bars.return_value = _bars_with_close_and_ema(
        last_close=100.0, ema_20=95.0,
    )
    client = MagicMock()
    client.place_protective_stop.return_value = "stop-s"

    short = _make_position("AAPL", "-10")
    pp.evaluate_and_act(
        client=client, market_data=md, unprotected=[short],
        stop_pct=Decimal("0.05"), now_in_market_hours=True,
    )

    kw = client.place_protective_stop.call_args.kwargs
    assert kw["position_side"] == OrderSide.SELL  # short side
    # qty passed positive to Alpaca
    assert kw["qty"] == Decimal("10")
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_position_protection.py -v -k evaluate_and_act`

Expected: FAIL with `AttributeError: module 'trading_bot.position_protection' has no attribute 'evaluate_and_act'`.

- [ ] **Step 3: Implement `ProtectionAction` and `evaluate_and_act`**

Append to `src/trading_bot/position_protection.py`:

```python
from trading_bot.alpaca_client import (
    AlpacaClient, AssetClass, OrderSide, Position,
)
from trading_bot.exceptions import AlpacaClientError
from trading_bot.market_data import MarketDataClient, compute_indicators


@dataclass(frozen=True)
class ProtectionAction:
    """Result of attempting to protect or close one unprotected position."""
    symbol: str
    qty: Decimal
    position_side: OrderSide
    asset_class: AssetClass
    outcome: Literal[
        "stop_placed", "flattened", "failed", "deferred_off_hours"
    ]
    # Populated for stop_placed.
    stop_price: float | None = None
    current_price: float | None = None
    # Populated for flattened (estimate based on last close — actual fill price unknown).
    fill_estimate: float | None = None
    # Populated for failed.
    error: str | None = None


def _classify_asset(raw: str) -> AssetClass:
    """Position.asset_class is a free-form string from Alpaca; normalise."""
    s = raw.lower()
    if "crypto" in s:
        return AssetClass.CRYPTO
    if "option" in s:
        return AssetClass.OPTION
    return AssetClass.STOCK


def _position_side(qty: Decimal) -> OrderSide:
    return OrderSide.BUY if qty >= 0 else OrderSide.SELL


def evaluate_and_act(
    *,
    client: AlpacaClient,
    market_data: MarketDataClient,
    unprotected: list[Position],
    stop_pct: Decimal,
    now_in_market_hours: bool,
) -> list[ProtectionAction]:
    """For each unprotected position: compute the strategy-aligned stop, then
    place it (healthy) or market-flatten (broken). Off-hours stocks defer the
    flatten path because Alpaca rejects equity market orders outside RTH.

    Failures (market-data or order-submit) are captured per-symbol so one bad
    apple doesn't abort the sweep.
    """
    actions: list[ProtectionAction] = []
    for pos in unprotected:
        asset_class = _classify_asset(pos.asset_class)
        side = _position_side(pos.qty)
        abs_qty = abs(pos.qty)

        try:
            bars = market_data.get_daily_bars(pos.symbol, lookback_days=60)
            ind = compute_indicators(bars)
        except (AlpacaClientError, ValueError) as e:
            actions.append(ProtectionAction(
                symbol=pos.symbol, qty=abs_qty, position_side=side,
                asset_class=asset_class, outcome="failed", error=str(e),
            ))
            continue

        decision, stop_level = _decide(
            current_price=ind.last_close, ema_20=ind.ema_20, stop_pct=stop_pct,
        )

        # Off-hours stock that needs flattening: defer.
        if (
            decision == "flatten"
            and asset_class == AssetClass.STOCK
            and not now_in_market_hours
        ):
            actions.append(ProtectionAction(
                symbol=pos.symbol, qty=abs_qty, position_side=side,
                asset_class=asset_class, outcome="deferred_off_hours",
            ))
            continue

        try:
            if decision == "protect":
                client.place_protective_stop(
                    symbol=pos.symbol, qty=abs_qty,
                    position_side=side, asset_class=asset_class,
                    stop_price=Decimal(str(stop_level)).quantize(Decimal("0.01")),
                )
                actions.append(ProtectionAction(
                    symbol=pos.symbol, qty=abs_qty, position_side=side,
                    asset_class=asset_class, outcome="stop_placed",
                    stop_price=stop_level, current_price=ind.last_close,
                ))
            else:
                close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
                client.place_market_order(
                    symbol=pos.symbol, qty=float(abs_qty),
                    side=close_side, asset_class=asset_class,
                )
                actions.append(ProtectionAction(
                    symbol=pos.symbol, qty=abs_qty, position_side=side,
                    asset_class=asset_class, outcome="flattened",
                    fill_estimate=ind.last_close,
                ))
        except AlpacaClientError as e:
            actions.append(ProtectionAction(
                symbol=pos.symbol, qty=abs_qty, position_side=side,
                asset_class=asset_class, outcome="failed", error=str(e),
            ))
    return actions
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_position_protection.py -v`

Expected: 12 passed (4 from Task 3 + 8 here).

- [ ] **Step 5: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/position_protection.py tests/test_position_protection.py
git commit -m "feat(position-protection): evaluate_and_act orchestrator + ProtectionAction"
```

---

## Task 5: Add `build_open_positions_email_html`, remove old builder

**Files:**
- Modify: `src/trading_bot/reports.py` (replace `build_naked_stops_email_html` with new builder; update module docstring at line 21)
- Modify: `tests/test_reports.py` (replace `test_naked_stops_email_*` with new tests; remove the stale import)

- [ ] **Step 1: Write failing tests for the new builder**

In `tests/test_reports.py`, find and **delete** the existing block:

```python
# Lines around 192-218 — the "Naked-stops alert" section and its two tests.
```

Then **delete** `build_naked_stops_email_html` from the import block at the top of `tests/test_reports.py` (around line 13).

Now add a new test block (replacing the deleted naked-stops block):

```python
# --------------------------------------------------------------------------
# Open Positions email (auto-protect summary)
# --------------------------------------------------------------------------


def _make_action(
    *, symbol="AAPL", qty="10", outcome="stop_placed",
    asset_class="stock", position_side="buy",
    stop_price=None, current_price=None, fill_estimate=None, error=None,
):
    from decimal import Decimal
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot.position_protection import ProtectionAction
    return ProtectionAction(
        symbol=symbol, qty=Decimal(qty),
        position_side=OrderSide(position_side),
        asset_class=AssetClass(asset_class),
        outcome=outcome,
        stop_price=stop_price, current_price=current_price,
        fill_estimate=fill_estimate, error=error,
    )


def test_open_positions_email_lists_protected_symbols():
    from trading_bot.reports import build_open_positions_email_html
    actions = [
        _make_action(symbol="AAPL", outcome="stop_placed",
                     stop_price=180.0, current_price=200.0),
        _make_action(symbol="MSFT", outcome="stop_placed",
                     stop_price=380.0, current_price=400.0),
    ]
    html = build_open_positions_email_html(actions, total_positions=5)
    assert "AAPL" in html
    assert "MSFT" in html
    assert "180.00" in html
    assert "Protected" in html


def test_open_positions_email_lists_closed_symbols():
    from trading_bot.reports import build_open_positions_email_html
    actions = [
        _make_action(symbol="XYZ", outcome="flattened", fill_estimate=12.34),
    ]
    html = build_open_positions_email_html(actions, total_positions=3)
    assert "XYZ" in html
    assert "Closed" in html
    assert "12.34" in html


def test_open_positions_email_lists_failures_and_deferred():
    from trading_bot.reports import build_open_positions_email_html
    actions = [
        _make_action(symbol="AAA", outcome="failed", error="rate limit"),
        _make_action(symbol="BBB", outcome="deferred_off_hours"),
    ]
    html = build_open_positions_email_html(actions, total_positions=2)
    assert "Failed" in html
    assert "rate limit" in html
    assert "Deferred" in html
    assert "BBB" in html


def test_open_positions_email_subject_clean_when_all_actioned():
    """No failures/deferred → subject is just 'Open Positions — N actioned'."""
    from trading_bot.reports import open_positions_email_subject
    actions = [
        _make_action(symbol="AAPL", outcome="stop_placed",
                     stop_price=180.0, current_price=200.0),
    ]
    subject = open_positions_email_subject(actions)
    assert subject == "Open Positions — 1 actioned"


def test_open_positions_email_subject_flags_attention_needed():
    """Any failed or deferred → 'N actioned, M need attention'."""
    from trading_bot.reports import open_positions_email_subject
    actions = [
        _make_action(symbol="AAPL", outcome="stop_placed",
                     stop_price=180.0, current_price=200.0),
        _make_action(symbol="BBB", outcome="failed", error="x"),
        _make_action(symbol="CCC", outcome="deferred_off_hours"),
    ]
    subject = open_positions_email_subject(actions)
    assert subject == "Open Positions — 1 actioned, 2 need attention"
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_reports.py -v -k open_positions`

Expected: FAIL with `ImportError: cannot import name 'build_open_positions_email_html' from 'trading_bot.reports'`.

- [ ] **Step 3: Replace the email builder**

In `src/trading_bot/reports.py`:

(a) Update the module docstring at lines 17-23. Change:

```python
    build_naked_stops_email_html(naked)  — verify-stops alert (NEW)
```

to:

```python
    build_open_positions_email_html(actions)  — verify-stops auto-protect summary
    open_positions_email_subject(actions)     — subject line for the above
```

(b) **Replace** the existing `build_naked_stops_email_html` function (lines 890-946) with the following two functions. Use `git diff` after to confirm the old function is gone:

```python
def open_positions_email_subject(actions) -> str:
    """Subject line for the verify-stops auto-protect summary.

    `Open Positions — N actioned`               (clean run)
    `Open Positions — N actioned, M need attention`   (any failed/deferred)
    """
    actioned = sum(
        1 for a in actions if a.outcome in ("stop_placed", "flattened")
    )
    attention = sum(
        1 for a in actions if a.outcome in ("failed", "deferred_off_hours")
    )
    if attention:
        return f"Open Positions — {actioned} actioned, {attention} need attention"
    return f"Open Positions — {actioned} actioned"


def build_open_positions_email_html(
    actions,
    *,
    total_positions: int | None = None,
) -> str:
    """Verify-stops auto-protect summary. Renders one section per outcome
    bucket; sections with no rows are omitted."""
    protected = [a for a in actions if a.outcome == "stop_placed"]
    closed = [a for a in actions if a.outcome == "flattened"]
    failed = [a for a in actions if a.outcome == "failed"]
    deferred = [a for a in actions if a.outcome == "deferred_off_hours"]

    kpis = _kpi_grid([
        _kpi_card("Total Open",
                  str(total_positions) if total_positions is not None else "—"),
        _kpi_card("Stops Placed", str(len(protected)), value_color=_GOOD),
        _kpi_card("Closed", str(len(closed)),
                  value_color=_BAD if closed else _TEXT_PRIMARY),
        _kpi_card("Need Attention", str(len(failed) + len(deferred)),
                  value_color=_WARN if (failed or deferred) else _TEXT_PRIMARY),
    ])

    body_parts: list[str] = [kpis]

    if protected:
        rows = []
        for a in protected:
            distance_pct = (
                (a.current_price - a.stop_price) / a.current_price * 100.0
                if a.current_price else 0.0
            )
            rows.append([
                f"<strong style=\"color:{_GOOD_LIGHT};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
                f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
                _pill(a.position_side.value, "good" if a.position_side.value == "buy" else "bad"),
                f"<span style=\"font-family:{_MONO_STACK}\">${a.current_price:,.2f}</span>",
                f"<span style=\"font-family:{_MONO_STACK}\">${a.stop_price:,.2f}</span>",
                f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_SECONDARY}\">{distance_pct:.2f}%</span>",
            ])
        body_parts.append(_section(
            "Protected",
            _data_table(
                headers=["Symbol", "Qty", "Side", "Last", "Stop", "Distance"],
                rows=rows,
            ),
            accent_glyph="●",
        ))

    if closed:
        rows = [[
            f"<strong style=\"color:{_BAD};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
            _pill(a.position_side.value, "good" if a.position_side.value == "buy" else "bad"),
            f"<span style=\"font-family:{_MONO_STACK}\">${a.fill_estimate:,.2f}</span>",
        ] for a in closed]
        body_parts.append(_section(
            "Closed",
            _data_table(
                headers=["Symbol", "Qty", "Side", "Last"],
                rows=rows,
            ),
            accent_glyph="◆",
        ))

    if failed:
        rows = [[
            f"<strong style=\"color:{_BAD};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
            f"<span style=\"font-family:{_FONT_STACK};color:{_TEXT_PRIMARY}\">{a.error or ''}</span>",
        ] for a in failed]
        body_parts.append(_section(
            "Failed — needs manual review",
            _data_table(headers=["Symbol", "Qty", "Error"], rows=rows),
            accent_glyph="⚠",
        ))

    if deferred:
        rows = [[
            f"<strong style=\"color:{_WARN};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
            _pill(a.position_side.value, "good" if a.position_side.value == "buy" else "bad"),
        ] for a in deferred]
        body_parts.append(_section(
            "Deferred to next session",
            _data_table(headers=["Symbol", "Qty", "Side"], rows=rows),
            accent_glyph="◆",
        ))

    subtitle = (
        f"{_pill('open positions', 'info')} "
        f"<span style=\"color:{_TEXT_SECONDARY};margin-left:8px\">"
        f"{len(protected)} protected · {len(closed)} closed · "
        f"{len(failed) + len(deferred)} need attention</span>"
    )

    return _shell(
        title="Open Positions — Auto-Protect Summary",
        subtitle_html=subtitle,
        body_html="".join(body_parts),
        accent=_ACCENT,
    )
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_reports.py -v -k open_positions`

Expected: 5 passed.

- [ ] **Step 5: Run the full reports test suite to catch any breakage**

Run: `pytest tests/test_reports.py -v`

Expected: all pass. The deleted `test_naked_stops_email_*` tests should not appear in collection.

- [ ] **Step 6: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/reports.py tests/test_reports.py
git commit -m "feat(reports): build_open_positions_email_html replaces naked-stops builder"
```

---

## Task 6: Wire `cli.py:verify_stops` to use the new pipeline

**Files:**
- Modify: `src/trading_bot/cli.py:35-41` (imports)
- Modify: `src/trading_bot/cli.py:767-825` (verify_stops body)

- [ ] **Step 1: Update the imports in cli.py**

In `src/trading_bot/cli.py`, lines 35-41, change:

```python
from trading_bot.reports import (
    build_alert_email_html,
    build_daily_report_html,
    build_naked_stops_email_html,
    build_rich_report_html,
    build_vip_alert_email_html,
)
```

to:

```python
from trading_bot.reports import (
    build_alert_email_html,
    build_daily_report_html,
    build_open_positions_email_html,
    build_rich_report_html,
    build_vip_alert_email_html,
    open_positions_email_subject,
)
```

- [ ] **Step 2: Rewrite `verify_stops`**

Replace the entire `verify_stops` function body in `src/trading_bot/cli.py` (currently lines 767-825) with:

```python
@main.command("verify-stops")
def verify_stops() -> None:
    """Sweep open positions, auto-protect or flatten any unprotected ones,
    email a summary of actions taken. Stocks act 24/7 for stop placement;
    market-flatten for stocks defers outside US RTH (Alpaca rejects market
    sells off-hours). Crypto acts 24/7."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from trading_bot.alpaca_client import AlpacaClient
    from trading_bot.market_data import MarketDataClient
    from trading_bot.position_protection import evaluate_and_act
    from trading_bot.supervisor import _is_market_hours_et

    settings = Settings()
    cfg = load_config(CONFIG_PATH)

    try:
        alpaca = AlpacaClient(settings)
        positions = alpaca.get_positions()
        open_orders = alpaca._client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
        )
    except Exception as e:
        click.echo(f"[verify-stops] alpaca query failed: {e}")
        return  # do not raise SystemExit — would kill the APScheduler worker.

    def _canon(sym: str) -> str:
        return str(sym).replace("/", "").upper()

    stops_by_symbol: dict[str, list] = {}
    for o in open_orders:
        if str(getattr(o, "type", "")).lower().endswith(("stop", "stop_limit")):
            stops_by_symbol.setdefault(_canon(o.symbol), []).append(o)

    unprotected = [p for p in positions if _canon(p.symbol) not in stops_by_symbol]

    click.echo(
        f"[verify-stops] positions={len(positions)} "
        f"stops={sum(len(v) for v in stops_by_symbol.values())} "
        f"unprotected={len(unprotected)}"
    )

    if not unprotected:
        return

    market_data = MarketDataClient(settings)
    actions = evaluate_and_act(
        client=alpaca,
        market_data=market_data,
        unprotected=unprotected,
        stop_pct=Decimal(str(cfg.risk.unprotected_stop_pct)),
        now_in_market_hours=_is_market_hours_et(),
    )

    for a in actions:
        click.echo(f"  {a.outcome.upper():22} {a.symbol:10} qty={a.qty}")

    html = build_open_positions_email_html(
        actions, total_positions=len(positions)
    )
    subject = open_positions_email_subject(actions)
    EmailSender(
        user=settings.gmail_user,
        app_password=settings.gmail_app_password,
        to=cfg.email.to,
    ).send(subject=subject, html_body=html)
    click.echo(f"[verify-stops] summary email sent to {cfg.email.to}")
```

- [ ] **Step 3: Verify imports `Decimal` and `EmailSender` are still in scope**

Run: `grep -n "from decimal\|EmailSender\|Settings\b\|load_config\|CONFIG_PATH" /Users/bharathkandala/Trading/src/trading_bot/cli.py | head -10`

Expected: confirm `Decimal`, `EmailSender`, `Settings`, `load_config`, `CONFIG_PATH` are all imported at module level. If any are missing (unlikely — they were used before), add them.

- [ ] **Step 4: Check that no other code still imports `build_naked_stops_email_html`**

Run: `grep -rn "build_naked_stops\|naked_stops_email" /Users/bharathkandala/Trading/src /Users/bharathkandala/Trading/tests 2>/dev/null`

Expected: empty output. If any matches remain, fix them.

- [ ] **Step 5: Run the full test suite**

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && pytest tests/ -x -q`

Expected: all tests pass. If a `tests/test_cli.py` test exercises `verify_stops` with the old behavior, update it to match the new flow (the test would mock Alpaca and assert the new email subject).

- [ ] **Step 6: Manual smoke test (read-only)**

Don't run `bot verify-stops` against a live account — it would now place real (paper) orders. Instead just import-check:

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && python -c "from trading_bot import cli; print(cli.verify_stops.help)"`

Expected: prints the new docstring.

- [ ] **Step 7: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/cli.py
git commit -m "feat(cli): verify-stops auto-protects unprotected positions"
```

---

## Task 7: Update dashboard architecture.html prose

**Files:**
- Modify: `src/trading_bot/dashboard/templates/architecture.html`

- [ ] **Step 1: Replace the seven prose mentions**

Apply these edits in `src/trading_bot/dashboard/templates/architecture.html`. The line numbers below are from the current state — confirm with `grep -n` before each edit if line numbers have drifted.

Line 119, change:
```
... if the position is naked, it market-flattens immediately.
```
to:
```
... if the position is unprotected, it market-flattens immediately.
```

Line 312, change:
```
... a flash crash could leave us naked. <strong>Mitigation:</strong> ...
```
to:
```
... a flash crash could leave the position unprotected. <strong>Mitigation:</strong> ...
```

Line 314, change:
```
A scheduled job (<code>bot verify-stops</code>, every :20 / :50 24/7) sweeps every open position, confirms each has a live stop order, and emails an alert if any are naked. Catches the rare bracket-leg detachment bug Alpaca has had in the past.
```
to:
```
A scheduled job (<code>bot verify-stops</code>, every :20 / :50 24/7) sweeps every open position. For any without a live stop, it auto-protects: places a strategy-aligned stop if the position is above its protective floor, or market-flattens if it's already broken (deferring stock flattens to next RTH). Emails a summary of the actions taken. Catches the rare bracket-leg detachment bug Alpaca has had in the past.
```

Line 323, change:
```
<li><strong>Stop-leg verification.</strong> Every :20 / :50 24/7, the verify-stops sweep confirms every open position has a corresponding stop order. Naked positions trigger an immediate email.</li>
```
to:
```
<li><strong>Open-position auto-protect.</strong> Every :20 / :50 24/7, the verify-stops sweep confirms every open position has a stop. Unprotected positions are auto-actioned (stop placed or flattened) and a summary email is sent.</li>
```

Line 407, change:
```
<tr><td><code>20,50 * * * *</code></td><td>verify-stops</td><td>Naked-position sweep, 24/7</td></tr>
```
to:
```
<tr><td><code>20,50 * * * *</code></td><td>verify-stops</td><td>Open-position auto-protect, 24/7</td></tr>
```

Line 436, change:
```
<tr><td><code>bot verify-stops</code></td><td>Naked-position sweep</td></tr>
```
to:
```
<tr><td><code>bot verify-stops</code></td><td>Open-position auto-protect sweep</td></tr>
```

Line 451, change:
```
<li><strong>Naked-position alert</strong> — any open position lacks a live stop order (verify-stops)</li>
```
to:
```
<li><strong>Open-position summary</strong> — verify-stops sweep auto-actioned one or more unprotected positions, or one needs manual review</li>
```

- [ ] **Step 2: Confirm no naked references remain in the template**

Run: `grep -in "naked" /Users/bharathkandala/Trading/src/trading_bot/dashboard/templates/architecture.html`

Expected: no matches.

- [ ] **Step 3: Render the dashboard locally to spot-check**

Skip if the user is in a hurry. Otherwise:

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && bot dashboard --port 8765 &` then open `http://127.0.0.1:8765/architecture` in a browser. Stop the dashboard (`kill %1`) when done.

- [ ] **Step 4: Run dashboard tests**

Run: `pytest tests/test_dashboard.py -v`

Expected: all pass (the template tests don't typically grep for "naked", but confirm nothing references the renamed text).

- [ ] **Step 5: Commit**

```bash
cd /Users/bharathkandala/Trading
git add src/trading_bot/dashboard/templates/architecture.html
git commit -m "docs(dashboard): rename naked-position prose to open-position auto-protect"
```

---

## Task 8: Final verification

- [ ] **Step 1: Full test suite**

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && pytest tests/ -q`

Expected: all green.

- [ ] **Step 2: Confirm "naked" is fully scrubbed from runtime code paths**

Run: `grep -rn -i "naked" /Users/bharathkandala/Trading/src 2>/dev/null`

Expected: no matches in `src/`. (Spec docs in `docs/` keep two literal references to old code names — that's intentional.)

- [ ] **Step 3: Verify the new subject string**

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && python -c "
from decimal import Decimal
from trading_bot.alpaca_client import AssetClass, OrderSide
from trading_bot.position_protection import ProtectionAction
from trading_bot.reports import open_positions_email_subject

a = ProtectionAction(symbol='AAPL', qty=Decimal('10'),
                     position_side=OrderSide.BUY, asset_class=AssetClass.STOCK,
                     outcome='stop_placed', stop_price=180.0, current_price=200.0)
print(open_positions_email_subject([a]))
"`

Expected: `Open Positions — 1 actioned`

- [ ] **Step 4: Run ruff or flake8 if configured**

Run: `cd /Users/bharathkandala/Trading && source venv/bin/activate && (ruff check src/trading_bot/position_protection.py src/trading_bot/cli.py src/trading_bot/reports.py 2>/dev/null || echo "(ruff not configured — skip)")`

Expected: clean or "(skip)".
