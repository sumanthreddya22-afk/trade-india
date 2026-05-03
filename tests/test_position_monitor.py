"""Phase C — Position Monitor + alpaca helpers tests.

Covers:
  * Trigger classifier (hard + soft triggers, priority order)
  * _count_new_negative_events / _has_fresh_event SQL helpers
  * _compute_tightened_stop (breakeven floor / 1% trailing in profit)
  * Sequential per-position loop in PositionMonitorRole._do_work
  * Hold-debate verdict → action mapping (hold / tighten_stop / exit_now)
  * Fail-soft: monitor never raises; LLM error leaves bracket untouched
  * Daily cap stops further debates mid-tick
  * AlpacaClient.replace_stop / flatten_position happy paths + error cases
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from trading_bot import hold_debate
from trading_bot.hold_debate import HoldDebateVerdict
from trading_bot.roles import position_monitor
from trading_bot.roles.position_monitor import PositionMonitorRole
from trading_bot.state_db import (
    Base, HoldDebateRun, IntelCandidate, IntelEvent,
    TradeIntelSnapshot, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Trigger classifier
# ---------------------------------------------------------------------------


def test_classify_no_triggers_returns_empty():
    fired = position_monitor._classify_triggers(
        entry_score=10.0, current_score=9.0,
        entry_sentiment=0.6, current_sentiment=0.5,
        n_new_negative=0, has_fresh_8k=False, has_vip_high=False,
    )
    assert fired == []


def test_classify_score_drop_fires_at_50pct():
    fired = position_monitor._classify_triggers(
        entry_score=10.0, current_score=4.9,  # 51% drop
        entry_sentiment=None, current_sentiment=None,
        n_new_negative=0, has_fresh_8k=False, has_vip_high=False,
        score_drop_threshold=0.5,
    )
    assert "score_drop" in fired


def test_classify_score_drop_below_threshold_no_fire():
    fired = position_monitor._classify_triggers(
        entry_score=10.0, current_score=6.0,  # 40% drop
        entry_sentiment=None, current_sentiment=None,
        n_new_negative=0, has_fresh_8k=False, has_vip_high=False,
        score_drop_threshold=0.5,
    )
    assert "score_drop" not in fired


def test_classify_sentiment_flip_fires():
    fired = position_monitor._classify_triggers(
        entry_score=None, current_score=None,
        entry_sentiment=0.6, current_sentiment=-0.5,
        n_new_negative=0, has_fresh_8k=False, has_vip_high=False,
    )
    assert "sentiment_flip" in fired


def test_classify_sentiment_flip_no_fire_when_only_partial():
    """Entry must be ≥ +threshold AND current must be ≤ -threshold."""
    fired = position_monitor._classify_triggers(
        entry_score=None, current_score=None,
        entry_sentiment=0.2, current_sentiment=-0.5,  # entry below +0.3
        n_new_negative=0, has_fresh_8k=False, has_vip_high=False,
    )
    assert "sentiment_flip" not in fired


def test_classify_negative_news_cluster_fires():
    fired = position_monitor._classify_triggers(
        entry_score=None, current_score=None,
        entry_sentiment=None, current_sentiment=None,
        n_new_negative=4, has_fresh_8k=False, has_vip_high=False,
        negative_news_count=3,
    )
    assert "negative_news_cluster" in fired


def test_classify_8k_hard_trigger_fires_first():
    fired = position_monitor._classify_triggers(
        entry_score=None, current_score=None,
        entry_sentiment=None, current_sentiment=None,
        n_new_negative=0, has_fresh_8k=True, has_vip_high=False,
    )
    assert fired[0] == "8k_hard_trigger"


def test_classify_vip_hard_trigger():
    fired = position_monitor._classify_triggers(
        entry_score=None, current_score=None,
        entry_sentiment=None, current_sentiment=None,
        n_new_negative=0, has_fresh_8k=False, has_vip_high=True,
    )
    assert "vip_high_severity" in fired


def test_classify_hard_triggers_take_priority_over_soft():
    fired = position_monitor._classify_triggers(
        entry_score=10.0, current_score=4.0,    # would fire score_drop
        entry_sentiment=0.6, current_sentiment=-0.6,
        n_new_negative=5, has_fresh_8k=True, has_vip_high=False,
    )
    # Hard trigger first
    assert fired[0] == "8k_hard_trigger"
    # Soft triggers also present
    assert "score_drop" in fired
    assert "sentiment_flip" in fired


def test_classify_8k_hard_trigger_can_be_disabled():
    fired = position_monitor._classify_triggers(
        entry_score=None, current_score=None,
        entry_sentiment=None, current_sentiment=None,
        n_new_negative=0, has_fresh_8k=True, has_vip_high=False,
        enable_8k_hard_trigger=False,
    )
    assert fired == []


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _add_event(engine, **kwargs):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    defaults = dict(
        symbol="A", asset_class="stock", source="alpaca_news",
        headline="x", url=f"https://x/{id(kwargs)}",
        ingested_at=now, event_at=now,
        event_hash=f"h{id(kwargs)}",
    )
    defaults.update(kwargs)
    with Session(engine) as s:
        s.add(IntelEvent(**defaults))
        s.commit()


def test_count_new_negative_events_filters_by_sentiment(engine):
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=1)
    _add_event(engine, symbol="NVDA", sentiment=-0.5, ingested_at=now,
               event_hash="n1", url="https://n1")
    _add_event(engine, symbol="NVDA", sentiment=-0.3, ingested_at=now,
               event_hash="n2", url="https://n2")
    _add_event(engine, symbol="NVDA", sentiment=-0.2, ingested_at=now,
               event_hash="n3", url="https://n3")  # below threshold
    _add_event(engine, symbol="NVDA", sentiment=0.5, ingested_at=now,
               event_hash="n4", url="https://n4")  # positive
    n = position_monitor._count_new_negative_events(engine, symbol="NVDA", since=cutoff)
    assert n == 2


def test_count_new_negative_events_respects_since(engine):
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(hours=24)
    cutoff = now - dt.timedelta(hours=1)
    _add_event(engine, symbol="NVDA", sentiment=-0.5, ingested_at=old,
               event_hash="o1", url="https://o1")
    n = position_monitor._count_new_negative_events(engine, symbol="NVDA", since=cutoff)
    assert n == 0


def test_has_fresh_event_finds_8k(engine):
    now = dt.datetime.now(dt.timezone.utc)
    _add_event(engine, symbol="NVDA", source="sec_8k", ingested_at=now,
               event_hash="8k1", url="https://8k1")
    assert position_monitor._has_fresh_event(
        engine, symbol="NVDA", source="sec_8k", lookback_minutes=60, now=now,
    ) is True


def test_has_fresh_event_filters_old(engine):
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(hours=2)
    _add_event(engine, symbol="NVDA", source="sec_8k", ingested_at=old,
               event_hash="8kold", url="https://8kold")
    assert position_monitor._has_fresh_event(
        engine, symbol="NVDA", source="sec_8k", lookback_minutes=30, now=now,
    ) is False


def test_has_fresh_event_severity_filter(engine):
    now = dt.datetime.now(dt.timezone.utc)
    _add_event(engine, symbol="NVDA", source="vip_tweet", ingested_at=now,
               raw_score=1.0, event_hash="vlow", url="https://vlow")
    _add_event(engine, symbol="NVDA", source="vip_tweet", ingested_at=now,
               raw_score=3.0, event_hash="vhigh", url="https://vhigh")
    has = position_monitor._has_fresh_event(
        engine, symbol="NVDA", source="vip_tweet",
        lookback_minutes=60, severity_filter=2.0, now=now,
    )
    assert has is True


# ---------------------------------------------------------------------------
# _compute_tightened_stop
# ---------------------------------------------------------------------------


def test_compute_tightened_stop_at_breakeven_when_underwater():
    p = {"entry_price": 100.0, "current_price": 95.0}
    assert position_monitor._compute_tightened_stop(p) == 100.0


def test_compute_tightened_stop_locks_99pct_in_profit():
    p = {"entry_price": 100.0, "current_price": 110.0}
    out = position_monitor._compute_tightened_stop(p)
    # max(100, 110*0.99) = max(100, 108.9) = 108.9
    assert out == pytest.approx(108.9)


def test_compute_tightened_stop_returns_none_on_missing():
    assert position_monitor._compute_tightened_stop({"entry_price": 100.0}) is None
    assert position_monitor._compute_tightened_stop({}) is None


# ---------------------------------------------------------------------------
# PositionMonitorRole._do_work integration
# ---------------------------------------------------------------------------


def _seed_snapshot(engine, *, entry_order_id="ord-1", symbol="NVDA",
                   entry_score=10.0, entry_sentiment=0.5):
    hold_debate.write_intel_snapshot(
        engine,
        entry_order_id=entry_order_id, symbol=symbol,
        asset_class="stock",
        entry_intel_score=entry_score,
        entry_top_reason="Q3 beat",
        entry_sentiment_avg=entry_sentiment,
        entry_top_sources=["sec_8k", "polygon_news"],
    )


def _seed_candidate(engine, *, symbol="NVDA", score=10.0, sentiment_avg=0.5):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(IntelCandidate(
            symbol=symbol, asset_class="stock",
            score=score, n_mentions=5, n_sources=3,
            first_seen=now, last_seen=now,
            top_reason="x", sources_json="{}",
            sentiment_avg=sentiment_avg, rolled_up_at=now,
        ))
        s.commit()


def _make_position(symbol="NVDA", entry_order_id="ord-1"):
    return {
        "symbol": symbol, "qty": 50, "entry_price": 875.0,
        "current_price": 862.0, "stop_price": 850.0,
        "take_profit_price": 912.5, "asset_class": "stock",
        "entry_order_id": entry_order_id, "days_held": 1,
        "unrealized_pnl_usd": -650.0, "unrealized_pnl_pct": -0.4,
    }


def test_do_work_no_positions_returns_empty(engine):
    role = PositionMonitorRole(
        engine=engine, alpaca_client=None,
        positions_provider=lambda: [],
    )
    out = role._do_work({})
    assert out["n_positions_checked"] == 0
    assert out["n_triggered"] == 0


def test_do_work_no_trigger_emits_nothing(engine):
    """Score barely changed, sentiment stable, no fresh 8-K — no trigger."""
    _seed_snapshot(engine, entry_score=10.0, entry_sentiment=0.5)
    _seed_candidate(engine, symbol="NVDA", score=9.0, sentiment_avg=0.4)
    role = PositionMonitorRole(
        engine=engine, alpaca_client=MagicMock(),
        positions_provider=lambda: [_make_position()],
    )
    out = role._do_work({})
    assert out["n_triggered"] == 0
    # No hold-debate row written
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(HoldDebateRun).all()
    assert len(rows) == 0


def test_do_work_8k_trigger_runs_debate_and_flattens(engine):
    """Fresh 8-K → debate fires → exit_now → flatten_position called."""
    _seed_snapshot(engine, entry_score=10.0, entry_sentiment=0.5)
    _seed_candidate(engine, symbol="NVDA", score=4.0, sentiment_avg=-0.4)
    _add_event(engine, symbol="NVDA", source="sec_8k",
               ingested_at=dt.datetime.now(dt.timezone.utc),
               event_hash="freshk", url="https://freshk")
    fake_alpaca = MagicMock()
    fake_alpaca.flatten_position.return_value = SimpleNamespace(
        flatten_order_id="flat-1", cancelled_child_order_ids=["s1", "tp1"], qty=50.0,
    )
    fake_verdict = HoldDebateVerdict(
        recommendation="exit_now", confidence="high",
        reason="catalyst inverted", aggressive_text="a",
        conservative_text="c", neutral_text="n",
    )
    role = PositionMonitorRole(
        engine=engine, alpaca_client=fake_alpaca,
        positions_provider=lambda: [_make_position()],
    )
    with patch(
        "trading_bot.hold_debate.run_hold_debate", return_value=fake_verdict,
    ):
        out = role._do_work({})
    assert out["n_triggered"] == 1
    assert out["n_acted"] == 1
    fake_alpaca.flatten_position.assert_called_once_with(symbol="NVDA")
    # Audit row written with action_taken='flattened'
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(HoldDebateRun).all()
    assert len(rows) == 1
    assert rows[0].action_taken == "flattened"
    assert rows[0].verdict == "exit_now"
    assert rows[0].trigger_reason == "8k_hard_trigger"


def test_do_work_tighten_stop_calls_replace_stop(engine):
    _seed_snapshot(engine, entry_score=10.0, entry_sentiment=0.5)
    _seed_candidate(engine, symbol="NVDA", score=4.0, sentiment_avg=-0.4)  # score_drop fires
    fake_alpaca = MagicMock()
    fake_verdict = HoldDebateVerdict(
        recommendation="tighten_stop", confidence="medium",
        reason="protect gains", aggressive_text="a",
        conservative_text="c", neutral_text="n",
    )
    # In-profit position
    pos = _make_position()
    pos["current_price"] = 900.0  # entry 875, current 900 → in profit
    role = PositionMonitorRole(
        engine=engine, alpaca_client=fake_alpaca,
        positions_provider=lambda: [pos],
    )
    with patch("trading_bot.hold_debate.run_hold_debate", return_value=fake_verdict):
        out = role._do_work({})
    assert out["n_acted"] == 1
    # Should use max(875, 900*0.99=891) = 891
    fake_alpaca.replace_stop.assert_called_once()
    call = fake_alpaca.replace_stop.call_args
    assert call.kwargs["symbol"] == "NVDA"
    assert call.kwargs["new_stop_price"] == pytest.approx(891.0)


def test_do_work_hold_verdict_takes_no_action(engine):
    _seed_snapshot(engine, entry_score=10.0, entry_sentiment=0.5)
    _seed_candidate(engine, symbol="NVDA", score=4.0, sentiment_avg=-0.4)
    fake_alpaca = MagicMock()
    fake_verdict = HoldDebateVerdict(
        recommendation="hold", confidence="high",
        reason="thesis intact", aggressive_text="a",
        conservative_text="c", neutral_text="n",
    )
    role = PositionMonitorRole(
        engine=engine, alpaca_client=fake_alpaca,
        positions_provider=lambda: [_make_position()],
    )
    with patch("trading_bot.hold_debate.run_hold_debate", return_value=fake_verdict):
        out = role._do_work({})
    assert out["n_acted"] == 0
    fake_alpaca.flatten_position.assert_not_called()
    fake_alpaca.replace_stop.assert_not_called()
    # Audit row still written with action_taken='none'
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(HoldDebateRun).all()
    assert rows[0].action_taken == "none"


def test_do_work_fail_soft_leaves_position_untouched(engine):
    """LLM error → verdict None → no action, but audit row written with verdict='fail_soft'."""
    _seed_snapshot(engine, entry_score=10.0, entry_sentiment=0.5)
    _seed_candidate(engine, symbol="NVDA", score=4.0, sentiment_avg=-0.4)
    fake_alpaca = MagicMock()
    role = PositionMonitorRole(
        engine=engine, alpaca_client=fake_alpaca,
        positions_provider=lambda: [_make_position()],
    )
    with patch("trading_bot.hold_debate.run_hold_debate", return_value=None):
        out = role._do_work({})
    assert out["n_triggered"] == 1
    assert out["n_acted"] == 0
    fake_alpaca.flatten_position.assert_not_called()
    fake_alpaca.replace_stop.assert_not_called()
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(HoldDebateRun).all()
    assert rows[0].verdict == "fail_soft"


def test_do_work_daily_cap_stops_further_debates(engine):
    """Once daily cap is hit mid-tick, subsequent triggered positions are skipped."""
    # Pre-fill the cap: 1 row already written today
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(HoldDebateRun(
            run_at=now, asset_class="stock", symbol="OTHER",
            verdict="hold", confidence="high",
        ))
        s.commit()
    _seed_snapshot(engine, entry_order_id="ord-1", symbol="NVDA")
    _seed_candidate(engine, symbol="NVDA", score=4.0, sentiment_avg=-0.4)

    settings = SimpleNamespace(hold_debate_daily_cap=1)
    role = PositionMonitorRole(
        engine=engine, alpaca_client=MagicMock(), settings=settings,
        positions_provider=lambda: [_make_position()],
    )
    with patch("trading_bot.hold_debate.run_hold_debate") as run_debate:
        out = role._do_work({})
    # Cap was already at 1, so no new debate fires
    run_debate.assert_not_called()
    assert out["per_symbol"][0].get("skipped_reason", "").startswith("daily_cap_reached")


# ---------------------------------------------------------------------------
# AlpacaClient.replace_stop / flatten_position
# ---------------------------------------------------------------------------


def _build_alpaca_client_with_mock_sdk():
    """Build an AlpacaClient with a fake underlying SDK. Skip the real
    client init by directly setting _client."""
    from trading_bot.shared.alpaca_client import AlpacaClient
    client = AlpacaClient.__new__(AlpacaClient)
    client._client = MagicMock()
    return client


def test_replace_stop_cancels_old_and_submits_new():
    from trading_bot.shared.alpaca_client import AlpacaClientError
    ac = _build_alpaca_client_with_mock_sdk()
    fake_pos = SimpleNamespace(symbol="NVDA", qty="50")
    ac._client.get_all_positions.return_value = [fake_pos]
    fake_stop = SimpleNamespace(id="stop-old", symbol="NVDA", type="stop")
    ac._client.get_orders.return_value = [fake_stop]
    new_stop_resp = SimpleNamespace(id="stop-new")
    ac._client.submit_order.return_value = new_stop_resp

    out = ac.replace_stop(symbol="NVDA", new_stop_price=860.0)
    ac._client.cancel_order_by_id.assert_called_once_with("stop-old")
    assert out.symbol == "NVDA"
    assert out.cancelled_order_ids == ["stop-old"]
    assert out.new_stop_order_id == "stop-new"
    assert out.new_stop_price == 860.0


def test_replace_stop_raises_when_no_position():
    from trading_bot.shared.alpaca_client import AlpacaClientError
    ac = _build_alpaca_client_with_mock_sdk()
    ac._client.get_all_positions.return_value = []
    with pytest.raises(AlpacaClientError, match="no open position"):
        ac.replace_stop(symbol="NVDA", new_stop_price=860.0)


def test_replace_stop_raises_when_no_stop_order():
    from trading_bot.shared.alpaca_client import AlpacaClientError
    ac = _build_alpaca_client_with_mock_sdk()
    ac._client.get_all_positions.return_value = [
        SimpleNamespace(symbol="NVDA", qty="50"),
    ]
    ac._client.get_orders.return_value = []  # no open stops
    with pytest.raises(AlpacaClientError, match="no open stop order"):
        ac.replace_stop(symbol="NVDA", new_stop_price=860.0)


def test_flatten_position_cancels_children_and_submits_market_sell():
    ac = _build_alpaca_client_with_mock_sdk()
    fake_pos = SimpleNamespace(symbol="NVDA", qty="50")
    ac._client.get_all_positions.return_value = [fake_pos]
    fake_orders = [
        SimpleNamespace(id="stop-1", symbol="NVDA", type="stop"),
        SimpleNamespace(id="tp-1", symbol="NVDA", type="limit"),
        SimpleNamespace(id="other-1", symbol="MSFT", type="limit"),  # different sym
    ]
    ac._client.get_orders.return_value = fake_orders
    flatten_resp = SimpleNamespace(id="flat-1")
    ac._client.submit_order.return_value = flatten_resp

    out = ac.flatten_position(symbol="NVDA")
    # Cancelled both NVDA child orders, NOT the MSFT order
    cancel_calls = [c.args[0] for c in ac._client.cancel_order_by_id.call_args_list]
    assert set(cancel_calls) == {"stop-1", "tp-1"}
    assert out.symbol == "NVDA"
    assert out.flatten_order_id == "flat-1"
    assert out.qty == 50.0


def test_flatten_position_raises_when_no_position():
    from trading_bot.shared.alpaca_client import AlpacaClientError
    ac = _build_alpaca_client_with_mock_sdk()
    ac._client.get_all_positions.return_value = []
    with pytest.raises(AlpacaClientError, match="no open position"):
        ac.flatten_position(symbol="NVDA")


def test_flatten_position_continues_when_child_cancel_fails():
    """A child-cancel failure shouldn't block the flatten — we still try
    the market sell so the position doesn't sit unprotected."""
    ac = _build_alpaca_client_with_mock_sdk()
    fake_pos = SimpleNamespace(symbol="NVDA", qty="50")
    ac._client.get_all_positions.return_value = [fake_pos]
    fake_orders = [
        SimpleNamespace(id="stop-1", symbol="NVDA", type="stop"),
    ]
    ac._client.get_orders.return_value = fake_orders
    ac._client.cancel_order_by_id.side_effect = ConnectionError("dns")
    ac._client.submit_order.return_value = SimpleNamespace(id="flat-1")

    out = ac.flatten_position(symbol="NVDA")
    # Cancel failed silently, but flatten still happened
    assert out.flatten_order_id == "flat-1"
