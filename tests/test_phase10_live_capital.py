"""Live-capital cap — paper-mode skip, expiry, per-strategy cap."""
from __future__ import annotations

import datetime as dt

from trading_bot.risk.live_capital import check_live_capital
from trading_bot.risk.types import AccountState, Position


def _acct(equity: float = 800.0) -> AccountState:
    return AccountState(
        equity=equity, cash=equity,
        equity_at_session_start=equity,
        day_trade_count=0, buying_power=equity,
    )


def _pos(symbol: str, mv: float, strategy_id: str = "DUAL_MOMENTUM_v1",
         qty: float = 1.0) -> Position:
    return Position(
        symbol=symbol, asset_class="equity", qty=qty,
        market_value=mv, classification="bot",
        strategy_id=strategy_id,
    )


PAPER_LOCK = {
    "live_capital_enabled": False,
    "total_equity_ceiling_usd": 1000.0,
    "per_strategy_max_capital_usd": {"DUAL_MOMENTUM_v1": 1000.0},
    "live_mode_expiry_iso": "2026-06-14",
}

LIVE_LOCK = {
    "live_capital_enabled": True,
    "total_equity_ceiling_usd": 1000.0,
    "per_strategy_max_capital_usd": {
        "DUAL_MOMENTUM_v1": 500.0,
        "CRYPTO_MOMENTUM_v1": 0.0,
    },
    "live_mode_expiry_iso": "2026-06-14",
}


def test_paper_mode_halts_with_disabled_reason() -> None:
    """The check itself returns halt for non-exits, but the dispatcher
    treats ``live_cap:disabled`` as 'paper mode → bypass'. Here we
    verify the reason string the dispatcher will pattern-match on."""
    d = check_live_capital(
        live_capital_lock=PAPER_LOCK,
        strategy_id="DUAL_MOMENTUM_v1",
        intent_side="buy", intent_notional=100.0,
        account=_acct(), positions=[],
    )
    assert d.verdict == "halt"
    assert "live_cap:disabled" in d.reason


def test_paper_mode_allows_exit_orders() -> None:
    d = check_live_capital(
        live_capital_lock=PAPER_LOCK,
        strategy_id="DUAL_MOMENTUM_v1",
        intent_side="sell_to_close", intent_notional=100.0,
        account=_acct(), positions=[],
    )
    assert d.verdict == "accept"


def test_live_mode_accepts_within_cap() -> None:
    d = check_live_capital(
        live_capital_lock=LIVE_LOCK,
        strategy_id="DUAL_MOMENTUM_v1",
        intent_side="buy", intent_notional=300.0,
        account=_acct(equity=400),
        positions=[_pos("SPY", mv=100.0)],
        today=dt.date(2026, 5, 20),
    )
    assert d.verdict == "accept"


def test_live_mode_halts_over_strategy_cap() -> None:
    d = check_live_capital(
        live_capital_lock=LIVE_LOCK,
        strategy_id="DUAL_MOMENTUM_v1",
        intent_side="buy", intent_notional=400.0,
        account=_acct(equity=600),
        positions=[_pos("SPY", mv=200.0)],
        today=dt.date(2026, 5, 20),
    )
    # 200 current + 400 intent = 600 > 500 cap
    assert d.verdict == "halt"
    assert "live_cap:strategy_cap" in d.reason


def test_live_mode_halts_unauthorised_strategy() -> None:
    d = check_live_capital(
        live_capital_lock=LIVE_LOCK,
        strategy_id="CRYPTO_MOMENTUM_v1",
        intent_side="buy", intent_notional=10.0,
        account=_acct(), positions=[],
        today=dt.date(2026, 5, 20),
    )
    assert d.verdict == "halt"
    assert "live_cap:strategy_disabled" in d.reason


def test_live_mode_halts_on_equity_overflow() -> None:
    d = check_live_capital(
        live_capital_lock=LIVE_LOCK,
        strategy_id="DUAL_MOMENTUM_v1",
        intent_side="buy", intent_notional=100.0,
        account=_acct(equity=2_000.0),    # over $1k ceiling
        positions=[],
        today=dt.date(2026, 5, 20),
    )
    assert d.verdict == "halt"
    assert "live_cap:equity_overflow" in d.reason


def test_live_mode_halts_when_lock_expired() -> None:
    d = check_live_capital(
        live_capital_lock=LIVE_LOCK,
        strategy_id="DUAL_MOMENTUM_v1",
        intent_side="buy", intent_notional=100.0,
        account=_acct(equity=400),
        positions=[],
        today=dt.date(2026, 7, 1),       # beyond 2026-06-14 expiry
    )
    assert d.verdict == "halt"
    assert "live_cap:lock_expired" in d.reason


def test_live_mode_allows_exit_even_when_strategy_disabled() -> None:
    """Closing a paper position should always succeed — even if the
    strategy lost its live cap."""
    d = check_live_capital(
        live_capital_lock=LIVE_LOCK,
        strategy_id="CRYPTO_MOMENTUM_v1",
        intent_side="sell_to_close", intent_notional=100.0,
        account=_acct(), positions=[],
        today=dt.date(2026, 5, 20),
    )
    assert d.verdict == "accept"
