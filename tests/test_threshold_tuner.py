"""Tests for trading_bot.roles.threshold_tuner.

Each per-knob rule is tested as a pure function with synthetic inputs.
The end-to-end role test stubs the data loaders to verify dispatch
between auto-mode (writes override) and recommend-mode (skipped until
LLM judge wired). The bound-clamping property is enforced in the
threshold_overrides write helper itself, but here we lock in that the
RULE outputs reasonable values pre-clamp.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.roles.threshold_tuner import (
    KNOBS,
    rule_iv_rank_floor,
    rule_max_position_pct,
    rule_min_annualized_yield,
    rule_min_premium_abs,
    rule_per_trade_risk_pct,
    rule_unblock_daily_debate_cap,
    rule_unblock_max_overage_ratio,
    rule_unblock_min_candidate_score,
    run_threshold_tuner,
)
from trading_bot.state_db import Base, get_engine
from trading_bot.threshold_overrides import lookup, list_active


# ---------------------------------------------------------------------------
# rule_per_trade_risk_pct
# ---------------------------------------------------------------------------


def test_rule_per_trade_risk_pct_insufficient_data():
    # Below 30 trades the rule must skip — gating against noisy small samples
    # is the operator's primary safety property here.
    assert rule_per_trade_risk_pct(win_rates=[1.0] * 5) is None


def test_rule_per_trade_risk_pct_low_win_rate_pins_to_floor():
    # 30% wins → 0.5% risk floor.
    pnls = [1.0] * 9 + [-1.0] * 21  # 30% win rate over 30 trades
    proposed, summary = rule_per_trade_risk_pct(win_rates=pnls)
    assert proposed == pytest.approx(0.5)
    assert summary["n_trades"] == 30


def test_rule_per_trade_risk_pct_mid_win_rate_ramps_up():
    pnls = [1.0] * 15 + [-1.0] * 15  # 50% win rate
    proposed, _ = rule_per_trade_risk_pct(win_rates=pnls)
    assert proposed == pytest.approx(1.0)


def test_rule_per_trade_risk_pct_high_win_rate_pins_to_ceiling():
    pnls = [1.0] * 25 + [-1.0] * 5  # ~83% — above the 70% knee
    proposed, _ = rule_per_trade_risk_pct(win_rates=pnls)
    assert proposed == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# rule_max_position_pct
# ---------------------------------------------------------------------------


def test_rule_max_position_pct_tightens_on_high_dd():
    # Tightening fires even on small samples — DD breach is the signal.
    proposed, summary = rule_max_position_pct(
        current=10.0, max_dd_pct=8.0, n_trades=10,
    )
    assert proposed == pytest.approx(8.0)
    assert summary["bucket"] == "tight"


def test_rule_max_position_pct_loosens_on_low_dd_with_enough_history():
    proposed, summary = rule_max_position_pct(
        current=10.0, max_dd_pct=1.0, n_trades=40,
    )
    assert proposed == pytest.approx(12.0)
    assert summary["bucket"] == "loose"


def test_rule_max_position_pct_does_not_loosen_on_low_history():
    """Loosening requires sustained sample (≥30 trades). Without it we stay
    put — protects the cold-start state from a rosy 5-trade run convincing
    the tuner to upsize."""
    assert rule_max_position_pct(
        current=10.0, max_dd_pct=1.0, n_trades=10,
    ) is None


def test_rule_max_position_pct_dead_band_returns_none():
    """Between 2-5% DD we don't move — avoids constant flipping when noisy."""
    assert rule_max_position_pct(
        current=10.0, max_dd_pct=3.0, n_trades=40,
    ) is None


def test_rule_max_position_pct_clamps_floor():
    proposed, _ = rule_max_position_pct(
        current=5.0, max_dd_pct=10.0, n_trades=40,
    )
    assert proposed == pytest.approx(5.0)  # would go to 3, but clamped


# ---------------------------------------------------------------------------
# rule_iv_rank_floor
# ---------------------------------------------------------------------------


def test_rule_iv_rank_floor_30th_percentile():
    iv_ranks = [10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 55.0]
    proposed, summary = rule_iv_rank_floor(iv_ranks=iv_ranks)
    # 30th pctile of 10 values → idx 2 → 20.0
    assert proposed == pytest.approx(20.0)
    assert summary["n_observations"] == 10


def test_rule_iv_rank_floor_skips_low_sample():
    assert rule_iv_rank_floor(iv_ranks=[20.0] * 5) is None


# ---------------------------------------------------------------------------
# rule_min_premium_abs
# ---------------------------------------------------------------------------


def test_rule_min_premium_abs_half_median():
    bids = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.10]
    proposed, summary = rule_min_premium_abs(recent_bids=bids)
    # median = 0.65 → floor = 0.32 (rounded to 0.33 actually 0.65*0.5 = 0.325 → 0.32)
    assert proposed == pytest.approx(0.32, abs=0.02)
    assert summary["n_bids"] == 10


def test_rule_min_premium_abs_skips_low_sample():
    assert rule_min_premium_abs(recent_bids=[0.5] * 5) is None


# ---------------------------------------------------------------------------
# rule_min_annualized_yield
# ---------------------------------------------------------------------------


def test_rule_min_annualized_yield_tracks_realized():
    # Realized 18% yield → floor = 16.2%
    yields = [0.16, 0.17, 0.18, 0.19, 0.20]
    proposed, summary = rule_min_annualized_yield(realized_yields=yields)
    assert proposed == pytest.approx(0.162, abs=0.005)
    assert summary["n_cycles"] == 5


def test_rule_min_annualized_yield_skips_low_sample():
    assert rule_min_annualized_yield(realized_yields=[0.18] * 3) is None


# ---------------------------------------------------------------------------
# rule_unblock_min_candidate_score
# ---------------------------------------------------------------------------


def test_rule_unblock_min_candidate_score_tightens_when_borderline_loses():
    # 6 borderline (score in [7,8]) place verdicts; only 1 won → 17% win rate
    outcomes = [
        (7.2, -2.0), (7.4, -1.5), (7.6, -3.0),
        (7.8, -1.0), (7.1, 4.0), (7.5, -2.5),
        (8.5, 5.0), (8.7, 3.0), (8.9, 4.0), (8.5, 2.0),  # filler high-score wins
    ]
    proposed, summary = rule_unblock_min_candidate_score(debate_outcomes=outcomes)
    assert proposed == pytest.approx(8.0)
    assert summary["bucket"] == "tighten"


def test_rule_unblock_min_candidate_score_loosens_when_borderline_pays_off():
    outcomes = [
        (7.2, 3.0), (7.4, 4.0), (7.6, 2.5),
        (7.8, 5.0), (7.1, 4.0), (7.5, -1.0),
        (8.5, 5.0), (8.7, 3.0), (8.9, 4.0), (8.5, 2.0),
    ]
    proposed, summary = rule_unblock_min_candidate_score(debate_outcomes=outcomes)
    assert proposed == pytest.approx(6.5)
    assert summary["bucket"] == "loosen"


def test_rule_unblock_min_candidate_score_status_quo():
    # 50% win rate exactly → status quo, no override
    outcomes = [
        (7.2, 1.0), (7.4, -1.0), (7.6, 2.0),
        (7.8, -1.0), (7.1, 1.0), (7.5, -1.0),
        (8.5, 5.0), (8.7, 3.0), (8.9, 4.0), (8.5, 2.0),
    ]
    assert rule_unblock_min_candidate_score(debate_outcomes=outcomes) is None


def test_rule_unblock_min_candidate_score_skips_low_sample():
    assert rule_unblock_min_candidate_score(debate_outcomes=[(7.5, 1.0)] * 3) is None


# ---------------------------------------------------------------------------
# rule_unblock_max_overage_ratio
# ---------------------------------------------------------------------------


def test_rule_unblock_max_overage_ratio_tightens_when_high_overage_loses():
    outcomes = [
        (0.45, -2.0), (0.50, -1.0), (0.55, -3.0),
        (0.60, -1.0), (0.42, 1.0),  # 1/5 wins in high bucket
        (0.20, 2.0), (0.15, 3.0), (0.25, 1.0), (0.10, 2.0), (0.30, 1.5),
    ]
    proposed, summary = rule_unblock_max_overage_ratio(debate_outcomes=outcomes)
    assert proposed == pytest.approx(0.40)
    assert summary["bucket"] == "tighten"


# ---------------------------------------------------------------------------
# rule_unblock_daily_debate_cap
# ---------------------------------------------------------------------------


def test_rule_unblock_daily_debate_cap_subscription_pinned_to_30():
    """Mailbox-billed debates → cost = 0 → cap pinned to 30."""
    proposed, summary = rule_unblock_daily_debate_cap(
        n_debates_30d=100, total_cost_30d_usd=0.0,
    )
    assert proposed == pytest.approx(30.0)
    assert summary["avg_cost_usd"] == 0


def test_rule_unblock_daily_debate_cap_scales_with_cost():
    # 30 debates cost $1.50 → avg $0.05 → budget $1/day → cap 20
    proposed, summary = rule_unblock_daily_debate_cap(
        n_debates_30d=30, total_cost_30d_usd=1.50,
    )
    assert proposed == pytest.approx(20.0)


def test_rule_unblock_daily_debate_cap_skips_low_sample():
    assert rule_unblock_daily_debate_cap(
        n_debates_30d=10, total_cost_30d_usd=0.0,
    ) is None


# ---------------------------------------------------------------------------
# End-to-end role
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


def _seed_iv_history(engine, n: int):
    """Write n option_iv_history rows over the last lookback window. The
    loader computes iv_rank as a per-symbol percentile from the
    atm_iv_30d series, so we vary the IV across rows to produce a real
    percentile distribution."""
    from sqlalchemy.orm import Session
    from trading_bot.state_db import OptionIvHistory
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        # Need >= 10 rows per symbol for the loader to compute a rank.
        # Spread over `n` days with monotonically rising IV so percentile
        # ranks span 0-100.
        for i in range(n):
            s.add(OptionIvHistory(
                symbol="TST",
                recorded_at=now - dt.timedelta(days=n - 1 - i),
                atm_iv_30d=0.10 + (i / n) * 0.40,  # 0.10..0.50
            ))
        s.commit()


def test_run_threshold_tuner_writes_iv_rank_floor_with_iv_history(engine, tmp_path):
    _seed_iv_history(engine, 30)
    proposals = tmp_path / "proposals.json"
    out = run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=proposals,
    )
    knobs_written = {o["knob"] for o in out.overrides_written}
    assert "iv_rank_floor" in knobs_written
    # Verify the override is readable via lookup
    val = lookup(engine, knob="iv_rank_floor")
    assert val is not None
    assert 10.0 <= val <= 50.0  # bounds


def test_run_threshold_tuner_skips_when_no_data(engine, tmp_path):
    """All loaders return empty → role finishes cleanly with all knobs skipped.
    This is the normal first-run state — there's nothing in state.db for the
    tuner to act on, so static YAML stays in effect."""
    proposals = tmp_path / "proposals.json"
    out = run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=proposals,
    )
    assert out.overrides_written == []
    # All knobs should be skipped (auto: insufficient data; recommend: pending)
    skipped_knobs = {s["knob"] for s in out.skipped}
    assert "iv_rank_floor" in skipped_knobs
    assert "per_trade_risk_pct" in skipped_knobs
    # Recommend-mode knobs always skipped until LLM judge wired
    assert "sector_cap_pct" in skipped_knobs


def test_run_threshold_tuner_writes_proposals_json(engine, tmp_path):
    proposals = tmp_path / "proposals.json"
    run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=proposals,
    )
    assert proposals.exists()
    import json as _json
    payload = _json.loads(proposals.read_text())
    assert "overrides_written" in payload
    assert "skipped" in payload
    assert "generated_at" in payload


def test_recommend_mode_knobs_register_as_skipped(engine, tmp_path):
    """sector_cap_pct, options_max_pct, delta_target_*, dte_* all stay
    pending until the LLM judge gate is wired. The role explicitly
    records them as skipped so the operator sees they're known but
    not yet automatable."""
    out = run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
    )
    skipped_knobs = {s["knob"]: s["reason"] for s in out.skipped}
    for knob_name in ("sector_cap_pct", "options_max_pct",
                      "delta_target_low", "dte_min"):
        assert skipped_knobs.get(knob_name) == "recommend_mode_pending_llm_judge"


def test_email_sender_called_when_overrides_written(engine, tmp_path):
    _seed_iv_history(engine, 30)
    sent = []

    class _StubSender:
        def send(self, *, subject, body):
            sent.append((subject, body))

    run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
        sender=_StubSender(),
    )
    assert len(sent) == 1
    subject, body = sent[0]
    assert "threshold tuner" in subject
    assert "iv_rank_floor" in body


def test_email_sender_not_called_when_nothing_to_report(engine, tmp_path):
    sent = []

    class _StubSender:
        def send(self, *, subject, body):
            sent.append((subject, body))

    run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
        sender=_StubSender(),
    )
    # Nothing to report → no email
    assert sent == []


def test_email_failure_does_not_crash_role(engine, tmp_path):
    _seed_iv_history(engine, 30)

    class _BrokenSender:
        def send(self, *, subject, body):
            raise RuntimeError("smtp down")

    # Must not raise
    run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
        sender=_BrokenSender(),
    )


def test_overrides_clamped_to_bounds(engine, tmp_path):
    """Even when the IV history would push p30 above bounds_max, the
    bounds clamp protects the hot path."""
    # Construct a per-symbol history where most observations sit at the top
    # of their own distribution → high percentile ranks → p30 close to 100.
    # Loader requires ≥10 history rows per symbol; we give 12 with the
    # latest-day values dominating so most ranks are ~80-100.
    from sqlalchemy.orm import Session
    from trading_bot.state_db import OptionIvHistory
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for sym_idx in range(8):  # 8 symbols
            for i in range(15):  # 15 days each — enough for ranking
                # First 10 days low IV; last 5 high — most recent observations
                # have rank ≥ 67%.
                iv = 0.05 if i < 10 else 0.50
                s.add(OptionIvHistory(
                    symbol=f"S{sym_idx}",
                    recorded_at=now - dt.timedelta(days=14 - i),
                    atm_iv_30d=iv,
                ))
        s.commit()
    run_threshold_tuner(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
    )
    val = lookup(engine, knob="iv_rank_floor")
    assert val is not None
    # Whatever the rule produced, the lookup must clamp into [10, 50].
    assert 10.0 <= val <= 50.0


def test_kpi_value_returns_run_count(engine, tmp_path):
    """Role exposes a tuner_runs KPI for the dashboard / digest."""
    from trading_bot.roles.threshold_tuner import ThresholdTunerRole
    role = ThresholdTunerRole(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
    )
    name, value, summary = role._kpi_value(30)
    assert name == "tuner_runs"
    assert value == 0  # no runs yet
    assert "0 threshold-tuner runs" in summary


def test_role_safe_run_records_role_run_row(engine, tmp_path):
    from trading_bot.roles.threshold_tuner import ThresholdTunerRole
    from trading_bot.state_db import RoleRun
    from sqlalchemy.orm import Session
    role = ThresholdTunerRole(
        engine=engine,
        closed_trades_db=tmp_path / "missing_trades.db",
        proposals_path=tmp_path / "proposals.json",
    )
    result = role.safe_run({})
    assert result.status.value == "ok"
    with Session(engine) as s:
        n = s.query(RoleRun).filter(RoleRun.role_name == "threshold_tuner").count()
    assert n == 1
