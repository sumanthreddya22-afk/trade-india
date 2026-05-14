"""Operator control primitives — invoked by both `bot` CLI and the
dashboard backend.

Each function is small, returns a JSON-serialisable dict, and writes an
audit entry to ``kill_switch_event`` / ``strategy_decision`` where the
underlying writer supports it.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ledger import DEFAULT_LEDGER_PATH, connect_writer
from trading_bot.operator import profiles as profiles_mod
from trading_bot.risk import (
    DEFAULT_POLICY_DIR, active_kills, clear_kill, ensure_kill_switch_table,
    fire_kill,
)

OPERATOR_HALT = "manual_operator_halt"


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

def status_snapshot(
    ledger_db: Optional[Path] = None,
) -> dict[str, Any]:
    """One-shot snapshot for the dashboard front page and `bot status`.

    Read-only across every source. Falls back to empty values rather
    than raising so the dashboard renders even with a partially
    initialised system.
    """
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    out: dict[str, Any] = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ledger_db": str(ledger_db),
        "ledger_present": ledger_db.exists(),
        "active_kills": [],
        "heartbeats": [],
        "current_profile": _current_profile_name(),
        "halted": False,
        "rth_open": _rth_open(),
    }
    if not ledger_db.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
        try:
            try:
                kills = sorted(active_kills(conn))
            except Exception:
                kills = []
            out["active_kills"] = kills
            out["halted"] = OPERATOR_HALT in kills
            out["heartbeats"] = _read_heartbeats(conn)
            out["positions_count"] = _safe_count(conn, "position_snapshot")
            out["orders_count"] = _safe_count(conn, "order_master")
            out["strategies"] = _list_strategies_brief(conn)
            out["account"] = _account_summary(conn)
            out["positions"] = _latest_positions(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _rth_open() -> bool:
    try:
        from trading_bot.daemon.market_clock import is_equity_rth
        return is_equity_rth()
    except Exception:
        return False


def _account_summary(conn: sqlite3.Connection) -> dict:
    """Latest equity + intraday P&L computed from account_snapshot rows.

    Intraday P&L = latest equity - first equity since today's 00:00 UTC.
    (Coarser than session-open; good enough for the dashboard. Plan v4
    §6 daily DD cap is also UTC-aligned in the current locks.)
    """
    try:
        cur = conn.execute(
            "SELECT snapshot_ts, equity, cash, buying_power "
            "FROM account_snapshot ORDER BY ledger_seq DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return {}
        latest = {
            "snapshot_ts": row[0], "equity": float(row[1]),
            "cash": float(row[2]), "buying_power": float(row[3]),
        }
        # First snapshot of "today" — UTC date boundary.
        today_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        cur2 = conn.execute(
            "SELECT equity, snapshot_ts FROM account_snapshot "
            "WHERE snapshot_ts >= ? ORDER BY ledger_seq ASC LIMIT 1",
            (today_str,),
        )
        opening = cur2.fetchone()
        if opening:
            latest["opening_equity"] = float(opening[0])
            latest["opening_ts"] = opening[1]
            latest["intraday_pnl"] = latest["equity"] - float(opening[0])
            latest["intraday_pnl_pct"] = (
                (latest["intraday_pnl"] / float(opening[0]) * 100.0)
                if opening[0] else 0.0
            )
        return latest
    except sqlite3.Error:
        return {}


def _latest_positions(conn: sqlite3.Connection) -> list[dict]:
    """One row per (symbol, source) at the latest snapshot_ts seen.

    Snapshots are written as a batch: every symbol carries the same
    ``snapshot_ts``. So the latest batch is "select all rows with the
    max snapshot_ts".
    """
    try:
        cur = conn.execute(
            "SELECT MAX(snapshot_ts) FROM position_snapshot WHERE source='broker'"
        )
        ts_row = cur.fetchone()
        if not ts_row or not ts_row[0]:
            return []
        cur2 = conn.execute(
            "SELECT symbol, qty, avg_cost, market_price, market_value, "
            "asset_class, classification FROM position_snapshot "
            "WHERE source='broker' AND snapshot_ts=? "
            "ORDER BY market_value DESC",
            (ts_row[0],),
        )
        cols = [c[0] for c in cur2.description]
        return [dict(zip(cols, r)) for r in cur2.fetchall()]
    except sqlite3.Error:
        return []


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    except sqlite3.Error:
        return 0


def _read_heartbeats(conn: sqlite3.Connection) -> list[dict]:
    try:
        cur = conn.execute(
            "SELECT job_name, last_run_ts, last_status, last_detail, last_duration_s "
            "FROM daemon_heartbeat ORDER BY job_name"
        )
        return [
            {"job_name": r[0], "last_run_ts": r[1], "last_status": r[2],
             "last_detail": r[3], "last_duration_s": r[4]}
            for r in cur.fetchall()
        ]
    except sqlite3.Error:
        return []


def _list_strategies_brief(conn: sqlite3.Connection) -> list[dict]:
    try:
        cur = conn.execute(
            "SELECT strategy_id, MAX(strategy_ver), status FROM strategy_version "
            "GROUP BY strategy_id ORDER BY strategy_id"
        )
        return [
            {"strategy_id": r[0], "version": r[1], "status": r[2]}
            for r in cur.fetchall()
        ]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# Halt / resume
# ---------------------------------------------------------------------------

def halt(*, reason: str, operator: str = "operator",
         ledger_db: Optional[Path] = None) -> dict:
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    conn = connect_writer(ledger_db)
    try:
        ensure_kill_switch_table(conn)
        seq = fire_kill(
            conn, detector=OPERATOR_HALT, reason=reason, actor=operator,
        )
        conn.commit()
    finally:
        conn.close()
    # Best-effort email alert. No raises.
    try:
        from trading_bot.obs.notifier import send_manual_halt_alert
        send_manual_halt_alert(operator=operator, reason=reason)
    except Exception:
        pass
    return {"ok": True, "ledger_seq": seq, "active": sorted(_active_kills_safe(ledger_db))}


def resume(*, reason: str = "operator-resume", operator: str = "operator",
           ledger_db: Optional[Path] = None) -> dict:
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    conn = connect_writer(ledger_db)
    try:
        ensure_kill_switch_table(conn)
        seq = clear_kill(
            conn, detector=OPERATOR_HALT, reason=reason, actor=operator,
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "ledger_seq": seq, "active": sorted(_active_kills_safe(ledger_db))}


def _active_kills_safe(ledger_db: Path) -> set[str]:
    if not ledger_db.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
        try:
            return active_kills(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------------------
# Risk profile
# ---------------------------------------------------------------------------

LOCK_PATH = DEFAULT_POLICY_DIR / "risk_policy.lock"


def _current_profile_name() -> str:
    """Best-effort profile inference from the current risk lock."""
    if not LOCK_PATH.exists():
        return "unknown"
    try:
        cur = json.loads(LOCK_PATH.read_text())
    except Exception:
        return "unknown"
    tag = str(cur.get("lock_version", ""))
    for name in ("safe", "neutral", "aggressive"):
        if f"profile-{name}" in tag:
            return name
    # If lock has no explicit profile tag, compare against neutral overlay.
    diffs = profiles_mod.diff_profile(cur, profiles_mod.NEUTRAL_OVERLAY)
    return "neutral" if not diffs else "custom"


def risk_profile_show() -> dict:
    return {
        "current": _current_profile_name(),
        "available": list(profiles_mod.PROFILES.keys()),
        "diffs_vs_safe": _safe_diff(profiles_mod.SAFE_OVERLAY),
        "diffs_vs_neutral": _safe_diff(profiles_mod.NEUTRAL_OVERLAY),
        "diffs_vs_aggressive": _safe_diff(profiles_mod.AGGRESSIVE_OVERLAY),
    }


def _safe_diff(overlay: Mapping) -> list[dict]:
    if not LOCK_PATH.exists():
        return []
    try:
        cur = json.loads(LOCK_PATH.read_text())
    except Exception:
        return []
    return profiles_mod.diff_profile(cur, overlay)


def risk_profile_set(profile: str, *, operator: str = "operator",
                     dry_run: bool = False) -> dict:
    if profile not in profiles_mod.PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {list(profiles_mod.PROFILES)}")
    overlay = profiles_mod.PROFILES[profile]

    if not LOCK_PATH.exists():
        raise FileNotFoundError(f"missing {LOCK_PATH} — policy directory uninitialised")
    cur = json.loads(LOCK_PATH.read_text())
    diffs = profiles_mod.diff_profile(cur, overlay)
    loosened = [d for d in diffs if d["direction"] == "loosen"]

    if dry_run:
        return {"profile": profile, "diffs": diffs, "loosened": loosened,
                "wrote_lock": False, "cooldown_required_days": 7 if loosened else 0}

    # Deep-merge overlay into current lock content.
    merged = _deep_merge(copy.deepcopy(cur), overlay)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    merged["lock_version"] = f"{today}.profile-{profile}"
    merged.setdefault("_audit", []).append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "operator": operator,
        "profile": profile,
        "diff_count": len(diffs),
    })

    LOCK_PATH.write_text(json.dumps(merged, indent=2) + "\n")

    # Recompute hashes — caller will see the regenerated file.
    recompute_result = _run_recompute_hashes()

    return {
        "profile": profile,
        "diffs": diffs,
        "loosened": loosened,
        "wrote_lock": True,
        "lock_version": merged["lock_version"],
        "cooldown_required_days": 7 if loosened else 0,
        "recompute_hashes": recompute_result,
    }


def _deep_merge(base: dict, overlay: Mapping) -> dict:
    for k, v in overlay.items():
        if isinstance(v, Mapping) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _run_recompute_hashes() -> dict:
    script = Path.cwd() / "tools" / "recompute_hashes.py"
    if not script.exists():
        return {"ran": False, "reason": "tools/recompute_hashes.py not found"}
    try:
        out = subprocess.run(
            [os.environ.get("PYTHON", "python"), str(script)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        return {"ran": True, "returncode": out.returncode,
                "stdout": out.stdout[-1000:], "stderr": out.stderr[-1000:]}
    except Exception as e:  # noqa: BLE001
        return {"ran": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Strategy submission
# ---------------------------------------------------------------------------

INTAKE_MODES = ("draft", "intake", "mutate")


def strategy_list(ledger_db: Optional[Path] = None) -> list[dict]:
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    if not ledger_db.exists():
        return []
    conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
    try:
        try:
            cur = conn.execute(
                "SELECT strategy_id, strategy_ver, lane, status, owner, "
                "thesis_id, hypothesis_id, validation_artifact_id "
                "FROM strategy_version ORDER BY strategy_id, strategy_ver"
            )
            return [
                {
                    "strategy_id": r[0], "strategy_ver": r[1], "lane": r[2],
                    "status": r[3], "owner": r[4], "thesis_id": r[5],
                    "hypothesis_id": r[6], "validation_artifact_id": r[7],
                }
                for r in cur.fetchall()
            ]
        except sqlite3.Error:
            return []
    finally:
        conn.close()


def strategy_promote(
    *, strategy_id: str, target_status: str,
    artifact_id: Optional[str] = None,
    packet_id: Optional[str] = None,
    operator: str = "operator",
    ledger_db: Optional[Path] = None,
) -> dict:
    """Promote a strategy to ``target_status``.

    Wraps the existing ``registry.promotion.gate``. On allowed=True,
    writes a new ``strategy_version`` row with the new status (rows
    are immutable; advancing status = new version).
    """
    import json as _json
    from trading_bot.registry import (
        get_active_version, promotion, register_version,
    )

    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    if not ledger_db.exists():
        return {"ok": False, "reason": "ledger missing"}

    val_lock_path = DEFAULT_POLICY_DIR / "validation_policy.lock"
    if not val_lock_path.exists():
        return {"ok": False, "reason": f"missing {val_lock_path}"}
    val_lock = _json.loads(val_lock_path.read_text())

    conn = connect_writer(ledger_db)
    try:
        try:
            current = get_active_version(conn, strategy_id)
        except Exception as e:  # VersionNotFound or other
            return {"ok": False, "reason": f"strategy not registered: {e}"}

        decision = promotion.gate(
            conn,
            strategy_id=strategy_id,
            strategy_ver=current.strategy_ver,
            target_status=target_status,
            validation_policy_lock=val_lock,
            promotion_packet_id=packet_id,
        )
        if not decision.allowed:
            return {
                "ok": False, "allowed": False,
                "reason": decision.reason,
                "target_status": target_status,
                "tier_required": decision.tier_required,
                "human_signoff_required": decision.human_signoff_required,
            }

        # Allowed → write new strategy_version row with target_status.
        new_ver = register_version(
            conn,
            strategy_id=strategy_id,
            strategy_ver=current.strategy_ver + 1,
            code_hash=current.code_hash,
            config_hash=current.config_hash,
            thesis_id=current.thesis_id,
            hypothesis_id=current.hypothesis_id,
            validation_artifact_id=decision.artifact_id or artifact_id,
            lane=current.lane,
            status=target_status,
            expiry_date=current.expiry_date,
            owner=f"{operator} (promoted from {current.status})",
        )
        conn.commit()
        return {
            "ok": True, "allowed": True,
            "strategy_id": strategy_id,
            "previous_version": current.strategy_ver,
            "previous_status": current.status,
            "new_version": new_ver.strategy_ver,
            "new_status": new_ver.status,
            "artifact_id": decision.artifact_id,
        }
    finally:
        conn.close()


def strategy_submit(
    *, name: str, description: str, mode: str = "draft",
    operator: str = "operator", ledger_db: Optional[Path] = None,
) -> dict:
    """Submit a new strategy hypothesis.

    Modes:
      * **draft** — register a ``strategy_version`` row at
        ``research_only`` status with the operator's hypothesis attached
        in the audit field. No AI is called. This is the fastest path
        and matches Plan v4 §3 "one thesis at a time".
      * **intake** — register draft *and* run ``research.run_intake``
        (adversarial pair: Bull + Bear personas). Persists both
        transcripts to ``strategy_decision``. Requires LLM hot-path env
        var; otherwise reports the gate.
      * **mutate** — register draft *and* enqueue a mutation cycle
        (``research.run_mutation_cycle``). Requires LLM hot-path AND
        ``TRADING_BOT_ENABLE_MUTATION_CYCLE``; otherwise reports the gate.

    Returns the strategy_id / version / mode / next-steps so the
    dashboard can show the right follow-up.
    """
    if mode not in INTAKE_MODES:
        raise ValueError(f"unknown mode {mode!r}; expected {INTAKE_MODES}")
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)

    # Slug strategy_id from the operator-provided name, plus a 6-char
    # content hash so two strategies with the same name don't collide.
    slug = "".join(c for c in name.upper().replace(" ", "_") if c.isalnum() or c == "_") or "STRAT"
    h = hashlib.sha256(description.encode("utf-8")).hexdigest()[:6]
    strategy_id = f"{slug}_{h}"

    from trading_bot.registry.schema import ensure_registry_tables
    from trading_bot.registry.strategies import register_version

    conn = connect_writer(ledger_db)
    try:
        ensure_registry_tables(conn)
        sv = register_version(
            conn,
            strategy_id=strategy_id,
            strategy_ver=1,
            code_hash="operator-submitted",
            config_hash=hashlib.sha256(description.encode("utf-8")).hexdigest(),
            thesis_id=f"{strategy_id}.thesis",
            hypothesis_id=f"{strategy_id}.h1",
            validation_artifact_id=None,
            lane="equity",
            status="research_only",
            expiry_date=None,
            owner=operator,
        )
        conn.commit()
    finally:
        conn.close()

    result: dict[str, Any] = {
        "ok": True, "strategy_id": strategy_id, "strategy_ver": 1,
        "mode": mode, "description": description, "next_steps": [],
    }

    if mode == "draft":
        result["next_steps"] = [
            f"Run `bot strategy submit --name {name} --mode intake --description '...'` "
            "to run the adversarial-pair intake (Bull + Bear personas).",
            "OR write an edge thesis in docs/edge_thesis_<n>.md and "
            "register it via tools/register_seed_strategy.py.",
        ]
        return result

    from trading_bot.feature_flags import is_llm_hotpath_enabled
    if not is_llm_hotpath_enabled():
        result["gated"] = True
        result["reason"] = (
            "LLM hot-path disabled. Set TRADING_BOT_ENABLE_LLM_HOTPATH=1 "
            "in .env and restart the daemon to run intake/mutate."
        )
        return result

    if mode == "intake":
        result["intake"] = _run_intake_real(
            strategy_id=strategy_id, description=description,
            operator=operator, ledger_db=ledger_db,
        )
        return result

    if mode == "mutate":
        if os.environ.get("TRADING_BOT_ENABLE_MUTATION_CYCLE", "").lower() not in {"1", "true", "yes"}:
            result["gated"] = True
            result["reason"] = (
                "TRADING_BOT_ENABLE_MUTATION_CYCLE not set. The mutation "
                "cycle is heavier (≈64 candidates/family/month, BH-FDR, "
                "persona runner). Set the env var and ensure "
                "research/search_space_v1.json is hash-locked."
            )
            return result
        result["mutate"] = _run_mutate_stub(
            strategy_id=strategy_id, description=description,
            operator=operator,
        )
        return result

    return result


def _run_intake_real(
    *, strategy_id: str, description: str, operator: str,
    ledger_db: Path,
) -> dict:
    """Run the adversarial pair (Bull + Bear) against ``description``.

    Uses ``SubprocessPersonaRunner`` to spawn ``claude --json`` (or
    whatever the operator has configured via the env var
    ``TRADING_BOT_PERSONA_CMD`` as a comma-separated list).

    Falls back to a clear error rather than silently mocking — the
    operator picked intake mode explicitly, so they want the real run.
    """
    from trading_bot.ledger import connect_writer
    from trading_bot.research.hypothesis_intake import (
        HypothesisProposal, run_intake,
    )
    from trading_bot.research.persona_runner import (
        PersonaInvocationError, SubprocessPersonaRunner,
    )

    persona_cmd_env = os.environ.get("TRADING_BOT_PERSONA_CMD", "claude,--json")
    command = tuple(s.strip() for s in persona_cmd_env.split(",") if s.strip())

    repo_root = Path(__file__).resolve().parents[3]
    rl_path = repo_root / "prompts" / "roles" / "quant_research_lead.v1.md"
    rv_path = repo_root / "prompts" / "roles" / "risk_validator.v1.md"
    if not rl_path.exists() or not rv_path.exists():
        return {"status": "error",
                "reason": f"persona files missing: {rl_path} / {rv_path}"}

    rl_runner = SubprocessPersonaRunner(
        role="quant_research_lead.v1", persona_path=rl_path, command=command,
    )
    rv_runner = SubprocessPersonaRunner(
        role="risk_validator.v1", persona_path=rv_path, command=command,
    )

    proposal = HypothesisProposal(
        thesis_id=f"{strategy_id}.thesis",
        hypothesis_id=f"{strategy_id}.h1",
        description=description,
        mechanism=(
            description
            if len(description) <= 400
            else description[:400] + "…"
        ),
        expected_regimes=("all",),
        kill_criteria=(
            "Sharpe < 0.3 over 6 months rolling",
            "max drawdown > 8%",
        ),
        proposed_by="operator",
    )

    conn = connect_writer(ledger_db)
    try:
        try:
            res = run_intake(
                conn,
                strategy_id=strategy_id, strategy_ver=1,
                hypothesis=proposal,
                research_lead_runner=rl_runner,
                risk_validator_runner=rv_runner,
                policy_hash="operator-intake",
                feature_snapshot_id="operator-intake",
            )
        except PersonaInvocationError as e:
            return {
                "status": "error",
                "reason": f"persona subprocess failed: {e}",
                "fix": (
                    "Set TRADING_BOT_PERSONA_CMD to a comma-separated "
                    "command that emits JSON on stdout. Default is "
                    "'claude,--json'."
                ),
                "strategy_id": strategy_id,
            }
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "accepted": res.accepted,
        "reason": res.reason,
        "hypothesis_hash": res.hypothesis_hash,
        "research_lead_verdict": res.research_lead_output.get("verdict"),
        "risk_validator_verdict": res.risk_validator_output.get("verdict"),
        "risk_validator_confidence": res.risk_validator_output.get("confidence"),
        "strategy_id": strategy_id,
        "operator": operator,
    }


def _run_mutate_stub(*, strategy_id: str, description: str, operator: str) -> dict:
    return {
        "status": "queued",
        "note": (
            "Mutation cycle queued. The daemon will pick this up on the "
            "monthly mutation-cycle tick. To run immediately, invoke "
            "`python -m trading_bot.research.run_mutation_cycle`."
        ),
        "strategy_id": strategy_id,
        "operator": operator,
    }


__all__ = [
    "INTAKE_MODES", "OPERATOR_HALT",
    "halt", "resume", "risk_profile_set", "risk_profile_show",
    "status_snapshot", "strategy_list", "strategy_promote",
    "strategy_submit",
]
