"""Phase 2 — PDT entry-side check.

Plan v4 §6 PDT note: exits always pass; entries are blocked when below
the equity boundary AND the worst-case round-trip would push the
rolling counter over the threshold.
"""
from __future__ import annotations

from trading_bot.risk.pdt import check_pdt
from trading_bot.risk.types import AccountState


PDT_LOCK = {
    "day_trade_threshold": 3,
    "equity_boundary_usd": 25_000,
    "rolling_window_business_days": 5,
    "exit_policy": {"exits_always_allowed": True},
}


def _acct(equity, day_trade_count=0):
    return AccountState(equity=equity, cash=equity*0.5,
                        equity_at_session_start=equity,
                        day_trade_count=day_trade_count)


def test_exit_always_passes_even_when_count_at_limit() -> None:
    d = check_pdt(intent_side="sell_to_close",
                  account=_acct(15_000, day_trade_count=3),
                  pdt_lock=PDT_LOCK)
    assert d.verdict == "accept"
    assert "exit" in d.reason


def test_above_equity_boundary_passes() -> None:
    d = check_pdt(intent_side="buy",
                  account=_acct(30_000, day_trade_count=3),
                  pdt_lock=PDT_LOCK)
    assert d.verdict == "accept"


def test_below_boundary_under_threshold_passes() -> None:
    # equity < $25k, day_trade_count=1; worst-case +1 = 2 ≤ 3 → pass.
    d = check_pdt(intent_side="buy",
                  account=_acct(15_000, day_trade_count=1),
                  pdt_lock=PDT_LOCK)
    assert d.verdict == "accept"


def test_below_boundary_at_threshold_blocks_entry() -> None:
    # day_trade_count=3; worst-case +1 = 4 > threshold=3 → halt.
    d = check_pdt(intent_side="buy",
                  account=_acct(15_000, day_trade_count=3),
                  pdt_lock=PDT_LOCK)
    assert d.verdict == "halt"
    assert "pdt_entry_block" in d.reason
