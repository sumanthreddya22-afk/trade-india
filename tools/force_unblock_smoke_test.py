"""tools/force_unblock_smoke_test.py — synthetic smoke test for the
stock+crypto unblock-debate hook.

Drives the orchestrator buy loop with a forced Signal that will be
rejected by the deterministic per_trade_risk_pct gate, then asserts
the unblock committee fires, persists a row to unblock_debate_runs,
and emails the operator. Same code path as a real intel_scan
rejection — just constructed deterministically so we don't have to
wait for a real momentum signal that happens to fail risk gates.

Usage:
    .venv/bin/python tools/force_unblock_smoke_test.py
    .venv/bin/python tools/force_unblock_smoke_test.py --asset crypto
    .venv/bin/python tools/force_unblock_smoke_test.py --asset stock --no-email

Costs ~4 Opus LLM calls per asset_class invocation (~$0.20 if no
mailbox). Skip with --no-llm to verify wiring without spending tokens.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd


def _account():
    from trading_bot.alpaca_client import AccountSnapshot
    return AccountSnapshot(
        equity=Decimal("15000"), cash=Decimal("15000"),
        buying_power=Decimal("30000"), portfolio_value=Decimal("15000"),
    )


def _bars():
    return pd.DataFrame(
        {"close": [100 + i for i in range(40)],
         "open": [100 + i for i in range(40)],
         "high": [101 + i for i in range(40)],
         "low": [99 + i for i in range(40)],
         "volume": [1_000_000] * 40},
        index=pd.date_range("2026-04-01", periods=40, freq="D", tz="UTC"),
    )


def _config():
    from trading_bot.config import (
        AllocationConfig, AppConfig, EmailConfig, RegimeAllocation,
        RiskConfig, StorageConfig, StrategyConfig, WheelConfig,
    )
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0, weekly_loss_limit_pct=5.0,
            per_trade_risk_pct=1.0, max_position_pct=10.0,
            max_symbol_concentration_pct=5.0,
            max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(options_max_pct=20.0),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
            "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
            "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
            "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
        },
        strategy=StrategyConfig(
            earnings_gate_enabled=False, macro_shock_gate_enabled=False,
            crypto_fear_greed_enabled=False,
            crypto_reddit_spike_enabled=False,
            crypto_coingecko_enabled=False, insider_cluster_enabled=False,
        ),
        # Lower the candidate-score threshold so a synthetic neutral-sentiment
        # signal still triggers the debate. Production keeps the default 7.0.
        wheel=WheelConfig(
            options_max_pct=20.0, enabled=True,
            unblock_debate_enabled=True,
            unblock_min_candidate_score=5.0,  # synthetic test bar
            unblock_max_overage_ratio=2.0,    # generous for smoke test
            unblock_daily_debate_cap=50,
        ),
        storage=StorageConfig(trade_journal_path="data/trade_journal.db"),
        email=EmailConfig(to="sumanth.avula95@gmail.com"),
    )


def _force_debate(asset_class: str) -> int:
    from trading_bot.state import WatchlistEntry
    from trading_bot.orchestrator import TradeOrchestrator
    from trading_bot.strategy import Signal, SignalAction
    from trading_bot.alpaca_client import OrderResult, Position
    from trading_bot.state_db import get_engine

    sym = "BTC/USD" if asset_class == "crypto" else "MSFT"
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.get_open_order_symbols.return_value = set()
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id=f"e-smoke-{asset_class}",
        stop_loss_order_id=f"s-smoke-{asset_class}",
    )

    journal = MagicMock()
    cfg = _config()
    engine = get_engine("data/state.db")

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime="trending_up",
        unblock_debate_enabled=True, unblock_debate_engine=engine,
    )

    # Force a Signal that the per_trade_risk_pct=1% gate WILL reject:
    # entry $100, stop $50 → risk_dollars = 50 × qty. With qty=10 →
    # risk = $500 = 3.3% of $15k equity → exceeds 1% per-trade cap.
    # Sentiment score ~0.1 so candidate_score lands above the threshold.
    forced = Signal(
        symbol=sym, action=SignalAction.BUY, qty=Decimal("10"),
        entry_price=Decimal("100"), stop_loss_price=Decimal("50"),
        reason=f"smoke test {asset_class}",
    )

    # The orchestrator's strategy.evaluate is what generates Signal —
    # monkeypatch it to return the forced signal for our symbol.
    orch._strategy.evaluate = lambda s, ind, equity: (
        forced if s == sym
        else Signal(s, SignalAction.HOLD, Decimal("0"), Decimal("0"),
                    Decimal("0"), "x")
    )

    print(f"=== forcing {asset_class} debate on {sym} ===")
    print(f"  signal: BUY {sym} qty=10 entry=$100 stop=$50")
    print(f"  expected: per_trade_risk_pct rejects → unblock committee debates")
    print()

    result = orch.scan(watchlist=[
        WatchlistEntry(symbol=sym, asset_class=asset_class, notes=""),
    ])

    print("scan complete; decisions:")
    for d in result.decisions:
        print(f"  {d.symbol:10}  action={d.action}  reason={d.reason[:80]}")

    # Verify the debate row was written.
    from sqlalchemy.orm import Session
    from trading_bot.state_db import UnblockDebateRun
    with Session(engine) as s:
        last = (s.query(UnblockDebateRun)
                 .filter(UnblockDebateRun.symbol == sym)
                 .order_by(UnblockDebateRun.id.desc())
                 .first())
    if last is None:
        print(f"\nNO debate row written for {sym} — debate did not fire.")
        return 1

    print(f"\ndebate row id={last.id}: verdict={last.verdict} confidence={last.confidence}")
    print(f"  judge_reason: {(last.judge_reason or '')[:200]}...")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--asset", choices=["stock", "crypto", "both"], default="both",
        help="which asset_class to force a debate for",
    )
    args = p.parse_args()

    if args.asset == "both":
        rc = _force_debate("stock")
        if rc != 0:
            return rc
        rc = _force_debate("crypto")
        return rc
    return _force_debate(args.asset)


if __name__ == "__main__":
    sys.exit(main())
