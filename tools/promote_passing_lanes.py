#!/usr/bin/env python
"""Promote strategies (and their lanes) based on the latest passing
validation_artifact for each.

Decision per strategy:
  * latest artifact PASSED → register a new strategy_version at
    status='tiny_paper' + advance lane_caps.lock[lane].status to
    'tiny_paper'.
  * latest artifact FAILED → register a new strategy_version at
    status='shadow' (per the operator's "Shadow-anyway" rule).
    lane_caps stays at whatever it was.

After all decisions, recomputes policy/HASHES.

Use:
  python tools/promote_passing_lanes.py            # do it
  python tools/promote_passing_lanes.py --dry-run  # preview
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


STRATEGIES = [
    # (strategy_id, lane)
    ("ETF_MOMENTUM_v1", "etf_momentum"),
    ("DUAL_MOMENTUM_v1", "dual_momentum"),
    ("CRYPTO_MOMENTUM_v1", "crypto_trend"),
    ("SPY_WHEEL_v1", "options_income_wheel"),
]


def _latest_artifact(conn, strategy_id):
    cur = conn.execute(
        "SELECT artifact_id, strategy_ver, tier, pass, code_hash, config_hash, "
        "       produced_ts, metrics_json "
        "FROM validation_artifact WHERE strategy_id=? "
        "ORDER BY produced_ts DESC LIMIT 1",
        (strategy_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "artifact_id": row[0], "strategy_ver": row[1], "tier": row[2],
        "passed": bool(row[3]), "code_hash": row[4], "config_hash": row[5],
        "produced_ts": row[6],
        "metrics": json.loads(row[7] or "{}"),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would change; don't write.")
    args = p.parse_args(argv)
    _load_env()

    from trading_bot.ledger import DEFAULT_LEDGER_PATH, connect_writer
    from trading_bot.registry import (
        VersionNotFound, get_active_version, register_version,
    )

    ledger = Path.cwd() / DEFAULT_LEDGER_PATH
    if not ledger.exists():
        print(f"FAIL: {ledger} missing", file=sys.stderr)
        return 1

    lane_caps_path = Path("policy/lane_caps.lock")
    if not lane_caps_path.exists():
        print(f"FAIL: {lane_caps_path} missing", file=sys.stderr)
        return 1
    lane_caps = json.loads(lane_caps_path.read_text())

    operator = "operator"

    conn = connect_writer(ledger)
    plan: list[dict] = []
    for sid, lane in STRATEGIES:
        artifact = _latest_artifact(conn, sid)
        try:
            current = get_active_version(conn, sid)
        except VersionNotFound:
            plan.append({"sid": sid, "lane": lane, "action": "skip",
                         "reason": "strategy not registered (run register_strategies.py)"})
            continue
        if artifact is None:
            plan.append({"sid": sid, "lane": lane, "action": "skip",
                         "reason": "no validation_artifact yet"})
            continue

        target_strat_status = "tiny_paper" if artifact["passed"] else "shadow"
        if current.status == target_strat_status:
            plan.append({"sid": sid, "lane": lane, "action": "noop_strategy",
                         "current_status": current.status,
                         "artifact_id": artifact["artifact_id"]})
        else:
            plan.append({
                "sid": sid, "lane": lane,
                "action": "register_new_version",
                "from": current.status, "to": target_strat_status,
                "artifact_id": artifact["artifact_id"],
                "passed": artifact["passed"],
            })

        if lane not in lane_caps["lanes"]:
            # New lane needs to be created with the appropriate status.
            target_lane_status = "tiny_paper" if artifact["passed"] else "shadow"
            plan.append({
                "sid": sid, "lane": lane, "action": "create_lane",
                "to": target_lane_status,
            })
        else:
            lane_status_now = lane_caps["lanes"][lane]["status"]
            target_lane_status = (
                "tiny_paper" if artifact["passed"]
                else "shadow"
            )
            if lane_status_now != target_lane_status:
                plan.append({
                    "sid": sid, "lane": lane, "action": "update_lane_caps",
                    "from": lane_status_now, "to": target_lane_status,
                })

    print(json.dumps(plan, indent=2))

    if args.dry_run:
        print("\nDRY RUN — no changes written.")
        conn.close()
        return 0

    # Apply strategy_version writes
    today = dt.date.today()
    for step in plan:
        if step["action"] != "register_new_version":
            continue
        sid = step["sid"]
        artifact_id = step["artifact_id"]
        current = get_active_version(conn, sid)
        register_version(
            conn,
            strategy_id=sid,
            strategy_ver=current.strategy_ver + 1,
            code_hash=current.code_hash, config_hash=current.config_hash,
            thesis_id=current.thesis_id, hypothesis_id=current.hypothesis_id,
            validation_artifact_id=artifact_id,
            lane=current.lane, status=step["to"],
            expiry_date=current.expiry_date,
            owner=f"{operator} (Phase 9 promote, artifact {artifact_id[:8]})",
        )
    conn.commit()
    conn.close()

    # Apply lane_caps updates (in-place edit + recompute hashes)
    lane_changes_applied = []
    for step in plan:
        if step["action"] == "create_lane":
            lane = step["lane"]
            lane_caps["lanes"][lane] = {
                "status": step["to"],
                "_status_note": (
                    f"Phase 9 auto-create {today.isoformat()}: "
                    f"lane created at {step['to']} after validation."
                ),
                "thesis_id": None,
                "demotion_criteria": [
                    "any risk_policy.lock tier breach",
                    "any kill criterion trip",
                ],
            }
            lane_changes_applied.append(step)
        elif step["action"] == "update_lane_caps":
            lane = step["lane"]
            lane_caps["lanes"][lane]["status"] = step["to"]
            lane_caps["lanes"][lane]["_status_note"] = (
                f"Phase 9 auto-promote {today.isoformat()}: "
                f"validation result drove status → {step['to']}. "
                "Previous note preserved in git history."
            )
            lane_changes_applied.append(step)
    if lane_changes_applied:
        lane_caps["lock_version"] = f"{today.isoformat()}.v4-phase9"
        lane_caps["_phase9_audit"] = lane_caps.get("_phase9_audit", []) + [
            {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
             "operator": operator, "applied": lane_changes_applied}
        ]
        lane_caps_path.write_text(json.dumps(lane_caps, indent=2) + "\n")

        # Recompute HASHES so the policy loader doesn't refuse to boot.
        out = subprocess.run(
            [sys.executable, "tools/recompute_hashes.py"],
            capture_output=True, text=True, check=False,
        )
        print("recompute_hashes.py:", out.stdout, out.stderr)

    print("\nPromotion complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
