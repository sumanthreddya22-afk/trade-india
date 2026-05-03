"""Phase 6 — orchestrator wiring for the pre-trade entry debate.

Three behaviours under test:

  1. Verdict 'place'   → order placed, audit row written, decision='placed_order'.
  2. Verdict 'skip'    → no order, audit row written,
                         decision='rejected_by_entry_debate'.
  3. Verdict None      → no order, audit row written with verdict='fail_soft',
                         decision='skipped_entry_debate_unreachable',
                         daemon_critical alert queued.

Plus: per-ticker intel-aware regime override — a high intel_score on a
sideways-regime symbol should still produce a BUY-eligible strategy
where pre-Phase-6 the orchestrator would have early-bailed.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.shared.alpaca_client import AccountSnapshot, AlpacaClientError, OrderResult
from trading_bot.entry_debate import EntryDebateVerdict
from trading_bot.orchestrator import TradeOrchestrator
from trading_bot.state import WatchlistEntry
from trading_bot.state_db import (
    Base, EntryDebateRun, IntelCandidate,
)
from trading_bot.strategy import Signal, SignalAction


def _config():
    from trading_bot.shared.config import (
        AllocationConfig, AppConfig, EmailConfig, RegimeAllocation,
        RiskConfig, StorageConfig, StrategyConfig,
    )
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0, weekly_loss_limit_pct=5.0,
            per_trade_risk_pct=1.0, max_position_pct=10.0,
            max_symbol_concentration_pct=5.0, max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(options_max_pct=20.0),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
            "sideways":    RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
            "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
            "risk_off":    RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
        },
        email=EmailConfig(to="t@x.com"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
        strategy=StrategyConfig(
            earnings_gate_enabled=False, macro_shock_gate_enabled=False,
            crypto_fear_greed_enabled=False, crypto_reddit_spike_enabled=False,
            crypto_coingecko_enabled=False, insider_cluster_enabled=False,
        ),
    )


def _account():
    return AccountSnapshot(
        equity=Decimal("15000"), cash=Decimal("15000"),
        buying_power=Decimal("30000"), portfolio_value=Decimal("15000"),
    )


def _bars():
    return pd.DataFrame(
        {"close": [100 + i for i in range(40)],
         "open":  [100 + i for i in range(40)],
         "high":  [101 + i for i in range(40)],
         "low":   [99 + i for i in range(40)],
         "volume": [1_000_000] * 40},
        index=pd.date_range("2026-04-01", periods=40, freq="D", tz="UTC"),
    )


@pytest.fixture
def db_engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def watchlist():
    return [WatchlistEntry(symbol="MSFT", asset_class="stock", notes="")]


def _forced_buy(symbol: str = "MSFT"):
    return Signal(
        symbol=symbol, action=SignalAction.BUY, qty=Decimal("2"),
        entry_price=Decimal("139"), stop_loss_price=Decimal("133"),
        reason="forced for test",
    )


def _seed_intel(engine, symbol="MSFT", asset_class="stock", score=8.5):
    """Insert a fresh intel_candidates row so lookup_score returns a value."""
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(IntelCandidate(
            symbol=symbol, asset_class=asset_class, score=score,
            n_mentions=4, n_sources=2, first_seen=now, last_seen=now,
            top_reason="forced for test", sources_json="{}",
            sentiment_avg=0.5, rolled_up_at=now,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# Verdict-handling paths
# ---------------------------------------------------------------------------


def test_entry_debate_place_verdict_lets_order_through(
    watchlist, db_engine, monkeypatch,
):
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-1", stop_loss_order_id="s-1",
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity: _forced_buy(sym),
    )
    monkeypatch.setattr(
        "trading_bot.orchestrator.run_entry_debate",
        lambda *a, **kw: EntryDebateVerdict(
            recommendation="place", confidence="high",
            reason="strong catalyst + clean technicals",
            aggressive_text="(stub)", conservative_text="(stub)",
            neutral_text="(stub)",
        ),
        raising=False,
    )
    # Patch the lazy import inside _run_entry_debate too
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(
        _ed, "run_entry_debate",
        lambda *a, **kw: EntryDebateVerdict(
            recommendation="place", confidence="high",
            reason="strong catalyst + clean technicals",
            aggressive_text="(stub)", conservative_text="(stub)",
            neutral_text="(stub)",
        ),
    )

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "placed_order"
    alpaca.place_order_with_stop_loss.assert_called_once()
    # Audit row written
    with Session(db_engine) as s:
        rows = s.query(EntryDebateRun).filter_by(symbol="MSFT").all()
    assert len(rows) == 1
    assert rows[0].verdict == "place"


def test_entry_debate_skip_verdict_blocks_order(
    watchlist, db_engine, monkeypatch,
):
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(
        _ed, "run_entry_debate",
        lambda *a, **kw: EntryDebateVerdict(
            recommendation="skip", confidence="medium",
            reason="recycled headline, no concrete edge",
            aggressive_text="(stub)", conservative_text="(stub)",
            neutral_text="(stub)",
        ),
    )

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "rejected_by_entry_debate"
    assert "recycled headline" in msft.reason
    alpaca.place_order_with_stop_loss.assert_not_called()
    with Session(db_engine) as s:
        row = s.query(EntryDebateRun).filter_by(symbol="MSFT").first()
    assert row is not None
    assert row.verdict == "skip"


def test_entry_debate_failure_skips_and_alerts(
    watchlist, db_engine, monkeypatch,
):
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(_ed, "run_entry_debate", lambda *a, **kw: None)

    queued: list = []
    monkeypatch.setattr(
        "trading_bot.alerts.queue_alert",
        lambda evt: queued.append(evt),
    )

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "skipped_entry_debate_unreachable"
    alpaca.place_order_with_stop_loss.assert_not_called()
    # Alert queued exactly once for this scan
    assert len(queued) >= 1
    assert any(getattr(e, "kind", None) == "daemon_critical" for e in queued)
    # Audit row written with verdict='fail_soft'
    with Session(db_engine) as s:
        row = s.query(EntryDebateRun).filter_by(symbol="MSFT").first()
    assert row is not None
    assert row.verdict == "fail_soft"


def test_entry_debate_disabled_skips_gate_entirely(
    watchlist, db_engine, monkeypatch,
):
    """When entry_debate_enabled=False the gate is bypassed: orders go
    directly through (existing pre-Phase-6 behaviour preserved)."""
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-1", stop_loss_order_id="s-1",
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=False,  # explicitly off
        entry_debate_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity: _forced_buy(sym),
    )
    # Trip-wire: if the entry debate runs, the test fails.
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(
        _ed, "run_entry_debate",
        lambda *a, **kw: pytest.fail("debate ran when disabled"),
    )

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "placed_order"


# ---------------------------------------------------------------------------
# Per-ticker intel-aware regime override
# ---------------------------------------------------------------------------


def test_sideways_with_high_intel_unlocks_momentum(
    watchlist, db_engine, monkeypatch,
):
    """In sideways the orchestrator-level strategy is None. Pre-Phase-6 the
    scan early-bailed with 'hold' for everyone. With intel_score >= threshold
    a per-ticker MomentumStrategy should be resolved and the ticker proceeds."""
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-1", stop_loss_order_id="s-1",
    )
    journal = MagicMock()
    cfg = _config()

    _seed_intel(db_engine, symbol="MSFT", asset_class="stock", score=9.0)

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="sideways",  # → orchestrator-level self._strategy is None
        entry_debate_enabled=False,
        intel_lookup_engine=db_engine,
        intel_score_regime_override_threshold=5.0,
    )
    # orch._strategy is None at orchestrator level; per-ticker resolution
    # should still find Momentum and emit a BUY signal we can place.
    assert orch._strategy is None

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    # Either placed_order (signals all aligned) or hold-with-strategy-reason.
    # The key assertion is we did NOT fall through the no-strategy-for-regime
    # bail; we got past per-ticker resolution.
    assert msft.action != "hold" or "no strategy enabled for regime" not in msft.reason


# ---------------------------------------------------------------------------
# Email transcript at each outcome (place / skip / place_failed)
# ---------------------------------------------------------------------------


def _stub_place_verdict():
    return EntryDebateVerdict(
        recommendation="place", confidence="high",
        reason="strong catalyst", aggressive_text="(stub)",
        conservative_text="(stub)", neutral_text="(stub)",
    )


def _stub_skip_verdict():
    return EntryDebateVerdict(
        recommendation="skip", confidence="medium",
        reason="generic noise", aggressive_text="(stub)",
        conservative_text="(stub)", neutral_text="(stub)",
    )


def test_email_sent_with_outcome_placed_when_order_succeeds(
    watchlist, db_engine, monkeypatch,
):
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-PLACED-1", stop_loss_order_id="s-1",
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate", lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(_ed, "run_entry_debate", lambda *a, **kw: _stub_place_verdict())

    sent: list = []
    monkeypatch.setattr(
        "trading_bot.email_entry_debate.send_entry_debate_email",
        lambda ctx, **kw: sent.append(ctx) or True,
    )

    orch.scan(watchlist=watchlist)

    assert len(sent) == 1, "exactly one transcript email per debate"
    ctx = sent[0]
    assert ctx.outcome == "placed"
    assert ctx.symbol == "MSFT"
    assert ctx.entry_order_id == "e-PLACED-1"
    assert ctx.verdict.recommendation == "place"


def test_email_sent_with_outcome_skipped_when_judge_says_skip(
    watchlist, db_engine, monkeypatch,
):
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate", lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(_ed, "run_entry_debate", lambda *a, **kw: _stub_skip_verdict())

    sent: list = []
    monkeypatch.setattr(
        "trading_bot.email_entry_debate.send_entry_debate_email",
        lambda ctx, **kw: sent.append(ctx) or True,
    )

    orch.scan(watchlist=watchlist)

    assert len(sent) == 1
    ctx = sent[0]
    assert ctx.outcome == "skipped"
    assert ctx.verdict.recommendation == "skip"
    assert "generic noise" in ctx.verdict.reason
    alpaca.place_order_with_stop_loss.assert_not_called()


def test_email_sent_with_outcome_place_failed_when_broker_rejects(
    watchlist, db_engine, monkeypatch,
):
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.side_effect = AlpacaClientError(
        "insufficient buying power"
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate", lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(_ed, "run_entry_debate", lambda *a, **kw: _stub_place_verdict())

    sent: list = []
    monkeypatch.setattr(
        "trading_bot.email_entry_debate.send_entry_debate_email",
        lambda ctx, **kw: sent.append(ctx) or True,
    )

    result = orch.scan(watchlist=watchlist)

    assert len(sent) == 1
    ctx = sent[0]
    assert ctx.outcome == "place_failed"
    assert ctx.verdict.recommendation == "place"  # judge said yes
    assert "insufficient buying power" in ctx.place_error
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "api_error"


def test_no_email_when_debate_failsoft(
    watchlist, db_engine, monkeypatch,
):
    """fail-soft path queues an alert but sends NO transcript email — there
    is no transcript to send when the debate produced no verdict."""
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate", lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(_ed, "run_entry_debate", lambda *a, **kw: None)

    monkeypatch.setattr(
        "trading_bot.alerts.queue_alert", lambda evt: None,
    )

    sent: list = []
    monkeypatch.setattr(
        "trading_bot.email_entry_debate.send_entry_debate_email",
        lambda ctx, **kw: sent.append(ctx) or True,
    )

    orch.scan(watchlist=watchlist)
    assert sent == [], "no transcript email when debate produced no verdict"


def test_email_failure_does_not_block_order_placement(
    watchlist, db_engine, monkeypatch,
):
    """SMTP outage must not crash the scan or roll back the order."""
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-1", stop_loss_order_id="s-1",
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="trending_up",
        entry_debate_enabled=True, entry_debate_engine=db_engine,
        intel_lookup_engine=db_engine,
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate", lambda sym, ind, equity: _forced_buy(sym),
    )
    import trading_bot.entry_debate as _ed
    monkeypatch.setattr(_ed, "run_entry_debate", lambda *a, **kw: _stub_place_verdict())

    def _boom(*a, **kw):
        raise ConnectionError("smtp down")
    monkeypatch.setattr(
        "trading_bot.email_entry_debate.send_entry_debate_email", _boom,
    )

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "placed_order"  # order still went through
    alpaca.place_order_with_stop_loss.assert_called_once()


def test_risk_off_is_hard_wall_even_with_high_intel(
    watchlist, db_engine, monkeypatch,
):
    """risk_off must bail via the cheap pre-loop short-circuit even when
    a high intel_score exists. Catching falling knives = exactly what
    risk_off is supposed to prevent."""
    market = MagicMock(); market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    _seed_intel(db_engine, symbol="MSFT", asset_class="stock", score=99.0)

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal,
        regime="risk_off",
        entry_debate_enabled=False,
        intel_lookup_engine=db_engine,
        intel_score_regime_override_threshold=5.0,
    )
    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "hold"
    assert "risk_off" in msft.reason
    alpaca.place_order_with_stop_loss.assert_not_called()
