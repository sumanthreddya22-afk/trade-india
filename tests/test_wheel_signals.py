"""Tests for wheel_signals.produce_candidates — signal-driven wheel candidate
sourcing. Bot only acts when news/intel surfaces a reason; never enumerates
the optionable universe and probes Finnhub per name."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.config import WheelConfig
from trading_bot.intelligence_finnhub import EarningsRow
from trading_bot.options.wheel_signals import (
    WheelCandidate, SignalDeps, produce_candidates, _read_last_iv,
)
from trading_bot.state_db import Base, OptionIvHistory


def _seed_iv_history(engine, symbol: str, ivs: list[float]) -> None:
    """Seed N daily IV rows ending today, oldest first."""
    today = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i, iv in enumerate(ivs):
            s.add(OptionIvHistory(
                symbol=symbol,
                recorded_at=today - dt.timedelta(days=len(ivs) - i),
                atm_iv_30d=iv,
            ))
        s.commit()


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'sig.db'}")
    Base.metadata.create_all(e)
    return e


def _deps(engine, *, vix=20.0, finnhub=None,
          sentiment_map=None, today=None):
    """Build a SignalDeps with mocked external IO. Each test overrides what it cares about."""
    macro = MagicMock()
    macro.snapshot.return_value = MagicMock(vix=vix)
    fin = finnhub or MagicMock()
    if not finnhub:
        fin.earnings_calendar.return_value = []
    sent_map = sentiment_map or {}
    return SignalDeps(
        finnhub=fin,
        iv_engine=engine,
        sentiment_for=lambda s: sent_map.get(s),
        macro_snapshotter=macro,
        today=today or dt.date(2026, 4, 29),
    )


def test_returns_empty_when_vix_below_floor(engine):
    cfg = WheelConfig(enabled=True, vix_floor=15, vix_ceiling=30)
    deps = _deps(engine, vix=10.0)
    out = produce_candidates(deps, eligible={"AAPL"}, cfg=cfg)
    assert out == []


def test_returns_empty_when_vix_above_ceiling(engine):
    cfg = WheelConfig(enabled=True, vix_floor=15, vix_ceiling=30)
    deps = _deps(engine, vix=35.0)
    out = produce_candidates(deps, eligible={"AAPL"}, cfg=cfg)
    assert out == []


def test_post_earnings_recent_signal_fires(engine):
    """Symbol with earnings 1-3 days ago AND IV history showing elevated IV
    surfaces as post_earnings_iv_crush candidate."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock()
    fin.earnings_calendar.return_value = [
        EarningsRow(symbol="AAPL", date=dt.date(2026, 4, 27), eps_estimate=1.5),
    ]
    _seed_iv_history(engine, "AAPL",
                     [0.20] * 30 + [0.45])  # baseline 20%, today's IV 45% — elevated
    deps = _deps(engine, finnhub=fin, today=dt.date(2026, 4, 29))
    out = produce_candidates(deps, eligible={"AAPL"}, cfg=cfg)
    assert len(out) == 1
    c = out[0]
    assert c.symbol == "AAPL"
    assert c.signal == "post_earnings_iv_crush"
    assert "earnings" in c.reason.lower()


def test_stable_sentiment_elevated_iv_signal_fires(engine):
    """Symbol with neutral-to-mildly-positive sentiment AND IV rank ≥ floor
    surfaces as stable_elevated_iv candidate (no earnings catalyst)."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock()
    fin.earnings_calendar.return_value = []  # no earnings recently
    _seed_iv_history(engine, "MSFT",
                     [0.18] * 30 + [0.30])  # IV rose to top of 30-day range
    deps = _deps(engine, finnhub=fin,
                 sentiment_map={"MSFT": 0.15})  # mildly positive, stable
    out = produce_candidates(deps, eligible={"MSFT"}, cfg=cfg)
    assert len(out) == 1
    assert out[0].signal == "stable_elevated_iv"
    assert out[0].symbol == "MSFT"


def test_skips_symbol_without_iv_history(engine):
    """No IV history → can't compute rank → cannot surface from non-earnings signals."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    deps = _deps(engine, sentiment_map={"NEW": 0.15})
    out = produce_candidates(deps, eligible={"NEW"}, cfg=cfg)
    assert out == []


def test_skips_symbol_with_negative_sentiment(engine):
    """Sentiment-stable signal requires sentiment in [0, 0.5]. Strongly negative
    or strongly positive sentiment is excluded."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    _seed_iv_history(engine, "BAD", [0.20] * 30 + [0.30])
    deps = _deps(engine, sentiment_map={"BAD": -0.40})  # too negative
    out = produce_candidates(deps, eligible={"BAD"}, cfg=cfg)
    assert out == []


def test_earnings_takes_priority_over_sentiment_signal(engine):
    """When both signals fire, post_earnings_iv_crush is reported (priority 1)."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock()
    fin.earnings_calendar.return_value = [
        EarningsRow(symbol="AAPL", date=dt.date(2026, 4, 27), eps_estimate=1.5),
    ]
    _seed_iv_history(engine, "AAPL", [0.20] * 30 + [0.40])
    deps = _deps(engine, finnhub=fin,
                 sentiment_map={"AAPL": 0.20},  # would also pass stable signal
                 today=dt.date(2026, 4, 29))
    out = produce_candidates(deps, eligible={"AAPL"}, cfg=cfg)
    assert len(out) == 1
    assert out[0].signal == "post_earnings_iv_crush"


def test_blocked_symbol_never_surfaces(engine):
    """Eligible set is the input — caller filters by blocklist before calling."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock()
    fin.earnings_calendar.return_value = [
        EarningsRow(symbol="BLOCKED", date=dt.date(2026, 4, 27), eps_estimate=0.5),
    ]
    deps = _deps(engine, finnhub=fin, today=dt.date(2026, 4, 29))
    out = produce_candidates(deps, eligible=set(), cfg=cfg)
    assert out == []


def test_results_ranked_by_confidence_descending(engine):
    """Multiple candidates returned in descending confidence (highest IV rank first)."""
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock()
    fin.earnings_calendar.return_value = []
    # MSFT history range [0.10, 0.50] with current=0.50 → IV rank ~100
    # AAPL history range [0.10, 0.50] with current=0.30 → IV rank ~50
    _seed_iv_history(engine, "MSFT", [0.10, 0.20, 0.30, 0.40, 0.50] * 6 + [0.50])
    _seed_iv_history(engine, "AAPL", [0.10, 0.20, 0.30, 0.40, 0.50] * 6 + [0.30])
    deps = _deps(engine, finnhub=fin,
                 sentiment_map={"MSFT": 0.10, "AAPL": 0.10})
    out = produce_candidates(deps, eligible={"MSFT", "AAPL"}, cfg=cfg)
    syms = [c.symbol for c in out]
    assert syms == ["MSFT", "AAPL"]
    assert out[0].confidence > out[1].confidence


def test_finnhub_unavailable_does_not_break_other_signals(engine):
    """If Finnhub raises, post-earnings signals are skipped but stable_elevated_iv
    still fires from the IV history that's already local."""
    from trading_bot.intelligence_finnhub import FinnhubUnavailable
    cfg = WheelConfig(enabled=True, iv_rank_floor=30)
    fin = MagicMock()
    fin.earnings_calendar.side_effect = FinnhubUnavailable("rate limited")
    _seed_iv_history(engine, "MSFT", [0.18] * 30 + [0.30])
    deps = _deps(engine, finnhub=fin, sentiment_map={"MSFT": 0.10})
    out = produce_candidates(deps, eligible={"MSFT"}, cfg=cfg)
    assert len(out) == 1
    assert out[0].signal == "stable_elevated_iv"


def test_read_last_iv_returns_most_recent(engine):
    _seed_iv_history(engine, "X", [0.10, 0.15, 0.22])
    last = _read_last_iv(engine, "X")
    assert last == 0.22


def test_read_last_iv_returns_none_when_no_history(engine):
    assert _read_last_iv(engine, "MISSING") is None
