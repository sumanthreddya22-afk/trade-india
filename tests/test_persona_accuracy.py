"""Tests for the per-persona accuracy aggregator."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import (
    EntryDebateRunCrypto,
    HoldDebateRunCrypto,
    ScoutDebateRunCrypto,
)
from trading_bot.pipelines.options.state_db import (
    ScoutDebateRunOptions,
    WheelCycleOptions,
    WheelDebateRunOptions,
)
from trading_bot.shared.persona_accuracy import (
    PersonaStats,
    _split_prompt_version,
    compute_persona_stats,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# _split_prompt_version
# ---------------------------------------------------------------------------


def test_split_prompt_version_handles_full_form():
    parsed = _split_prompt_version("crypto_scout/skeptic=v1,analyst=v2,judge=v3")
    assert parsed == {"skeptic": "v1", "analyst": "v2", "judge": "v3"}


def test_split_prompt_version_handles_empty():
    assert _split_prompt_version("") == {}


def test_split_prompt_version_handles_no_prefix():
    parsed = _split_prompt_version("aggressive=v1,judge=v1")
    assert parsed == {"aggressive": "v1", "judge": "v1"}


# ---------------------------------------------------------------------------
# PersonaStats.hit_rate_pct
# ---------------------------------------------------------------------------


def test_hit_rate_none_when_no_outcomes():
    stats = PersonaStats(debate_role="x", pipeline="crypto", n_verdicts=10)
    assert stats.hit_rate_pct is None


def test_hit_rate_three_quarters():
    stats = PersonaStats(
        debate_role="x", pipeline="crypto",
        n_verdicts=10, n_outcomes_known=4, n_correct=3,
    )
    assert stats.hit_rate_pct == 75.0


# ---------------------------------------------------------------------------
# compute_persona_stats — empty DB returns empty dict
# ---------------------------------------------------------------------------


def test_compute_returns_empty_when_no_audit_rows(engine):
    result = compute_persona_stats(engine, lookback_days=30)
    assert result == {}


# ---------------------------------------------------------------------------
# compute_persona_stats — crypto attribution
# ---------------------------------------------------------------------------


def test_crypto_scout_attributes_three_personas(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        session.add(ScoutDebateRunCrypto(
            run_at=now, symbol="ETH/USD",
            verdict="elevate", confidence="high",
            judge_reason="ok", prompt_version="crypto_scout/skeptic=v1,analyst=v1,judge=v1",
        ))
        session.commit()

    result = compute_persona_stats(engine, lookback_days=30, now=now)
    assert ("crypto", "scout_skeptic") in result
    assert ("crypto", "scout_analyst") in result
    assert ("crypto", "scout_judge") in result
    assert result[("crypto", "scout_skeptic")].n_runs == 1
    assert result[("crypto", "scout_judge")].n_verdicts == 1


def test_crypto_hold_judge_correctness_from_pnl(engine):
    """exit_now with pnl_pct > -5 = correct (avoided big loss)."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        session.add(HoldDebateRunCrypto(
            run_at=now, symbol="BTC/USD",
            trigger_reason="big_drop",
            verdict="exit_now", confidence="high",
            judge_reason="ok",
            resulting_pnl_pct=-2.0,  # avoided big loss
            prompt_version="crypto_hold/aggressive=v1,conservative=v1,neutral=v1,judge=v1",
        ))
        session.add(HoldDebateRunCrypto(
            run_at=now, symbol="ETH/USD",
            trigger_reason="big_drop",
            verdict="hold", confidence="high",
            judge_reason="ok",
            resulting_pnl_pct=-3.0,  # held but lost — incorrect
            prompt_version="crypto_hold/aggressive=v1,conservative=v1,neutral=v1,judge=v1",
        ))
        session.commit()

    result = compute_persona_stats(engine, lookback_days=30, now=now)
    judge = result[("crypto", "hold_judge")]
    assert judge.n_verdicts == 2
    assert judge.n_outcomes_known == 2
    assert judge.n_correct == 1
    assert judge.hit_rate_pct == 50.0


def test_old_runs_excluded_by_lookback(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        session.add(ScoutDebateRunCrypto(
            run_at=now - dt.timedelta(days=60),
            symbol="ETH/USD", verdict="elevate", confidence="high",
            judge_reason="old", prompt_version="crypto_scout/skeptic=v1",
        ))
        session.commit()
    result = compute_persona_stats(engine, lookback_days=30, now=now)
    assert result == {}


# ---------------------------------------------------------------------------
# compute_persona_stats — options attribution
# ---------------------------------------------------------------------------


def test_options_wheel_judge_correctness_from_cycle_pnl(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        cycle_winning = WheelCycleOptions(
            underlying="AAPL",
            state="closed",
            started_at=now - dt.timedelta(days=10),
            ended_at=now - dt.timedelta(days=1),
            cumulative_premium=2.5, realized_pnl=3.0,  # win
        )
        cycle_losing = WheelCycleOptions(
            underlying="TSLA",
            state="closed",
            started_at=now - dt.timedelta(days=10),
            ended_at=now - dt.timedelta(days=1),
            cumulative_premium=2.5, realized_pnl=-2.0,  # loss
        )
        session.add_all([cycle_winning, cycle_losing])
        session.commit()
        win_id = cycle_winning.id
        loss_id = cycle_losing.id

        for cyc_id in (win_id, loss_id):
            session.add(WheelDebateRunOptions(
                run_at=now,
                underlying="AAPL" if cyc_id == win_id else "TSLA",
                verdict="place", confidence="high",
                judge_reason="ok", cycle_id=cyc_id,
                prompt_version="options_wheel/aggressive=v1,conservative=v1,neutral=v1,judge=v1",
            ))
        session.commit()

    result = compute_persona_stats(engine, lookback_days=30, now=now)
    assert ("options", "wheel_judge") in result
    judge = result[("options", "wheel_judge")]
    assert judge.n_verdicts == 2
    assert judge.n_outcomes_known == 2
    assert judge.n_correct == 1
    assert judge.hit_rate_pct == 50.0


def test_options_scout_runs_attributed(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        session.add(ScoutDebateRunOptions(
            run_at=now, underlying="AAPL",
            verdict="elevate", confidence="high",
            judge_reason="ok",
            prompt_version="options_scout/skeptic=v1,analyst=v1,judge=v1",
        ))
        session.commit()
    result = compute_persona_stats(engine, lookback_days=30, now=now)
    assert ("options", "scout_skeptic") in result
    assert ("options", "scout_analyst") in result
    assert ("options", "scout_judge") in result


def test_pipeline_isolation(engine):
    """Same debate_role in two pipelines must not collide."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        session.add(ScoutDebateRunCrypto(
            run_at=now, symbol="ETH/USD",
            verdict="elevate", confidence="high", judge_reason="ok",
            prompt_version="crypto_scout/skeptic=v1,analyst=v1,judge=v1",
        ))
        session.add(ScoutDebateRunOptions(
            run_at=now, underlying="AAPL",
            verdict="elevate", confidence="high", judge_reason="ok",
            prompt_version="options_scout/skeptic=v1,analyst=v1,judge=v1",
        ))
        session.commit()

    result = compute_persona_stats(engine, lookback_days=30, now=now)
    crypto_judge = result[("crypto", "scout_judge")]
    options_judge = result[("options", "scout_judge")]
    assert crypto_judge.n_runs == 1
    assert options_judge.n_runs == 1
    # Distinct keys, distinct counts.
    assert crypto_judge is not options_judge
