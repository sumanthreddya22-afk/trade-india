"""Position Monitor — Phase C role.

15-min cadence (during US market hours + 30 min before open). For each
open position:
  1. Cheap SQL check — re-fetch intel score from intel_candidates, compare
     to entry baseline (from TradeIntelSnapshot). Trivial cost.
  2. If any trigger condition fires → run the hold debate.
  3. Apply the verdict (no-op / replace_stop / flatten_position).
  4. Persist a HoldDebateRun audit row.

Triggers (any one fires the debate):
  * intel_score dropped >50% from entry baseline
  * sentiment flipped from positive (>+0.3) to negative (<-0.3)
  * 3+ new negative articles since entry
  * Fresh sec_8k event for the held symbol within last lookback window
    (HARD trigger — Phase A interaction)
  * VIP tweet with severity=high mentioning the symbol

Fail-soft EVERYWHERE: any error returns None, leaves bracket order
untouched, queues an operator alert. We NEVER auto-exit on infrastructure
failure — the deterministic stop loss remains the floor.

SEQUENTIAL EXECUTION: positions are checked one-by-one. When a debate
fires, all 4 LLM calls complete before moving to the next position.
No parallelism inside or across positions.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable

from sqlalchemy import desc as _desc
from sqlalchemy.orm import Session

from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import (
    IntelCandidate, IntelEvent, RoleRun, TradeIntelSnapshot,
)


log = logging.getLogger(__name__)


# Default trigger thresholds — overridable via strategy/config.yaml::hold.
DEFAULT_SCORE_DROP_THRESHOLD = 0.5      # 50% drop from entry score fires
DEFAULT_SENTIMENT_FLIP_THRESHOLD = 0.3  # +0.3 → -0.3 swing fires
DEFAULT_NEGATIVE_NEWS_COUNT = 3         # 3+ new negative events fires
DEFAULT_8K_HARD_TRIGGER = True          # any fresh sec_8k fires immediately
DEFAULT_8K_LOOKBACK_MINUTES = 30        # how far back to look for fresh 8-Ks
DEFAULT_VIP_HARD_TRIGGER = True         # high-severity vip_tweet fires
DEFAULT_DAILY_DEBATE_CAP = 30


def _classify_triggers(
    *,
    entry_score: float | None,
    current_score: float | None,
    entry_sentiment: float | None,
    current_sentiment: float | None,
    n_new_negative: int,
    has_fresh_8k: bool,
    has_vip_high: bool,
    score_drop_threshold: float = DEFAULT_SCORE_DROP_THRESHOLD,
    sentiment_flip_threshold: float = DEFAULT_SENTIMENT_FLIP_THRESHOLD,
    negative_news_count: int = DEFAULT_NEGATIVE_NEWS_COUNT,
    enable_8k_hard_trigger: bool = DEFAULT_8K_HARD_TRIGGER,
    enable_vip_hard_trigger: bool = DEFAULT_VIP_HARD_TRIGGER,
) -> list[str]:
    """Pure trigger classifier (testable). Returns a list of fired triggers
    in priority order (hard triggers first, soft last).
    """
    fired: list[str] = []
    # HARD triggers — primary-source events. Fire first.
    if enable_8k_hard_trigger and has_fresh_8k:
        fired.append("8k_hard_trigger")
    if enable_vip_hard_trigger and has_vip_high:
        fired.append("vip_high_severity")
    # SOFT triggers — derivative metrics. Computed below.
    if (entry_score is not None and current_score is not None
            and entry_score > 0
            and (current_score / entry_score) <= (1.0 - score_drop_threshold)):
        fired.append("score_drop")
    if (entry_sentiment is not None and current_sentiment is not None
            and entry_sentiment >= sentiment_flip_threshold
            and current_sentiment <= -sentiment_flip_threshold):
        fired.append("sentiment_flip")
    if n_new_negative >= negative_news_count:
        fired.append("negative_news_cluster")
    return fired


def _count_new_negative_events(
    engine, *, symbol: str, since: dt.datetime,
) -> int:
    """Count intel_events for symbol with sentiment <= -0.3 since the
    given timestamp."""
    with Session(engine) as session:
        rows = (
            session.query(IntelEvent)
            .filter(IntelEvent.symbol == symbol)
            .filter(IntelEvent.ingested_at >= since)
            .filter(IntelEvent.sentiment.isnot(None))
            .filter(IntelEvent.sentiment <= -0.3)
            .all()
        )
    return len(rows)


def _has_fresh_event(
    engine, *, symbol: str, source: str, lookback_minutes: int = 30,
    severity_filter: float | None = None, now: dt.datetime | None = None,
) -> bool:
    """Has there been a fresh event for symbol from the given source
    within the lookback window? Optionally filter by severity (e.g., raw_score
    >= severity_filter for vip_tweets).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(minutes=lookback_minutes)
    with Session(engine) as session:
        q = (
            session.query(IntelEvent)
            .filter(IntelEvent.symbol == symbol)
            .filter(IntelEvent.source == source)
            .filter(IntelEvent.ingested_at >= cutoff)
        )
        if severity_filter is not None:
            q = q.filter(IntelEvent.raw_score >= severity_filter)
        return q.first() is not None


def _current_intel_snapshot(
    engine, *, symbol: str, asset_class: str,
) -> tuple[float | None, float | None]:
    """Return (current_intel_score, current_sentiment_avg) for the symbol.
    None on missing row. Bypasses scout dismissal — even a dismissed
    candidate's score is informative for the hold-debate baseline check.
    """
    with Session(engine) as session:
        row = (
            session.query(IntelCandidate)
            .filter(IntelCandidate.symbol == symbol)
            .filter(IntelCandidate.asset_class == asset_class)
            .first()
        )
    if row is None:
        return (None, None)
    return (
        float(row.score),
        float(row.sentiment_avg) if row.sentiment_avg is not None else None,
    )


class PositionMonitorRole(BaseRole):
    """Tier-2 lab role. Cadence: 15 min market-hours + 30 min before open.

    Compatibility note: APScheduler / daemon scheduler is responsible for
    the cadence; this role just runs end-to-end on each tick.
    """

    name = "position_monitor"
    tier = 2
    process = "lab"
    job_description = (
        "Watches open positions for thesis decay and fires the hold debate "
        "when triggers indicate the entry catalyst has changed."
    )
    sla_seconds = 5 * 60
    upstream_roles = ["intel_ingestor"]
    downstream_roles = ["debate_outcome_analyzer"]

    def __init__(
        self,
        *,
        engine,
        alpaca_client=None,
        settings=None,
        positions_provider=None,
    ):
        super().__init__(engine=engine)
        self._alpaca = alpaca_client
        self._settings = settings
        # Test hook — overrides _list_positions()
        self._positions_provider = positions_provider

    # ---- core work ----

    def _list_positions(self) -> list[dict]:
        """Return [{symbol, qty, entry_price, current_price, entry_order_id,
        stop_price, take_profit_price, asset_class, days_held, unrealized_pnl_usd,
        unrealized_pnl_pct}].

        Uses the alpaca client by default; tests can inject a positions_provider
        to bypass the broker call.
        """
        if self._positions_provider is not None:
            return list(self._positions_provider())
        if self._alpaca is None:
            return []
        out: list[dict] = []
        try:
            positions = self._alpaca.get_positions()
        except Exception as e:  # noqa: BLE001
            log.warning("position_monitor: get_positions failed: %s", e)
            return []
        for p in positions:
            try:
                out.append(_position_to_dict(p))
            except Exception:
                continue
        return out

    def _do_work(self, ctx) -> dict:
        # Per-tick budget gate
        try:
            from trading_bot import hold_debate
        except Exception as e:  # noqa: BLE001
            return {"error": f"hold_debate import failed: {e}"}

        settings = self._settings
        daily_cap = int(getattr(settings, "hold_debate_daily_cap",
                                 DEFAULT_DAILY_DEBATE_CAP) or DEFAULT_DAILY_DEBATE_CAP)
        score_drop_thr = float(getattr(settings, "hold_score_drop_threshold",
                                       DEFAULT_SCORE_DROP_THRESHOLD))
        sentiment_flip_thr = float(getattr(settings, "hold_sentiment_flip_threshold",
                                            DEFAULT_SENTIMENT_FLIP_THRESHOLD))
        negative_news_thr = int(getattr(settings, "hold_negative_news_count_threshold",
                                         DEFAULT_NEGATIVE_NEWS_COUNT))
        enable_8k_hard = bool(getattr(settings, "hold_8k_hard_trigger",
                                       DEFAULT_8K_HARD_TRIGGER))
        enable_vip_hard = bool(getattr(settings, "hold_vip_hard_trigger",
                                        DEFAULT_VIP_HARD_TRIGGER))

        positions = self._list_positions()
        n_checked = 0
        n_triggered = 0
        n_acted = 0
        per_symbol: list[dict] = []

        for p in positions:
            n_checked += 1
            symbol = p["symbol"]
            asset_class = p.get("asset_class") or "stock"
            entry_order_id = p.get("entry_order_id") or ""

            # Snapshot lookup (entry baseline)
            snap = hold_debate.lookup_snapshot(self.engine, entry_order_id)
            entry_score = float(snap.entry_intel_score) if snap and snap.entry_intel_score is not None else None
            entry_sentiment = float(snap.entry_sentiment_avg) if snap and snap.entry_sentiment_avg is not None else None
            entry_thesis = (snap.entry_top_reason if snap else "") or ""
            entry_top_sources: list[str] = []
            if snap and snap.entry_top_sources_json:
                try:
                    import json as _json
                    parsed = _json.loads(snap.entry_top_sources_json)
                    if isinstance(parsed, list):
                        entry_top_sources = parsed
                except Exception:
                    pass

            # Cheap re-fetch
            current_score, current_sentiment = _current_intel_snapshot(
                self.engine, symbol=symbol, asset_class=asset_class,
            )
            since_entry = snap.captured_at if snap and snap.captured_at else (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
            )
            if since_entry.tzinfo is None:
                since_entry = since_entry.replace(tzinfo=dt.timezone.utc)
            n_negative = _count_new_negative_events(
                self.engine, symbol=symbol, since=since_entry,
            )
            has_fresh_8k = _has_fresh_event(
                self.engine, symbol=symbol, source="sec_8k",
                lookback_minutes=DEFAULT_8K_LOOKBACK_MINUTES,
            )
            has_vip_high = _has_fresh_event(
                self.engine, symbol=symbol, source="vip_tweet",
                lookback_minutes=DEFAULT_8K_LOOKBACK_MINUTES,
                severity_filter=2.0,  # raw_score>=2 corresponds to high severity
            )

            # Classify triggers
            fired = _classify_triggers(
                entry_score=entry_score, current_score=current_score,
                entry_sentiment=entry_sentiment, current_sentiment=current_sentiment,
                n_new_negative=n_negative,
                has_fresh_8k=has_fresh_8k, has_vip_high=has_vip_high,
                score_drop_threshold=score_drop_thr,
                sentiment_flip_threshold=sentiment_flip_thr,
                negative_news_count=negative_news_thr,
                enable_8k_hard_trigger=enable_8k_hard,
                enable_vip_hard_trigger=enable_vip_hard,
            )

            if not fired:
                per_symbol.append({"symbol": symbol, "triggered": False})
                continue
            n_triggered += 1

            # Daily cap check (re-fetch each iteration so we stop firing
            # mid-tick if we hit the cap)
            today_count = hold_debate.count_todays_hold_debates(self.engine)
            if not hold_debate.should_hold_debate(
                daily_debate_count=today_count, daily_cap=daily_cap,
            ):
                per_symbol.append({
                    "symbol": symbol, "triggered": True,
                    "skipped_reason": f"daily_cap_reached ({today_count}/{daily_cap})",
                })
                continue

            new_events_summary = _summarize_new_events(
                self.engine, symbol=symbol, since=since_entry,
            )

            # Phase D — inject latest lesson block (if fresh)
            try:
                from trading_bot.lesson_loop import latest_lesson_block
                lessons_block = latest_lesson_block(self.engine)
            except Exception:
                lessons_block = ""

            # SEQUENTIAL: run the 4-LLM debate end-to-end before moving on
            verdict = hold_debate.run_hold_debate(
                self.engine,
                symbol=symbol,
                asset_class=asset_class,
                qty=p.get("qty", 0),
                entry_price=p.get("entry_price", 0.0),
                current_price=p.get("current_price"),
                stop_price=p.get("stop_price"),
                take_profit_price=p.get("take_profit_price"),
                days_held=int(p.get("days_held", 0)),
                unrealized_pnl_usd=p.get("unrealized_pnl_usd"),
                unrealized_pnl_pct=p.get("unrealized_pnl_pct"),
                entry_thesis=entry_thesis,
                entry_intel_score=entry_score,
                entry_sentiment=entry_sentiment,
                entry_top_sources=entry_top_sources,
                current_intel_score=current_score,
                current_sentiment=current_sentiment,
                trigger_reason=fired[0],  # primary trigger (highest priority)
                new_events_summary=new_events_summary,
                lessons_block=lessons_block,
            )

            action_taken = "none"
            if verdict is not None:
                if verdict.recommendation == "exit_now":
                    if self._alpaca is not None:
                        try:
                            self._alpaca.flatten_position(symbol=symbol)
                            action_taken = "flattened"
                            n_acted += 1
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "position_monitor: flatten_position failed for %s: %s",
                                symbol, e,
                            )
                            action_taken = "flatten_failed"
                elif verdict.recommendation == "tighten_stop":
                    if self._alpaca is not None:
                        try:
                            new_stop = _compute_tightened_stop(p)
                            if new_stop is not None:
                                self._alpaca.replace_stop(
                                    symbol=symbol, new_stop_price=new_stop,
                                )
                                action_taken = "stop_replaced"
                                n_acted += 1
                            else:
                                action_taken = "tighten_skipped_no_floor"
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "position_monitor: replace_stop failed for %s: %s",
                                symbol, e,
                            )
                            action_taken = "tighten_failed"
                else:
                    action_taken = "none"

            # Always persist an audit row for the trigger (verdict may be None)
            hold_debate.persist_run(
                self.engine,
                verdict=verdict,
                symbol=symbol,
                asset_class=asset_class,
                entry_order_id=entry_order_id,
                trigger_reason=fired[0],
                current_score=current_score,
                current_sentiment=current_sentiment,
                entry_score=entry_score,
                entry_sentiment=entry_sentiment,
                action_taken=action_taken,
            )

            per_symbol.append({
                "symbol": symbol,
                "triggered": True,
                "triggers": fired,
                "verdict": verdict.recommendation if verdict else "fail_soft",
                "action_taken": action_taken,
            })

        return {
            "n_positions_checked": n_checked,
            "n_triggered": n_triggered,
            "n_acted": n_acted,
            "per_symbol": per_symbol,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "monitor_runs",
            float(count),
            f"{count} position-monitor runs in last {lookback_days}d",
        )


def _summarize_new_events(
    engine, *, symbol: str, since: dt.datetime, max_events: int = 5,
) -> str:
    """Build a small summary of recent intel events to put in the hold-debate
    brief. Sorted by ingested_at desc; capped at max_events."""
    with Session(engine) as session:
        rows = (
            session.query(IntelEvent)
            .filter(IntelEvent.symbol == symbol)
            .filter(IntelEvent.ingested_at >= since)
            .order_by(_desc(IntelEvent.ingested_at))
            .limit(max_events)
            .all()
        )
    if not rows:
        return "  (no new events)"
    lines = []
    for r in rows:
        sent = (
            f"{float(r.sentiment):+.2f}"
            if r.sentiment is not None else "(none)"
        )
        lines.append(
            f"  - {r.source}: {(r.headline or '')[:160]} (sentiment={sent})"
        )
    return "\n".join(lines)


def _compute_tightened_stop(p: dict) -> float | None:
    """Compute the new stop price for a tighten_stop verdict.

    Strategy: max(entry_price, current_price * 0.99) — protect the entry
    price as floor; if currently in profit, lock in 99% of the gain.
    Returns None if we can't compute (missing inputs).
    """
    entry = p.get("entry_price")
    current = p.get("current_price")
    if entry is None or current is None:
        return None
    try:
        entry = float(entry)
        current = float(current)
    except (ValueError, TypeError):
        return None
    if current <= entry:
        # Not in profit — tighten to entry (breakeven)
        return entry
    # In profit — lock in 99% of the gain (1% trailing)
    return max(entry, current * 0.99)


def _position_to_dict(pos) -> dict:
    """Convert an Alpaca SDK Position object to the shape the monitor uses."""
    qty = float(getattr(pos, "qty", 0) or 0)
    avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)
    current = float(getattr(pos, "current_price", 0) or 0)
    market_value = float(getattr(pos, "market_value", 0) or 0)
    cost_basis = float(getattr(pos, "cost_basis", 0) or 0)
    upl_usd = market_value - cost_basis if cost_basis else None
    upl_pct = (
        (current / avg_entry - 1.0) * 100.0 if avg_entry else None
    )
    return {
        "symbol": str(getattr(pos, "symbol", "")),
        "qty": qty,
        "entry_price": avg_entry,
        "current_price": current,
        "asset_class": str(getattr(pos, "asset_class", "stock") or "stock"),
        "entry_order_id": "",  # not on the Position object; orchestrator can stamp later
        "stop_price": None,
        "take_profit_price": None,
        "days_held": 0,        # not tracked on Position; could derive from journal
        "unrealized_pnl_usd": upl_usd,
        "unrealized_pnl_pct": upl_pct,
    }
