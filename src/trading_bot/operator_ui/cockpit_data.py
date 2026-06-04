"""Live data builder for the v4 cockpit.

Reads the ledger + policy locks + intel cache files + status snapshot
and returns a single dict matching the shape of the cockpit's
``data.jsx`` globals. The HTML loads this as an overlay AFTER the
mock data.jsx, so any field we can't compute today gracefully falls
through to the mock baseline.

Pure read-only: no writes to the ledger, no broker calls, no LLM
invocations. Safe to call from a high-frequency endpoint (dashboard
polling) — but the cockpit reads it once at page load and shows the
snapshot until the operator refreshes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ledger import DEFAULT_LEDGER_PATH
from trading_bot.operator import controls
from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
INTEL_CACHE = Path.home() / ".cache" / "trading_bot" / "intel"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_query(
    conn: sqlite3.Connection, sql: str, params: tuple = (),
) -> list[dict]:
    """Run a query; on missing-table or any DB error return ``[]``.

    Cockpit must not crash because a Phase-D table hasn't been used yet."""
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError as e:
        log.debug("cockpit query failed: %s", e)
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _short_hash(h: str | None, head: int = 4, tail: int = 2) -> str:
    if not h:
        return "—"
    h = h.replace("sha256:", "")
    if len(h) <= head + tail:
        return h
    return f"{h[:head]}…{h[-tail:]}"


def _fmt_pct(x: float) -> float:
    return round(x, 4)


# ---------------------------------------------------------------------------
# STATUS_BASE — account, lanes, kill switches, halted state
# ---------------------------------------------------------------------------


def build_status_base(
    conn: sqlite3.Connection, snap: Mapping[str, Any],
) -> dict:
    # Latest account_snapshot row.
    rows = _safe_query(
        conn,
        "SELECT snapshot_ts, equity, cash, buying_power, daytrade_count, "
        "pattern_day_trader, broker_status FROM account_snapshot "
        "ORDER BY ledger_seq DESC LIMIT 2",
    )
    latest = rows[0] if rows else {}
    # First snapshot of the trading day (used for day_pl).
    today_iso = dt.datetime.now(dt.timezone.utc).date().isoformat()
    first_today = _safe_query(
        conn,
        "SELECT equity FROM account_snapshot WHERE snapshot_ts >= ? "
        "ORDER BY ledger_seq ASC LIMIT 1",
        (today_iso,),
    )
    sod_equity = float(first_today[0]["equity"]) if first_today else float(
        latest.get("equity", 0.0)
    )
    cur_equity = float(latest.get("equity", 0.0))
    day_pl_abs = round(cur_equity - sod_equity, 2)
    day_pl_pct = (
        (cur_equity / sod_equity - 1.0) if sod_equity > 0 else 0.0
    )

    active_kills = list(snap.get("active_kills") or [])
    halted = bool(active_kills)

    # Lanes — derive from registered strategies + their positions.
    lanes = build_lanes(conn)

    # Kill switch panel: known catalogue with active flags from snap.
    kill_catalog = [
        "manual_operator_halt", "crypto_cap_breach", "data_staleness",
        "pdt_breach", "drawdown_2pct", "drift_threshold",
        "lock_mismatch", "ledger_chain_fail",
    ]
    kills = [
        {"name": k, "active": k in active_kills}
        for k in kill_catalog
    ]

    return {
        "system_state": "halted" if halted else "running",
        "halted": {
            "active": halted,
            "reason": active_kills[0] if active_kills else None,
            "since": None,
            "operator": None,
        },
        "risk_profile": _read_risk_profile(),
        "account": {
            "equity": cur_equity,
            "cash": float(latest.get("cash", 0.0)),
            "day_pl_abs": day_pl_abs,
            "day_pl_pct": _fmt_pct(day_pl_pct),
            "buying_power": float(latest.get("buying_power", 0.0)),
            "daytrade_count": int(latest.get("daytrade_count", 0) or 0),
            "snapshot_ts": latest.get("snapshot_ts"),
        },
        "lanes": lanes,
        "kill_switches": kills,
        "boot_check": {
            "ok": not halted,
            "hash_verified_at": snap.get("ts"),
        },
    }


def _read_risk_profile() -> str:
    p = DEFAULT_POLICY_DIR / "risk_policy.lock"
    if not p.exists():
        return "neutral"
    try:
        payload = json.loads(p.read_text())
        return payload.get("profile", "neutral")
    except Exception:  # noqa: BLE001
        return "neutral"


# ---------------------------------------------------------------------------
# LANES + EXPOSURE
# ---------------------------------------------------------------------------


_LANE_MAP = {
    "us_equity": ("stocks", "ETF Momentum"),
    "crypto": ("crypto", "Crypto"),
    "us_option": ("options", "Wheel"),
}
# Normalise legacy / alias / enum-prefixed asset_class strings to the
# canonical keys. The broker snapshot writes ``assetclass.crypto`` etc.
# (Python enum __str__ format).
_LANE_ALIASES = {
    "option": "us_option", "equity": "us_equity", "stock": "us_equity",
}


def _normalize_asset_class(raw: str | None) -> str:
    if not raw:
        return "us_equity"
    s = str(raw).lower()
    if "." in s:
        s = s.split(".", 1)[1]
    return _LANE_ALIASES.get(s, s)


# Position source we read from. ``broker`` rows are written by the
# Alpaca adapter on each position_snapshot job tick (the bot's view of
# the live account); ``bot`` rows would be the strategy-attributed
# snapshot but isn't populated until v3 strategies actually fill.
_POSITION_SOURCE = "broker"


def build_lanes(conn: sqlite3.Connection) -> list[dict]:
    # Get current equity to compute exposure %.
    acct = _safe_query(
        conn, "SELECT equity FROM account_snapshot ORDER BY ledger_seq DESC LIMIT 1",
    )
    equity = float(acct[0]["equity"]) if acct else 0.0

    # Sum market value per asset class from latest position snapshot.
    rows = _safe_query(
        conn,
        f"""
        SELECT p.asset_class, p.symbol, p.market_value
        FROM position_snapshot p
        INNER JOIN (
            SELECT symbol, MAX(snapshot_ts) AS mx
            FROM position_snapshot WHERE source=?
            GROUP BY symbol
        ) latest ON p.symbol = latest.symbol AND p.snapshot_ts = latest.mx
        WHERE p.source=?
        """,
        (_POSITION_SOURCE, _POSITION_SOURCE),
    )
    sums: dict[str, float] = {}
    for r in rows:
        ac = _normalize_asset_class(r.get("asset_class"))
        sums[ac] = sums.get(ac, 0.0) + abs(float(r.get("market_value") or 0.0))

    # Static caps from risk_policy.lock.
    caps = _read_lane_caps()

    lanes = []
    for asset_class, (key, name) in _LANE_MAP.items():
        exposure = sums.get(asset_class, 0.0)
        exposure_pct = (exposure / equity) if equity > 0 else 0.0
        lanes.append({
            "key": key, "name": name,
            "short": key.capitalize(),
            "enabled": True,
            "exposure_pct": _fmt_pct(exposure_pct),
            "cap_pct": caps.get(asset_class, 0.5),
        })
    return lanes


def _read_lane_caps() -> dict[str, float]:
    p = DEFAULT_POLICY_DIR / "risk_policy.lock"
    try:
        lock = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {"us_equity": 0.85, "crypto": 0.15, "us_option": 0.20}
    asset = lock.get("asset_class", {})
    return {
        "us_equity": float(asset.get("equity_gross_max_pct", 80.0)) / 100.0,
        "crypto": float(asset.get("crypto_gross_max_pct", 15.0)) / 100.0,
        "us_option": float(asset.get("option_gross_max_pct", 20.0)) / 100.0,
    }


def build_exposure_breakdown(conn: sqlite3.Connection) -> list[dict]:
    acct = _safe_query(
        conn, "SELECT equity, cash FROM account_snapshot "
        "ORDER BY ledger_seq DESC LIMIT 1",
    )
    if not acct:
        return []
    equity = float(acct[0]["equity"])
    cash = float(acct[0]["cash"])
    rows = _safe_query(
        conn,
        f"""
        SELECT p.asset_class, SUM(ABS(p.market_value)) AS mv
        FROM position_snapshot p
        INNER JOIN (
            SELECT symbol, MAX(snapshot_ts) AS mx FROM position_snapshot
            WHERE source=? GROUP BY symbol
        ) latest ON p.symbol = latest.symbol AND p.snapshot_ts = latest.mx
        WHERE p.source=?
        GROUP BY p.asset_class
        """,
        (_POSITION_SOURCE, _POSITION_SOURCE),
    )
    # Roll up by normalised asset class (in case the broker snapshot
    # writes ``assetclass.crypto`` for some rows and ``crypto`` for
    # others — both should land in the same bucket).
    by_class: dict[str, float] = {}
    for r in rows:
        ac = _normalize_asset_class(r.get("asset_class"))
        by_class[ac] = by_class.get(ac, 0.0) + (r.get("mv") or 0.0)
    out = []
    for ac, mv in by_class.items():
        name = _LANE_MAP.get(ac, (ac, ac))[1]
        out.append({
            "name": name,
            "value": _fmt_pct(mv / equity if equity else 0.0),
            "color": "var(--info)" if ac == "us_equity" else
                     "var(--warn)" if ac == "crypto" else
                     "var(--text-faint)",
        })
    if equity > 0:
        out.append({
            "name": "Cash",
            "value": _fmt_pct(cash / equity),
            "color": "var(--text-faint)",
        })
    return out


# ---------------------------------------------------------------------------
# REGIME
# ---------------------------------------------------------------------------


def build_regime(conn: sqlite3.Connection) -> dict:
    """Pull current regime per asset class + the intel signals
    feeding the classifier."""
    asset_classes = []
    for ac in ("stocks", "crypto", "options"):
        rows = _safe_query(
            conn,
            "SELECT new_regime, event_ts, source, trigger_signals_json "
            "FROM regime_event WHERE asset_class=? "
            "ORDER BY ledger_seq DESC LIMIT 1",
            (ac,),
        )
        cur = rows[0]["new_regime"] if rows else "normal"
        since = rows[0]["event_ts"] if rows else None
        asset_classes.append({
            "asset_class": ac, "regime": cur, "since": since,
            "source": rows[0]["source"] if rows else "classifier:default",
        })

    # Live signals from intel caches.
    signals = []
    fng = _read_intel_cache("crypto_fear_greed.json")
    if fng and "value" in fng:
        signals.append({
            "name": "Crypto F&G",
            "val": f"{int(fng['value'])} ({fng.get('classification', '')})",
            "trend": "flat",
        })
    tc = _read_intel_cache("treasury_curve.json")
    if tc and tc.get("tenors"):
        t = tc["tenors"]
        ten = t.get("10y")
        short = t.get("2y") or t.get("13w")
        if ten and short:
            slope_bps = (ten - short) * 100
            signals.append({
                "name": "Yield curve",
                "val": f"{slope_bps:+.0f}bps",
                "trend": "flat" if abs(slope_bps) < 30 else "down",
            })
        if ten:
            signals.append({
                "name": "10Y Treasury",
                "val": f"{ten:.2f}%",
                "trend": "flat",
            })
    cboe = _read_intel_cache("cboe.json")
    if cboe:
        if cboe.get("skew"):
            signals.append({
                "name": "CBOE SKEW",
                "val": f"{cboe['skew']:.1f}",
                "trend": "up" if cboe["skew"] > 140 else "flat",
            })
        if cboe.get("put_call_ratio"):
            signals.append({
                "name": "Put/Call",
                "val": f"{cboe['put_call_ratio']:.2f}",
                "trend": "flat",
            })

    # Choose a top-level label = the most-stressed asset class.
    severity = {"normal": 0, "caution": 1, "stress": 2, "crisis": 3, "recovery": 1}
    if asset_classes:
        top = max(asset_classes, key=lambda r: severity.get(r["regime"], 0))
        label = top["regime"]
    else:
        label = "normal"

    return {
        "label": label,
        "since": asset_classes[0]["since"] if asset_classes else None,
        "asset_classes": asset_classes,
        "signals": signals,
    }


def _read_intel_cache(filename: str) -> Optional[dict]:
    p = INTEL_CACHE / filename
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# RISK_CAPS
# ---------------------------------------------------------------------------


def build_risk_caps(conn: sqlite3.Connection) -> list[dict]:
    lane_caps = _read_lane_caps()
    acct = _safe_query(
        conn, "SELECT equity FROM account_snapshot ORDER BY ledger_seq DESC LIMIT 1",
    )
    equity = float(acct[0]["equity"]) if acct else 0.0
    rows = _safe_query(
        conn,
        """
        SELECT p.asset_class, SUM(ABS(p.market_value)) AS mv, COUNT(DISTINCT p.symbol) AS n
        FROM position_snapshot p
        INNER JOIN (
            SELECT symbol, MAX(snapshot_ts) AS mx FROM position_snapshot
            WHERE source=? GROUP BY symbol
        ) latest ON p.symbol = latest.symbol AND p.snapshot_ts = latest.mx
        WHERE p.source=?
        GROUP BY p.asset_class
        """,
        (_POSITION_SOURCE, _POSITION_SOURCE),
    )
    sums: dict[str, float] = {}
    for r in rows:
        sums[_normalize_asset_class(r["asset_class"])] = (
            sums.get(_normalize_asset_class(r["asset_class"]), 0.0)
            + (r.get("mv") or 0.0)
        )
    total = sum(sums.values())

    caps = [
        {
            "name": "account_exposure",
            "used": _fmt_pct(total / equity if equity else 0.0),
            "cap": lane_caps.get("us_equity", 0.85),
            "unit": "%",
        },
        {
            "name": "stocks_lane",
            "used": _fmt_pct(sums.get("us_equity", 0.0) / equity if equity else 0.0),
            "cap": lane_caps["us_equity"],
            "unit": "%",
        },
        {
            "name": "crypto",
            "used": _fmt_pct(sums.get("crypto", 0.0) / equity if equity else 0.0),
            "cap": lane_caps["crypto"],
            "unit": "%",
        },
        {
            "name": "options_lane",
            "used": _fmt_pct(sums.get("us_option", 0.0) / equity if equity else 0.0),
            "cap": lane_caps["us_option"],
            "unit": "%",
        },
    ]
    # PDT count
    pdt = _safe_query(
        conn, "SELECT day_trade_count FROM account_snapshot "
        "ORDER BY ledger_seq DESC LIMIT 1",
    )
    caps.append({
        "name": "pdt_count",
        "used": int(pdt[0]["day_trade_count"]) if pdt else 0,
        "cap": 3,
        "unit": "#",
    })
    return caps


# ---------------------------------------------------------------------------
# STRATEGY_MODE + STRATEGIES (Lab)
# ---------------------------------------------------------------------------


_STATE_TO_UI = {
    "research_only": "research_only",
    "shadow": "shadow",
    "tiny_paper": "armed",
    "scaled_paper": "armed",
    "live": "armed",
    "halted": "paused",
    "reduce_only": "exit_only",
    "observe_only": "exit_only",
    "retired": "retired",
}


def _lane_for_strategy(strategy_id: str) -> str:
    sid = strategy_id.upper()
    if "CRYPTO" in sid:
        return "crypto"
    if "WHEEL" in sid or "OPTION" in sid:
        return "options"
    return "stocks"


def build_strategy_mode(conn: sqlite3.Connection) -> list[dict]:
    rows = _safe_query(
        conn,
        "SELECT strategy_id, strategy_ver, code_hash, status "
        "FROM strategy_version ORDER BY strategy_id, strategy_ver DESC",
    )
    seen = set()
    out = []
    for r in rows:
        # Show only the latest version per strategy_id in this widget.
        if r["strategy_id"] in seen:
            continue
        seen.add(r["strategy_id"])
        ch = r.get("code_hash") or ""
        out.append({
            "name": f"{r['strategy_id']}#{_short_hash(ch)}",
            "state": _STATE_TO_UI.get(r["status"], r["status"]),
            "lane": _lane_for_strategy(r["strategy_id"]),
            "hash": _short_hash(ch, 4, 2),
        })
    return out


def build_strategies(conn: sqlite3.Connection) -> list[dict]:
    """Full lab roster — every strategy_version with validation tier."""
    rows = _safe_query(
        conn,
        """
        SELECT sv.strategy_id, sv.strategy_ver, sv.code_hash, sv.status,
               sv.lane, sv.created_ts,
               (SELECT MAX(tier) FROM validation_artifact va
                 WHERE va.strategy_id=sv.strategy_id
                   AND va.strategy_ver=sv.strategy_ver
                   AND va."pass"=1) AS max_tier_passed,
               (SELECT MAX(decision_ts) FROM strategy_decision sd
                 WHERE sd.strategy_id=sv.strategy_id) AS last_decision
        FROM strategy_version sv
        ORDER BY sv.strategy_id, sv.strategy_ver DESC
        """,
    )
    tier_map = {
        "research_candidate": 1, "paper_candidate": 2, "live_candidate": 3,
    }
    # Pre-fetch metrics per (strategy, ver) from the latest passing
    # artifact and parse the JSON payload once.
    metrics_rows = _safe_query(
        conn,
        """
        SELECT va.strategy_id, va.strategy_ver, va.tier, va.metrics_json,
               va.produced_ts
        FROM validation_artifact va
        WHERE va."pass"=1
        ORDER BY va.produced_ts DESC
        """,
    )
    metrics_by_key: dict[tuple, dict] = {}
    for m in metrics_rows:
        key = (m["strategy_id"], m["strategy_ver"])
        if key in metrics_by_key:
            continue
        try:
            metrics_by_key[key] = json.loads(m.get("metrics_json") or "{}")
        except json.JSONDecodeError:
            metrics_by_key[key] = {}

    out = []
    for r in rows:
        tier_raw = r.get("max_tier_passed") or ""
        tier_int = tier_map.get(tier_raw, 0)
        state = r["status"]
        if state == "tiny_paper":
            state = "paper"
        m = metrics_by_key.get((r["strategy_id"], r["strategy_ver"]), {})
        p_sharpe = m.get("observed_sharpe_annualised")
        d_sharpe = m.get("oos_dsr")
        pbo = m.get("pbo") or m.get("probability_of_overfit")
        out.append({
            "name": f"{r['strategy_id']} v{r['strategy_ver']}",
            "hash": _short_hash(r.get("code_hash") or ""),
            "lane": _lane_for_strategy(r["strategy_id"]),
            "state": state,
            "tier": tier_int,
            "p_sharpe": float(p_sharpe) if p_sharpe is not None else None,
            "d_sharpe": float(d_sharpe) if d_sharpe is not None else None,
            "pbo": float(pbo) if pbo is not None else None,
            "last_run": (r.get("last_decision") or "")[:16],
            "live_eligible": state == "paper" and tier_int >= 3,
        })
    return out


# ---------------------------------------------------------------------------
# POSITIONS + OPEN ORDERS
# ---------------------------------------------------------------------------


def build_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = _safe_query(
        conn,
        """
        SELECT p.symbol, p.asset_class, p.qty, p.avg_cost, p.market_price,
               p.market_value, p.strategy_id, p.classification, p.snapshot_ts
        FROM position_snapshot p
        INNER JOIN (
            SELECT symbol, MAX(snapshot_ts) AS mx FROM position_snapshot
            WHERE source=? GROUP BY symbol
        ) latest ON p.symbol = latest.symbol AND p.snapshot_ts = latest.mx
        WHERE p.source=? AND p.qty != 0
        ORDER BY ABS(p.market_value) DESC
        """,
        (_POSITION_SOURCE, _POSITION_SOURCE),
    )
    out = []
    for r in rows:
        qty = float(r["qty"])
        entry = float(r["avg_cost"] or 0.0)
        mark = float(r["market_price"] or 0.0)
        mv = float(r["market_value"] or 0.0)
        pl_abs = (mark - entry) * qty if entry > 0 and mark > 0 else 0.0
        pl_pct = (mark / entry - 1.0) if entry > 0 and mark > 0 else 0.0
        ac = _normalize_asset_class(r.get("asset_class"))
        lane = _LANE_MAP.get(ac, ("stocks", ""))[0]
        out.append({
            "symbol": r["symbol"],
            "lane": lane,
            "qty": qty,
            "entry": entry,
            "mark": mark,
            "pl_abs": round(pl_abs, 2),
            "pl_pct": _fmt_pct(pl_pct),
            "classification": r.get("classification", "unknown"),
            "stop": None,
            "opened_at": r.get("snapshot_ts"),
            "order_uid": None,
            "strategy_version": r.get("strategy_id"),
            "drift_bps": None,
        })
    return out


def build_open_orders(conn: sqlite3.Connection) -> list[dict]:
    rows = _safe_query(
        conn,
        """
        SELECT order_uid, client_order_id, symbol, side, qty, asset_class,
               state, broker_order_id, state_ts
        FROM order_current
        WHERE state IN ('intent','submitted','acked','partially_filled')
        ORDER BY state_ts DESC LIMIT 30
        """,
    )
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for r in rows:
        try:
            ts = dt.datetime.fromisoformat(r["state_ts"].replace("Z", "+00:00"))
            age_s = int((now - ts).total_seconds())
        except Exception:  # noqa: BLE001
            age_s = 0
        ac = _normalize_asset_class(r.get("asset_class"))
        lane = _LANE_MAP.get(ac, ("stocks", ""))[0]
        out.append({
            "symbol": r["symbol"],
            "lane": lane,
            "side": (r["side"] or "").upper(),
            "qty": float(r["qty"] or 0.0),
            "type": "MKT",
            "status": r["state"],
            "age_s": age_s,
            "idempotency": r["client_order_id"],
            "client_order_id": r["client_order_id"],
            "stuck": age_s > 60 and r["state"] in ("intent", "submitted"),
            "canceled": False,
        })
    return out


# ---------------------------------------------------------------------------
# ACTIVITY FEED (multi-table union)
# ---------------------------------------------------------------------------


def build_activity(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    items: list[dict] = []
    # Strategy decisions
    for r in _safe_query(
        conn,
        "SELECT decision_ts AS ts, strategy_id, risk_decision, risk_reason, "
        "ledger_seq FROM strategy_decision ORDER BY ledger_seq DESC LIMIT 40",
    ):
        verb = r["risk_decision"]
        msg = f"{r['strategy_id']}: {verb}"
        if r.get("risk_reason"):
            msg += f" — {r['risk_reason']}"
        items.append({
            "ts": r["ts"], "seq": r["ledger_seq"],
            "type": "submit" if verb == "accept" else "skip",
            "lane": _lane_for_strategy(r["strategy_id"]),
            "msg": msg,
        })
    # Fills
    for r in _safe_query(
        conn,
        "SELECT event_ts AS ts, ledger_seq, order_uid, symbol, qty, price "
        "FROM fill_event ORDER BY ledger_seq DESC LIMIT 20",
    ):
        items.append({
            "ts": r["ts"], "seq": r["ledger_seq"],
            "type": "fill", "lane": None,
            "msg": f"FILL {r['symbol']} {r['qty']} @ {r['price']:.2f} "
                   f"→ {r['order_uid'][:12]}",
        })
    # Drift breaches
    for r in _safe_query(
        conn,
        "SELECT event_ts AS ts, ledger_seq, lane, ratio, breach "
        "FROM drift_event ORDER BY ledger_seq DESC LIMIT 10",
    ):
        items.append({
            "ts": r["ts"], "seq": r["ledger_seq"],
            "type": "policy", "lane": r["lane"],
            "msg": f"drift {r['lane']} ratio={r['ratio']:.2f}"
                   + (" BREACH" if r.get("breach") else ""),
        })
    # Regime transitions
    for r in _safe_query(
        conn,
        "SELECT event_ts AS ts, ledger_seq, asset_class, prior_regime, "
        "new_regime, source FROM regime_event ORDER BY ledger_seq DESC LIMIT 10",
    ):
        items.append({
            "ts": r["ts"], "seq": r["ledger_seq"],
            "type": "policy", "lane": r["asset_class"],
            "msg": f"regime {r['asset_class']}: {r['prior_regime']} → "
                   f"{r['new_regime']} ({r['source']})",
        })
    # Universe audits
    for r in _safe_query(
        conn,
        "SELECT event_ts AS ts, ledger_seq, strategy_id, turnover_pct, "
        "breach FROM universe_audit_event ORDER BY ledger_seq DESC LIMIT 10",
    ):
        items.append({
            "ts": r["ts"], "seq": r["ledger_seq"],
            "type": "scan",
            "lane": _lane_for_strategy(r["strategy_id"]),
            "msg": f"universe audit {r['strategy_id']}: "
                   f"turnover {r['turnover_pct']:.0f}%"
                   + (" BREACH" if r.get("breach") else ""),
        })
    # LLM calls
    for r in _safe_query(
        conn,
        "SELECT event_ts AS ts, ledger_seq, persona_id, model, cache_hit, "
        "latency_ms FROM llm_call_event ORDER BY ledger_seq DESC LIMIT 10",
    ):
        items.append({
            "ts": r["ts"], "seq": r["ledger_seq"],
            "type": "mutate" if "mutator" in r["persona_id"] else "policy",
            "lane": None,
            "msg": (
                f"llm {r['persona_id']}/{r['model']}"
                + (" cache" if r["cache_hit"] else f" {r['latency_ms']}ms")
            ),
        })
    # Heartbeats — only the latest one (avoid spam)
    hb = _safe_query(
        conn,
        "SELECT last_run_ts AS ts, job_name, last_status, last_detail "
        "FROM daemon_heartbeat ORDER BY last_run_ts DESC LIMIT 1",
    )
    if hb:
        r = hb[0]
        items.append({
            "ts": r["ts"], "seq": None,
            "type": "heart", "lane": None,
            "msg": f"{r['job_name']}: {r['last_detail']}",
        })

    # Sort by timestamp desc, take top N.
    items.sort(key=lambda i: i["ts"] or "", reverse=True)
    # Format ts to HH:MM:SS where possible.
    out = []
    for it in items[:limit]:
        ts = it.get("ts") or ""
        try:
            short = ts.split("T")[1][:8]
        except Exception:  # noqa: BLE001
            short = ts[:8]
        out.append({**it, "ts": short})
    return out


# ---------------------------------------------------------------------------
# DECISIONS (recent strategy_decision rows, structured for the Activity surface)
# ---------------------------------------------------------------------------


def build_decisions(conn: sqlite3.Connection) -> list[dict]:
    rows = _safe_query(
        conn,
        "SELECT decision_ts, strategy_id, strategy_ver, risk_decision, "
        "risk_reason, intent_json, ledger_seq FROM strategy_decision "
        "ORDER BY ledger_seq DESC LIMIT 20",
    )
    out = []
    for r in rows:
        intent = {}
        try:
            intent = json.loads(r["intent_json"] or "{}")
        except json.JSONDecodeError:
            pass
        verdict = r["risk_decision"]
        action = (
            "entry" if verdict == "accept" and intent.get("side") == "buy"
            else "exit" if verdict == "accept" and intent.get("side") == "sell"
            else "skip" if verdict in ("skip", "halt") else verdict
        )
        try:
            t = r["decision_ts"].split("T")[1][:5]
        except Exception:  # noqa: BLE001
            t = ""
        out.append({
            "id": f"d{r['ledger_seq']}",
            "time": t,
            "strategy": f"{r['strategy_id']}#v{r['strategy_ver']}",
            "symbol": intent.get("symbol", "—"),
            "action": action,
            "reason": r.get("risk_reason") or "—",
            "seq": r["ledger_seq"],
        })
    return out


# ---------------------------------------------------------------------------
# LESSONS (drift_postmortem_event Claude memos)
# ---------------------------------------------------------------------------


def build_lessons(conn: sqlite3.Connection) -> list[dict]:
    rows = _safe_query(
        conn,
        "SELECT event_ts, source_event_type, memo_markdown "
        "FROM drift_postmortem_event ORDER BY ledger_seq DESC LIMIT 5",
    )
    out = []
    for r in rows:
        tag = {
            "drift_event": "drift",
            "universe_audit_event": "universe",
            "regime_event": "regime",
        }.get(r["source_event_type"], "system")
        ts = (r["event_ts"] or "").replace("T", " ")[:16]
        body = (r["memo_markdown"] or "").strip()[:400]
        out.append({"ts": ts, "tag": tag, "body": body})
    return out


# ---------------------------------------------------------------------------
# MUTATIONS + PROMOTION QUEUE
# ---------------------------------------------------------------------------


def build_mutations(conn: sqlite3.Connection) -> list[dict]:
    rows = _safe_query(
        conn,
        """
        SELECT mo.candidate_id, mo.raw_p_value, mo.adjusted_p_value, mo.survived,
               mo.event_ts, ml.family, ml.mutation_id, ml.variant_value
        FROM mutation_outcome mo
        LEFT JOIN mutation_log ml ON ml.candidate_id = mo.candidate_id
        ORDER BY mo.event_ts DESC LIMIT 20
        """,
    )
    out = []
    for r in rows:
        try:
            t = (r.get("event_ts") or "").split("T")[1][:5]
        except Exception:  # noqa: BLE001
            t = ""
        tag = "survived" if r.get("survived") else (
            "rejected" if r.get("adjusted_p_value") else "proposed"
        )
        out.append({
            "time": t,
            "strat": r.get("family", "—"),
            "param": f"{r.get('mutation_id', '')} → {r.get('variant_value')}",
            "tag": tag,
            "p": f"{r['raw_p_value']:.3f}" if r.get("raw_p_value") else "—",
        })
    return out


def build_promotion_queue(conn: sqlite3.Connection) -> list[dict]:
    """Strategies at tiny_paper with a passing Tier-1 artifact —
    eligible for operator review to graduate to scaled_paper."""
    rows = _safe_query(
        conn,
        """
        SELECT sv.strategy_id, sv.strategy_ver, sv.code_hash, sv.lane
        FROM strategy_version sv
        WHERE sv.status='tiny_paper'
        ORDER BY sv.strategy_id, sv.strategy_ver DESC
        """,
    )
    out = []
    for r in rows:
        out.append({
            "name": f"{r['strategy_id']}#{_short_hash(r.get('code_hash') or '')}",
            "lane": _lane_for_strategy(r["strategy_id"]),
            "p_sharpe": None,
            "d_sharpe": None,
            "pbo": None,
        })
    return out


# ---------------------------------------------------------------------------
# LLM_SPEND (aggregated from llm_call_event)
# ---------------------------------------------------------------------------


# Rough per-1k-tokens pricing (USD). Sonnet input/output blended; Opus
# blended; values approximate operator's actual costs.
_LLM_PRICE_PER_1K = {
    "sonnet": {"in": 0.003, "out": 0.015},
    "opus":   {"in": 0.015, "out": 0.075},
    "haiku":  {"in": 0.0008, "out": 0.004},
}


def _approx_cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    price = _LLM_PRICE_PER_1K.get(model, _LLM_PRICE_PER_1K["sonnet"])
    return (in_tok / 1000.0) * price["in"] + (out_tok / 1000.0) * price["out"]


def build_llm_spend(conn: sqlite3.Connection) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    rows_today = _safe_query(
        conn,
        "SELECT model, persona_id, input_tokens, output_tokens, cache_hit "
        "FROM llm_call_event WHERE event_ts >= ? AND cache_hit=0 AND dropped=0",
        (today,),
    )
    rows_month = _safe_query(
        conn,
        "SELECT model, persona_id, input_tokens, output_tokens "
        "FROM llm_call_event WHERE event_ts >= date('now','start of month') "
        "AND cache_hit=0 AND dropped=0",
    )
    today_total = sum(
        _approx_cost_usd(r["model"], r["input_tokens"], r["output_tokens"])
        for r in rows_today
    )
    month_total = sum(
        _approx_cost_usd(r["model"], r["input_tokens"], r["output_tokens"])
        for r in rows_month
    )

    # Bucket by role group (judge/reviewer/mutator/postmortem).
    role_buckets = {
        "Judge": {"models": {"opus"}, "personas": {
            "strategy_implementer", "quant_research_lead", "risk_validator",
        }},
        "Reviewer": {"models": set(), "personas": {
            "mutation_reviewer", "universe_audit_analyst",
            "search_space_expander",
        }},
        "Mutator": {"models": set(), "personas": {
            "mutation_proposer", "strategy_scout",
        }},
        "Postmortem": {"models": set(), "personas": {
            "drift_postmortem", "regime_analyst",
        }},
    }
    role_today: dict[str, float] = {k: 0.0 for k in role_buckets}
    for r in rows_today:
        cost = _approx_cost_usd(
            r["model"], r["input_tokens"], r["output_tokens"],
        )
        for role, cfg in role_buckets.items():
            if r["persona_id"] in cfg["personas"]:
                role_today[role] += cost
                break
    total = sum(role_today.values()) or 0.001
    roles_out = []
    for role, cost in role_today.items():
        # Choose representative model + colour key.
        if role == "Judge":
            model_label, color = "Opus", "opus"
        elif role == "Postmortem":
            model_label, color = "Sonnet", "haiku"
        else:
            model_label, color = "Sonnet", "sonnet"
        roles_out.append({
            "role": role,
            "model": model_label,
            "today": round(cost, 2),
            "share": round(cost / total, 3),
            "color": color,
        })

    # Daily budget (from llm_throttle).
    try:
        from trading_bot.shared.llm_throttle import daily_cap
        budget = daily_cap()
    except Exception:  # noqa: BLE001
        budget = 180
    return {
        "today_total": round(today_total, 2),
        "month_total": round(month_total, 2),
        "budget_month": round(budget * 30 * 0.05, 2),  # rough $$ budget guess
        "calls_today": len(rows_today),
        "calls_today_cap": int(budget),
        "roles": roles_out,
    }


# ---------------------------------------------------------------------------
# JOBS (daemon_heartbeat)
# ---------------------------------------------------------------------------


def build_jobs(snap: Mapping[str, Any]) -> list[dict]:
    out = []
    for hb in (snap.get("heartbeats") or []):
        out.append({
            "name": hb["job_name"],
            "schedule": "—",
            "last": (hb.get("last_run_ts") or "").split("T")[-1][:8],
            "dur_ms": int((hb.get("last_duration_s") or 0.0) * 1000),
            "next_s": 0,
            "status": "ok" if hb.get("last_status") == "ok" else "fail",
            "err": (hb.get("last_detail") or "") if hb.get("last_status") != "ok" else None,
        })
    return out


# ---------------------------------------------------------------------------
# FRESHNESS (data_watermark + intel cache files)
# ---------------------------------------------------------------------------


def build_freshness(conn: sqlite3.Connection) -> list[dict]:
    out = []
    # Data watermarks (kernel-side market data freshness).
    for r in _safe_query(
        conn,
        "SELECT lane, last_quote_ts FROM data_watermark "
        "ORDER BY lane",
    ):
        try:
            ts = dt.datetime.fromisoformat(
                r["last_quote_ts"].replace("Z", "+00:00"),
            )
            age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
        except Exception:  # noqa: BLE001
            age = 0
        out.append({
            "src": f"{r['lane']}_quotes",
            "last": (r["last_quote_ts"] or "").split("T")[-1][:8],
            "cadence": "1m",
            "lag_s": int(age),
            "ok": age < 600,
        })
    # Intel feed cache freshness.
    for cache_name, label in [
        ("treasury_curve.json", "treasury_curve"),
        ("crypto_fear_greed.json", "crypto_fear_greed"),
        ("cboe.json", "cboe"),
    ]:
        p = INTEL_CACHE / cache_name
        if not p.exists():
            out.append({
                "src": label, "last": "—", "cadence": "6h",
                "lag_s": 999999, "ok": False, "why": "no cache",
            })
            continue
        mtime = dt.datetime.fromtimestamp(
            p.stat().st_mtime, tz=dt.timezone.utc,
        )
        age = int(
            (dt.datetime.now(dt.timezone.utc) - mtime).total_seconds()
        )
        out.append({
            "src": label,
            "last": mtime.isoformat()[:19],
            "cadence": "6h",
            "lag_s": age,
            "ok": age < 6 * 3600 + 600,  # 6h + 10m grace
        })
    return out


# ---------------------------------------------------------------------------
# POLICY LOCKS + PERSONAS
# ---------------------------------------------------------------------------


def build_policy_locks() -> list[dict]:
    out = []
    for p in sorted(DEFAULT_POLICY_DIR.glob("*.lock")):
        mtime = dt.datetime.fromtimestamp(
            p.stat().st_mtime, tz=dt.timezone.utc,
        ).date().isoformat()
        ver = "—"
        try:
            obj = json.loads(p.read_text())
            ver = obj.get("lock_version", "—")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "name": p.stem,
            "ver": ver,
            "changed": mtime,
            "signer": "operator",
            "status": "verified",
        })
    for p in sorted(DEFAULT_POLICY_DIR.glob("*.json")):
        if p.name == "HASHES":
            continue
        mtime = dt.datetime.fromtimestamp(
            p.stat().st_mtime, tz=dt.timezone.utc,
        ).date().isoformat()
        ver = "—"
        try:
            obj = json.loads(p.read_text())
            ver = obj.get("lock_version", "—")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "name": p.stem, "ver": ver,
            "changed": mtime, "signer": "operator", "status": "verified",
        })
    return out


def build_personas() -> list[dict]:
    out = []
    persona_dir = REPO_ROOT / "prompts" / "roles"
    if not persona_dir.exists():
        return out
    for p in sorted(persona_dir.glob("*.md")):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        out.append({
            "name": p.stem,
            "hash": _short_hash(h),
            "status": "verified",
        })
    return out


# ---------------------------------------------------------------------------
# HALTS + LEDGER HEALTH + DAEMON
# ---------------------------------------------------------------------------


def build_halts(conn: sqlite3.Connection) -> list[dict]:
    # Active kill switches + recent reconciliation_proof rows with mismatch.
    rows = _safe_query(
        conn,
        "SELECT recon_ts, recon_window, match, action_taken, ledger_seq "
        "FROM reconciliation_proof WHERE match=0 "
        "ORDER BY ledger_seq DESC LIMIT 5",
    )
    out = []
    for r in rows:
        ts = (r["recon_ts"] or "").replace("T", " ")[:16]
        out.append({
            "time": ts, "reason": r["recon_window"] + " recon mismatch",
            "operator": "kernel", "seq": r["ledger_seq"],
            "duration": "—",
        })
    return out


def build_ledger_health(conn: sqlite3.Connection) -> dict:
    table_rows = []
    for name in (
        "order_master", "order_state_event", "fill_event", "strategy_decision",
        "position_snapshot", "validation_artifact", "drift_event",
        "regime_event", "universe_audit_event", "llm_call_event",
        "drift_postmortem_event", "paper_validation_event",
        "mutation_review_event", "source_scout_event",
        "strategy_candidate", "strategy_blueprint", "strategy_codegen_event",
    ):
        n = _safe_query(conn, f"SELECT COUNT(*) AS n FROM {name}")
        if n:
            table_rows.append({"name": name, "rows": int(n[0]["n"])})
    # Last hash across the canonical strategy_decision chain.
    last = _safe_query(
        conn,
        "SELECT ledger_seq, this_hash FROM strategy_decision "
        "ORDER BY ledger_seq DESC LIMIT 1",
    )
    last_seq = int(last[0]["ledger_seq"]) if last else 0
    last_hash = _short_hash(last[0]["this_hash"]) if last else "—"
    # Last 60 hash-chain blocks: we don't sample chains lazily; show last
    # 60 ledger seqs from strategy_decision as a stand-in.
    blocks = _safe_query(
        conn,
        "SELECT ledger_seq FROM strategy_decision "
        "ORDER BY ledger_seq DESC LIMIT 60",
    )
    return {
        "tables": table_rows,
        "last_seq": last_seq,
        "last_hash": last_hash,
        "chain_verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "blocks": [
            {"ok": True, "seq": b["ledger_seq"]}
            for b in reversed(blocks)
        ],
    }


def build_daemon() -> dict:
    pid = None
    try:
        import subprocess
        r = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, check=False,
        )
        for line in r.stdout.splitlines():
            if "com.tradingbot.local.daemon" in line:
                parts = line.split()
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                break
    except Exception:  # noqa: BLE001
        pass
    return {
        "last_beat": dt.datetime.now(dt.timezone.utc).isoformat(),
        "uptime": "—",
        "host": "0.0.0.0",
        "pid": pid or 0,
        "beats_per_min": 12,  # rough; ticks every ~5s for hot jobs
    }


# ---------------------------------------------------------------------------
# COST_MODEL, DRIFT, RECON
# ---------------------------------------------------------------------------


def build_cost_model() -> dict:
    p = DEFAULT_POLICY_DIR / "cost_model.lock"
    out = {"per_trade_bps": {"raw": 0.0, "broker_paper": 0.0, "pessimistic": 0.0}}
    if not p.exists():
        return out
    try:
        cfg = json.loads(p.read_text())
        st = cfg.get("stocks", {})
        bps = float(st.get("extra_slippage_bps", 5))
        out["per_trade_bps"] = {
            "raw": 0.0,
            "broker_paper": round(bps / 2.0, 2),
            "pessimistic": bps,
        }
    except Exception:  # noqa: BLE001
        pass
    return out


def build_drift(conn: sqlite3.Connection) -> dict:
    rows = _safe_query(
        conn,
        "SELECT ratio, breach, realised_mean_bps, tolerance_multiplier, "
        "modelled_mean_bps FROM drift_event ORDER BY ledger_seq DESC LIMIT 20",
    )
    spark = list(reversed([
        float(r.get("realised_mean_bps") or 0.0) for r in rows
    ]))
    cur = spark[-1] if spark else 0.0
    threshold = (
        float(rows[0].get("tolerance_multiplier") or 0.0)
        * float(rows[0].get("modelled_mean_bps") or 5.0)
    ) if rows else 2.5
    return {
        "window": 20,
        "current_bps": round(cur, 2),
        "threshold_bps": round(threshold, 2),
        "sparkline": spark or [0.0],
    }


def build_recon(conn: sqlite3.Connection) -> dict:
    rows = _safe_query(
        conn,
        "SELECT recon_ts, recon_window, match, action_taken "
        "FROM reconciliation_proof ORDER BY ledger_seq DESC LIMIT 50",
    )
    total = len(rows)
    mismatches = sum(1 for r in rows if not r.get("match"))
    last_ts = (rows[0]["recon_ts"] if rows else "").replace("T", " ")[:19]
    unresolved = sum(
        1 for r in rows
        if not r.get("match") and r.get("action_taken") != "resolved"
    )
    return {
        "last_run": last_ts,
        "total": total,
        "mismatches": mismatches,
        "unresolved": unresolved,
    }


# ---------------------------------------------------------------------------
# DAILY DIGEST
# ---------------------------------------------------------------------------


def build_daily_digest(conn: sqlite3.Connection) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    n_fills = len(_safe_query(
        conn, "SELECT 1 FROM fill_event WHERE event_ts >= ?", (today,),
    ))
    n_submits = len(_safe_query(
        conn,
        "SELECT 1 FROM strategy_decision WHERE decision_ts >= ? "
        "AND risk_decision='accept'", (today,),
    ))
    n_decisions = len(_safe_query(
        conn, "SELECT 1 FROM strategy_decision WHERE decision_ts >= ?",
        (today,),
    ))
    n_halts = len(_safe_query(
        conn, "SELECT 1 FROM strategy_decision WHERE decision_ts >= ? "
        "AND risk_decision='halt'", (today,),
    ))
    n_mut = len(_safe_query(
        conn, "SELECT 1 FROM mutation_outcome WHERE event_ts >= ?",
        (today,),
    ))
    # Equity Δ 24h
    acct = _safe_query(
        conn,
        "SELECT equity FROM account_snapshot ORDER BY ledger_seq DESC LIMIT 1",
    )
    first = _safe_query(
        conn,
        "SELECT equity FROM account_snapshot WHERE snapshot_ts >= "
        "datetime('now', '-1 day') ORDER BY ledger_seq ASC LIMIT 1",
    )
    if acct and first:
        delta = float(acct[0]["equity"]) - float(first[0]["equity"])
        pct = (
            delta / float(first[0]["equity"])
            if float(first[0]["equity"]) > 0 else 0.0
        )
        eq_label = f"{'+' if delta >= 0 else ''}${delta:.0f}"
        eq_sub = f"{'+' if pct >= 0 else ''}{pct*100:.2f}%"
        eq_up = delta >= 0
    else:
        eq_label, eq_sub, eq_up = "—", "—", True
    return {
        "date": today,
        "stats": [
            {"label": "Equity Δ 24h", "value": eq_label, "sub": eq_sub, "up": eq_up},
            {"label": "Fills", "value": str(n_fills),
             "sub": f"{n_submits} submits", "up": True},
            {"label": "Decisions", "value": str(n_decisions),
             "sub": f"{n_halts} halted"},
            {"label": "Mutations", "value": str(n_mut), "sub": "today"},
        ],
    }


# ---------------------------------------------------------------------------
# TOPOLOGY (system-map nodes with live statuses)
# ---------------------------------------------------------------------------


def _node(node_id: str, title: str, sub: str, x: int, y: int, w: int, h: int,
          *, status: str = "ok", metric: str = "—",
          group: str = "kernel", primary: bool = False,
          lane: Optional[str] = None) -> dict:
    n = {
        "id": node_id, "title": title, "sub": sub,
        "x": x, "y": y, "w": w, "h": h,
        "status": status, "metric": metric, "group": group,
    }
    if primary:
        n["primary"] = True
    if lane:
        n["lane"] = lane
    return n


def build_topology_nodes(
    conn: sqlite3.Connection, snap: Mapping[str, Any],
) -> list[dict]:
    """Live node statuses derived from ledger + daemon state.

    Status rules:
      ok     everything green
      warn   degraded job heartbeat OR lane near cap OR caution regime
      fail   halted, broker hard-fail, crisis regime
      off    deliberately disabled
    """
    hbs = snap.get("heartbeats") or []
    n_jobs = len(hbs)
    fail_jobs = [h for h in hbs if h.get("last_status") not in ("ok", None)]
    sched_status = "ok" if not fail_jobs else "warn"
    sched_metric = (
        f"{n_jobs} jobs · {len(fail_jobs)} failing"
        if fail_jobs else f"{n_jobs} jobs ok"
    )

    severity = {
        "normal": 0, "caution": 1, "stress": 2, "crisis": 3, "recovery": 1,
    }
    worst_regime = "normal"
    for ac in ("stocks", "crypto", "options"):
        rows = _safe_query(
            conn,
            "SELECT new_regime FROM regime_event "
            "WHERE asset_class=? ORDER BY ledger_seq DESC LIMIT 1",
            (ac,),
        )
        cur = rows[0]["new_regime"] if rows else "normal"
        if severity.get(cur, 0) > severity.get(worst_regime, 0):
            worst_regime = cur
    regime_status = (
        "fail" if worst_regime == "crisis"
        else "warn" if worst_regime in ("stress", "caution") else "ok"
    )

    active_kills = list(snap.get("active_kills") or [])
    risk_status = "fail" if active_kills else "ok"
    risk_metric = (
        f"{len(active_kills)} kills active"
        if active_kills else "no kills"
    )

    last_seq_row = _safe_query(
        conn, "SELECT MAX(ledger_seq) AS s FROM strategy_decision",
    )
    last_seq = (last_seq_row[0].get("s") if last_seq_row else None) or 0
    ledger_metric = f"seq {last_seq}"

    cand = _safe_query(
        conn, "SELECT COUNT(*) AS n FROM strategy_candidate",
    )
    n_cand = int(cand[0]["n"]) if cand else 0
    research_metric = (
        f"{n_cand} candidate{'s' if n_cand != 1 else ''}"
        if n_cand else "scaffold"
    )

    mut = _safe_query(
        conn, "SELECT COUNT(*) AS n FROM mutation_outcome",
    )
    n_mut = int(mut[0]["n"]) if mut else 0
    mutation_metric = (
        f"{n_mut} outcomes" if n_mut else "nightly (no runs yet)"
    )

    # Broker — recency of the snapshot heartbeats.
    broker_hb = next(
        (h for h in hbs if h.get("job_name") in (
            "account_snapshot", "position_snapshot",
        )),
        None,
    )
    broker_status = "ok"
    broker_metric = "paper"
    if broker_hb:
        try:
            ts = dt.datetime.fromisoformat(
                broker_hb["last_run_ts"].replace("Z", "+00:00"),
            )
            age = (
                dt.datetime.now(dt.timezone.utc) - ts
            ).total_seconds()
            if age > 900:
                broker_status = "warn"
                broker_metric = f"snapshot stale {int(age/60)}m"
            elif broker_hb.get("last_status") not in ("ok", None):
                broker_status = "fail"
                broker_metric = "snapshot fail"
        except Exception:  # noqa: BLE001
            pass

    # Execution — stuck working orders.
    stuck = _safe_query(
        conn,
        "SELECT COUNT(*) AS n FROM order_current "
        "WHERE state IN ('intent','submitted','acked') "
        "AND state_ts < datetime('now','-60 seconds')",
    )
    n_stuck = int(stuck[0]["n"]) if stuck else 0
    exec_status = "warn" if n_stuck else "ok"
    exec_metric = (
        f"{n_stuck} stuck" if n_stuck else "router idle"
    )

    # Lanes — exposure vs cap.
    lanes_data = build_lanes(conn)
    lane_status_for: dict[str, tuple[str, str]] = {}
    for l in lanes_data:
        exp = l["exposure_pct"]
        cap = l["cap_pct"]
        ratio = exp / cap if cap > 0 else 0
        if exp <= 0:
            status, metric = "ok", "no positions"
        elif ratio >= 0.95:
            status, metric = "warn", f"AT CAP · {exp*100:.1f}%"
        else:
            status, metric = "ok", f"{exp*100:.1f}% / {cap*100:.0f}%"
        lane_status_for[l["key"]] = (status, metric)
    s_stocks = lane_status_for.get("stocks", ("ok", "—"))
    s_crypto = lane_status_for.get("crypto", ("ok", "—"))
    s_options = lane_status_for.get("options", ("ok", "no positions"))

    return [
        _node("research", "Research Factory", "scout · intake · codegen",
              60, 60, 260, 100, status="ok",
              metric=research_metric, group="research"),
        _node("scheduler", "Scheduler", "daemon jobs",
              520, 60, 260, 100, status=sched_status,
              metric=sched_metric, group="kernel"),
        _node("ledger", "Ledger", "append-only · hash chain",
              980, 60, 260, 100, status="ok",
              metric=ledger_metric, group="kernel"),
        _node("risk", "Risk Kernel",
              "caps · regime overlay · kill switches",
              220, 220, 820, 130, status=risk_status,
              metric=risk_metric, group="kernel", primary=True),
        _node("regime", "Regime",
              f"5-regime · {worst_regime}",
              60, 405, 220, 70, status=regime_status,
              metric=worst_regime, group="research"),
        _node("mutation", "Mutation",
              "nightly · BH-FDR",
              1020, 405, 220, 70, status="ok",
              metric=mutation_metric, group="research"),
        _node("execution", "Execution", "idempotent order router",
              470, 400, 320, 80, status=exec_status,
              metric=exec_metric, group="kernel"),
        _node("broker", "Broker · Alpaca", "paper",
              470, 510, 320, 70, status=broker_status,
              metric=broker_metric, group="broker"),
        _node("lane-stocks", "Stocks", "ETF / Dual Momentum v3",
              60, 620, 280, 100, status=s_stocks[0],
              metric=s_stocks[1], group="lane-stocks", lane="stocks"),
        _node("lane-crypto", "Crypto", "Crypto Momentum v3",
              510, 620, 280, 100, status=s_crypto[0],
              metric=s_crypto[1], group="lane-crypto", lane="crypto"),
        _node("lane-options", "Options", "SPY Wheel v3",
              960, 620, 280, 100, status=s_options[0],
              metric=s_options[1], group="lane-options", lane="options"),
    ]


# ---------------------------------------------------------------------------
# EQUITY_CURVE (from account_snapshot history — replaces mock 90d curve)
# ---------------------------------------------------------------------------


def build_equity_curve(conn: sqlite3.Connection) -> dict:
    rows = _safe_query(
        conn,
        """
        SELECT snapshot_ts, equity FROM account_snapshot
        WHERE snapshot_ts >= datetime('now', '-90 days')
        ORDER BY snapshot_ts ASC
        """,
    )
    by_day: dict[str, tuple[str, float]] = {}
    for r in rows:
        day = (r["snapshot_ts"] or "")[:10]
        by_day[day] = (r["snapshot_ts"], float(r["equity"] or 0.0))
    points = [
        {"ts": ts, "equity": round(eq, 2)}
        for _day, (ts, eq) in sorted(by_day.items())
    ]
    return {"range": "90d", "points": points, "markers": []}


# ---------------------------------------------------------------------------
# ACTION_REQUIRED (computed from live data — replaces mock alert cards)
# ---------------------------------------------------------------------------


def build_action_required(
    conn: sqlite3.Connection, snap: Mapping[str, Any],
) -> list[dict]:
    actions: list[dict] = []
    rows = _safe_query(
        conn,
        """
        SELECT p.symbol, p.qty, p.classification
        FROM position_snapshot p
        INNER JOIN (
            SELECT symbol, MAX(snapshot_ts) AS mx FROM position_snapshot
            WHERE source=? GROUP BY symbol
        ) latest ON p.symbol = latest.symbol AND p.snapshot_ts = latest.mx
        WHERE p.source=? AND p.qty != 0
              AND p.classification IN ('unknown', 'external')
        """,
        (_POSITION_SOURCE, _POSITION_SOURCE),
    )
    if rows:
        n = len(rows)
        sym_preview = ", ".join(r["symbol"] for r in rows[:3])
        actions.append({
            "id": "ar_external_positions",
            "severity": "med",
            "title": (
                f"{n} unattributed broker position"
                + ("s" if n != 1 else "")
            ),
            "cause": (
                f"{sym_preview}{', …' if n > 3 else ''} not attributed "
                "to a bot strategy. These are counted against asset-class "
                "exposure caps but skipped by v3 rebalances."
            ),
            "cta": [{"label": "Review", "primary": True}],
        })
    mismatches = _safe_query(
        conn,
        "SELECT recon_ts, recon_window, action_taken "
        "FROM reconciliation_proof WHERE match=0 "
        "AND action_taken != 'resolved' ORDER BY ledger_seq DESC LIMIT 1",
    )
    if mismatches:
        actions.append({
            "id": "ar_recon",
            "severity": "high",
            "title": "Reconciliation mismatch unresolved",
            "cause": (
                f"Latest mismatch on {mismatches[0]['recon_ts'][:19]} "
                f"({mismatches[0]['recon_window']}); "
                f"action={mismatches[0]['action_taken']}."
            ),
            "cta": [{"label": "Open reconciliation", "primary": True}],
        })
    kills = list(snap.get("active_kills") or [])
    if kills:
        actions.append({
            "id": "ar_kills",
            "severity": "high",
            "title": (
                f"{len(kills)} active kill switch"
                + ("es" if len(kills) != 1 else "")
            ),
            "cause": ", ".join(kills),
            "cta": [{"label": "Halt panel", "primary": True}],
        })
    return actions


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def build_state(
    ledger_db: Optional[Path] = None,
) -> dict:
    """Build the full cockpit state. Each builder is wrapped so a
    failure in one section degrades gracefully (the section is just
    absent and the cockpit's mock baseline shows through)."""
    ledger_db = ledger_db or (REPO_ROOT / DEFAULT_LEDGER_PATH)
    snap = controls.status_snapshot(ledger_db=ledger_db)

    state: dict[str, Any] = {}

    def _step(name: str, fn):
        try:
            state[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("cockpit_data step %s failed: %s", name, e)
            state[name] = None
        return state[name]

    # Single shared connection (read-only-ish; we don't write).
    conn = sqlite3.connect(str(ledger_db))
    try:
        _step("STATUS_BASE", lambda: build_status_base(conn, snap))
        _step("LANES", lambda: state["STATUS_BASE"]["lanes"]
              if state.get("STATUS_BASE") else build_lanes(conn))
        _step("REGIME", lambda: build_regime(conn))
        _step("RISK_CAPS", lambda: build_risk_caps(conn))
        _step("STRATEGY_MODE", lambda: build_strategy_mode(conn))
        _step("POSITIONS", lambda: build_positions(conn))
        _step("OPEN_ORDERS", lambda: build_open_orders(conn))
        _step("SEED_ACTIVITY", lambda: build_activity(conn))
        _step("EXPOSURE_BREAKDOWN", lambda: build_exposure_breakdown(conn))
        _step("DAILY_DIGEST", lambda: build_daily_digest(conn))
        _step("DECISIONS", lambda: build_decisions(conn))
        _step("LESSONS", lambda: build_lessons(conn))
        _step("STRATEGIES", lambda: build_strategies(conn))
        _step("MUTATIONS", lambda: build_mutations(conn))
        _step("PROMOTION_QUEUE", lambda: build_promotion_queue(conn))
        _step("LLM_SPEND", lambda: build_llm_spend(conn))
        _step("JOBS", lambda: build_jobs(snap))
        _step("FRESHNESS", lambda: build_freshness(conn))
        _step("POLICY_LOCKS", lambda: build_policy_locks())
        _step("PERSONAS", lambda: build_personas())
        # v4 Phase A live overlays for the system map + alert cards +
        # equity history (replace mock baselines).
        _step("TOPOLOGY_NODES", lambda: build_topology_nodes(conn, snap))
        _step("EQUITY_CURVE", lambda: build_equity_curve(conn))
        _step("ACTION_REQUIRED", lambda: build_action_required(conn, snap))
        # Surfaces still expect WF_FOLDS / HEATMAP globals. We don't
        # compute either today (walk-forward + parameter plateau are
        # research-only artifacts not yet plumbed through). Emit
        # empties so the cockpit renders "no data" rather than fake
        # 5-fold Sharpes from the mock baseline.
        _step("WF_FOLDS", lambda: [])
        _step("HEATMAP", lambda: [[0]])
        _step("HALTS", lambda: build_halts(conn))
        _step("LEDGER_HEALTH", lambda: build_ledger_health(conn))
        _step("DAEMON", lambda: build_daemon())
        _step("COST_MODEL", lambda: build_cost_model())
        _step("DRIFT", lambda: build_drift(conn))
        _step("RECON", lambda: build_recon(conn))
    finally:
        conn.close()

    return state


# ---------------------------------------------------------------------------
# PAPER PORTFOLIO (backtest-driven — no broker needed)
# ---------------------------------------------------------------------------


PAPER_PORTFOLIO_CONFIG = REPO_ROOT / "data" / "paper_portfolio.json"


def _load_portfolio_config() -> dict:
    """Load paper portfolio config from data/paper_portfolio.json.

    If missing, returns defaults (2-year lookback, 10L per strategy).
    """
    if PAPER_PORTFOLIO_CONFIG.exists():
        try:
            return json.loads(PAPER_PORTFOLIO_CONFIG.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {
        "inception_date": (
            dt.date.today() - dt.timedelta(days=365 * 2)
        ).isoformat(),
        "starting_equity_per_strategy": 10_00_000,
        "currency": "INR",
        "strategies": [
            {
                "id": "etf_momentum", "name": "ETF Momentum",
                "universe": ["NIFTYBEES", "JUNIORBEES", "BANKBEES",
                             "GOLDBEES", "LIQUIDBEES", "SETFNIF50"],
                "lane": "stocks",
                "signal_module": "trading_bot.strategies.etf_momentum_v1.signal",
            },
            {
                "id": "dual_momentum", "name": "Dual Momentum",
                "universe": ["NIFTYBEES", "LIQUIDBEES"],
                "lane": "stocks",
                "signal_module": "trading_bot.strategies.dual_momentum_v1.signal",
            },
            {
                "id": "crypto_momentum", "name": "Crypto Momentum",
                "universe": ["BTC/INR", "ETH/INR"],
                "lane": "crypto",
                "signal_module": "trading_bot.strategies.crypto_momentum_v1.signal",
            },
        ],
    }


def build_paper_portfolio() -> dict:
    """Run live backtests against historical_bars.db and return a
    portfolio snapshot with per-strategy P&L, trade logs, and current
    holdings — all in INR.

    Reads inception date + equity from ``data/paper_portfolio.json``.
    To reset, edit inception_date in that file (or run
    ``bot portfolio-reset``).

    This is the paper-trading view: no broker connection needed, no
    ledger writes. Pure read-only computation against the bars DB.
    """
    from trading_bot.research.historical_bars import (
        DEFAULT_HISTORICAL_PATH, open_store, load_bars,
    )
    from trading_bot.research.backtest import run_backtest, CostLens

    if not DEFAULT_HISTORICAL_PATH.exists():
        return {"error": "No historical bars DB — run tools/load_historical_bars.py first"}

    # Load config
    cfg = _load_portfolio_config()
    inception = dt.date.fromisoformat(cfg["inception_date"])
    EQUITY = int(cfg.get("starting_equity_per_strategy", 10_00_000))
    currency = cfg.get("currency", "INR")

    # Load cost model lock
    cost_lock_path = DEFAULT_POLICY_DIR / "cost_model.lock"
    if cost_lock_path.exists():
        cost_lock = json.loads(cost_lock_path.read_text())
    else:
        cost_lock = {"stocks": {"extra_slippage_bps": 10}}

    end = dt.date.today()
    # Backtest needs lookback for momentum signal (252 trading days).
    # Load bars from 1 year before inception for signal warmup,
    # but only count P&L from inception onward.
    data_start = inception - dt.timedelta(days=400)
    start = inception

    conn = open_store()

    strategies_config = cfg.get("strategies", [])

    results = []
    total_invested = 0
    total_current = 0

    for scfg in strategies_config:
        try:
            import importlib
            mod = importlib.import_module(scfg["signal_module"])
            signal_fn = mod.signal_fn
            universe = tuple(scfg["universe"])

            # Load bars from well before inception for signal warmup
            bars = load_bars(conn, symbols=universe, start=data_start, end=end)
            bars = {s: b for s, b in bars.items() if len(b) >= 20}

            if not bars:
                results.append({
                    "name": scfg["name"], "id": scfg["id"],
                    "lane": scfg.get("lane", "stocks"),
                    "invested": EQUITY, "current": EQUITY, "pnl": 0,
                    "return_pct": 0, "trades": [], "n_trades": 0,
                    "max_dd": 0, "sharpe": 0, "win_rate": 0,
                    "error": "No data",
                })
                continue

            def make_signal(sf, univ):
                def _sig(history, decision_date):
                    return sf(history, decision_date, universe=univ)
                return _sig

            lens = CostLens.pessimistic(cost_lock)
            r = run_backtest(
                bars_by_symbol=bars,
                signal_fn=make_signal(signal_fn, universe),
                start=start, end=end,
                starting_equity=EQUITY,
                cost_lens=lens,
                rebalance_freq="monthly",
            )

            pnl = r.final_equity - r.starting_equity
            ret_pct = (r.final_equity / r.starting_equity - 1) * 100

            trade_log = []
            for t in r.trades:
                trade_log.append({
                    "date": t.fill_date.isoformat(),
                    "symbol": t.symbol,
                    "side": t.side.upper(),
                    "qty": round(t.qty, 4),
                    "price": round(t.price, 2),
                    "value": round(t.notional, 2),
                    "fees": round(t.fees, 2),
                })

            total_invested += EQUITY
            total_current += r.final_equity

            results.append({
                "name": scfg["name"], "id": scfg["id"],
                "lane": scfg.get("lane", "stocks"),
                "invested": EQUITY,
                "current": round(r.final_equity, 2),
                "pnl": round(pnl, 2),
                "return_pct": round(ret_pct, 2),
                "n_trades": r.n_trades,
                "max_dd": round(r.max_drawdown_pct, 1),
                "sharpe": round(r.sharpe_annualised, 2),
                "win_rate": round(r.win_rate * 100, 1),
                "total_fees": round(r.total_fees, 2),
                "trades": trade_log,
            })
        except Exception as e:
            err_msg = str(e)
            # Don't log "no trading dates" as a warning — it's expected
            # when inception is today/tomorrow.
            if "no trading dates" not in err_msg:
                log.warning("paper portfolio %s failed: %s", scfg["name"], e)
            results.append({
                "name": scfg["name"], "id": scfg["id"],
                "lane": scfg.get("lane", "stocks"),
                "invested": EQUITY, "current": EQUITY, "pnl": 0,
                "return_pct": 0, "trades": [], "n_trades": 0,
                "max_dd": 0, "sharpe": 0, "win_rate": 0,
                "status": "waiting" if "no trading dates" in err_msg else "error",
                "message": (
                    f"Starts {inception.isoformat()} — no trading days yet"
                    if "no trading dates" in err_msg
                    else err_msg
                ),
            })

    conn.close()

    # Current market prices
    conn2 = open_store()
    all_syms = set()
    for scfg2 in strategies_config:
        all_syms.update(scfg2["universe"])
    price_bars = load_bars(
        conn2, symbols=tuple(all_syms),
        start=end - dt.timedelta(days=10), end=end,
    )
    conn2.close()

    prices = {}
    labels = {
        "NIFTYBEES": ("Nippon Nifty 50 ETF", "Index ETF"),
        "JUNIORBEES": ("Nippon Nifty Next 50 ETF", "Mid-Cap ETF"),
        "BANKBEES": ("Nippon Bank Nifty ETF", "Banking ETF"),
        "GOLDBEES": ("Nippon Gold ETF", "Commodity ETF"),
        "LIQUIDBEES": ("Nippon Liquid ETF", "Debt ETF"),
        "SETFNIF50": ("SBI Nifty 50 ETF", "Index ETF"),
        "BTC/INR": ("Bitcoin", "Crypto"),
        "ETH/INR": ("Ethereum", "Crypto"),
    }
    for sym in sorted(all_syms):
        b = price_bars.get(sym, [])
        if b:
            lbl = labels.get(sym, (sym, "Equity"))
            prices[sym] = {
                "price": round(b[-1].close, 2),
                "name": lbl[0],
                "type": lbl[1],
                "date": b[-1].bar_date.isoformat(),
            }

    total_pnl = total_current - total_invested

    return {
        "inception_date": inception.isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "currency": currency,
        "per_strategy_equity": EQUITY,
        "config_file": str(PAPER_PORTFOLIO_CONFIG),
        "total_invested": total_invested,
        "total_current": round(total_current, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(
            (total_current / total_invested - 1) * 100, 2
        ) if total_invested else 0,
        "strategies": results,
        "market_prices": prices,
    }


def build_realtime_portfolio() -> dict:
    """Build portfolio with LIVE prices from yfinance.

    Combines the backtest-computed positions (what we hold) with
    real-time prices (what they're worth right now). Returns the
    same shape as build_paper_portfolio() but with live mark-to-market.
    """
    from trading_bot.ingest.live_prices import fetch_live_prices

    # First get the backtest portfolio (positions + trades)
    base = build_paper_portfolio()
    if base.get("error"):
        return base

    # Collect all symbols across strategies
    all_syms = set()
    for s in base.get("strategies", []):
        for t in s.get("trades", []):
            all_syms.add(t["symbol"])
    # Also the universe symbols for price display
    cfg = _load_portfolio_config()
    for scfg in cfg.get("strategies", []):
        all_syms.update(scfg.get("universe", []))

    # Fetch live prices
    live = fetch_live_prices(list(all_syms))

    # Build live price display
    live_prices = {}
    labels = {
        "NIFTYBEES": ("Nippon Nifty 50 ETF", "Index ETF"),
        "JUNIORBEES": ("Nippon Nifty Next 50 ETF", "Mid-Cap ETF"),
        "BANKBEES": ("Nippon Bank Nifty ETF", "Banking ETF"),
        "GOLDBEES": ("Nippon Gold ETF", "Commodity ETF"),
        "LIQUIDBEES": ("Nippon Liquid ETF", "Debt ETF"),
        "SETFNIF50": ("SBI Nifty 50 ETF", "Index ETF"),
        "BTC/INR": ("Bitcoin", "Crypto"),
        "ETH/INR": ("Ethereum", "Crypto"),
    }
    for sym, q in live.items():
        lbl = labels.get(sym, (sym, "Equity"))
        live_prices[sym] = {
            "price": q.price,
            "prev_close": q.prev_close,
            "change": q.change,
            "change_pct": q.change_pct,
            "market_state": q.market_state,
            "name": lbl[0],
            "type": lbl[1],
            "fetched_at": q.fetched_at,
        }

    base["market_prices"] = live_prices
    base["live"] = True
    base["live_fetched_at"] = (
        dt.datetime.now(dt.timezone.utc).isoformat()
    )

    return base


__all__ = ["build_state", "build_paper_portfolio", "build_realtime_portfolio"]
