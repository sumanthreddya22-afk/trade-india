"""Helper for the LLM mailbox routine — writes a result file atomically
and moves the brief from pending/ → processed/.

Used by the scheduled Claude Code routine described in
tools/llm_mailbox_routine.md. The routine reasons through a brief in its
own session, then invokes this script to persist the verdict for the
trading-bot daemon to pick up.

Usage:
    .venv/bin/python tools/mailbox_write_result.py \
        --id "20260501T231100Z-decision_reflector-abc12345" \
        --result-file /tmp/result.json

The result-file must contain a JSON object matching the result schema
documented in src/trading_bot/llm_mailbox.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from trading_bot.llm_mailbox import MailboxQueue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="brief id to write a result for")
    parser.add_argument(
        "--result-file", type=Path, required=True,
        help="path to a JSON file containing the result payload",
    )
    parser.add_argument(
        "--mailbox-base", type=Path, default=Path("data/llm_queue"),
        help="mailbox base dir (default: data/llm_queue)",
    )
    args = parser.parse_args()

    if not args.result_file.exists():
        print(f"result-file does not exist: {args.result_file}", file=sys.stderr)
        return 2

    try:
        payload = json.loads(args.result_file.read_text())
    except json.JSONDecodeError as e:
        print(f"result-file is not valid JSON: {e}", file=sys.stderr)
        return 2

    # Inject id + completed_at_utc if the routine didn't.
    payload.setdefault("id", args.id)
    payload.setdefault(
        "completed_at_utc",
        dt.datetime.now(dt.timezone.utc).isoformat(),
    )

    mailbox = MailboxQueue(base=args.mailbox_base)
    mailbox.write_result(args.id, result=payload)
    print(f"wrote result for {args.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
