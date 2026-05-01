"""Manually clear (or set) the strategy_coach fallback gate.

The fallback flag turns off all stock + crypto trading by short-circuiting
the scanners. It's normally driven by strategy_coach's once-daily alpha
evaluation, but operator overrides are needed when the automation
misfires (e.g. the 2026-04-29 zombie-rows incident) or when post-cleanup
work needs to reopen trading without waiting for the next scheduled run.

Usage:
    .venv/bin/python tools/clear_fallback_flag.py \\
        --reason "post-cleanup, zombies removed, warmup guard installed"

    # Set ON instead (rare — usually only for testing)
    .venv/bin/python tools/clear_fallback_flag.py --on --reason "testing"

    # Inspect current state without writing
    .venv/bin/python tools/clear_fallback_flag.py --show
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy.orm import Session

from trading_bot.state_db import get_engine
from trading_bot.state_fallback import current_flag, set_flag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="data/state.db",
        help="Path to state.db (default: data/state.db)",
    )
    parser.add_argument(
        "--on", action="store_true",
        help="Set fallback_active=1 instead of clearing it.",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print the current flag state and exit without writing.",
    )
    parser.add_argument(
        "--reason", default="manual override",
        help="Audit trail reason recorded with the flip.",
    )
    args = parser.parse_args()

    engine = get_engine(args.db)

    if args.show:
        with Session(engine) as session:
            flag = current_flag(session)
        if flag is None:
            print("no fallback_flag rows yet")
            return 0
        state = "ACTIVE (trading halted)" if flag.fallback_active else "off (trading allowed)"
        print(f"fallback_active = {flag.fallback_active}  ({state})")
        print(f"set_at          = {flag.set_at}")
        print(f"set_by          = {flag.set_by}")
        print(f"reason          = {flag.reason}")
        return 0

    new_state = bool(args.on)

    with Session(engine) as session:
        prev = current_flag(session)
        prev_state = bool(prev and prev.fallback_active)
        if prev_state == new_state:
            print(f"no-op: fallback_active is already {int(new_state)}")
            return 0
        set_flag(
            session,
            fallback_active=new_state,
            set_by="manual",
            reason=args.reason,
        )

    print(
        f"fallback_active flipped {int(prev_state)} -> {int(new_state)}  "
        f"(reason: {args.reason})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
