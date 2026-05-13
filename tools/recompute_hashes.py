#!/usr/bin/env python3
"""Recompute policy/HASHES.

Walks ``policy/*.lock``, ``prompts/roles/*.md``, and the active edge thesis
files, computes a SHA-256 of each, and rewrites ``policy/HASHES`` in the
shape that Plan v4 §0 specifies:

    <64-hex sha256>  <relative_path>

One entry per line, sorted by relative path. Idempotent: running twice in
a row leaves the file unchanged unless a tracked file's content changed.

Invocation:

    python tools/recompute_hashes.py            # rewrite HASHES
    python tools/recompute_hashes.py --check    # exit 1 if HASHES is stale

The L0 governance startup check (Phase 2) will run the equivalent of
``--check`` and refuse to start the kernel on mismatch.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HASHES_PATH = REPO_ROOT / "policy" / "HASHES"

# Files that go into the hash manifest. Globs are evaluated against the
# repo root. Order does not matter — the writer sorts by relative path.
TRACKED_GLOBS: tuple[str, ...] = (
    "policy/*.lock",
    "prompts/roles/*.md",
    "docs/edge_thesis_v*.md",
    "research/search_space_v*.json",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_tracked() -> list[Path]:
    out: set[Path] = set()
    for glob in TRACKED_GLOBS:
        out.update(REPO_ROOT.glob(glob))
    # Stable order for diffing.
    return sorted(out)


def _render(paths: list[Path]) -> str:
    lines = []
    for p in paths:
        rel = p.relative_to(REPO_ROOT).as_posix()
        digest = _sha256(p)
        lines.append(f"{digest}  {rel}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if policy/HASHES is out of date",
    )
    args = parser.parse_args()

    tracked = _collect_tracked()
    if not tracked:
        print("WARN: no tracked files matched; nothing to hash", file=sys.stderr)
        return 1

    rendered = _render(tracked)

    if args.check:
        existing = HASHES_PATH.read_text() if HASHES_PATH.exists() else ""
        if existing == rendered:
            print(f"OK: policy/HASHES matches {len(tracked)} tracked files")
            return 0
        print(
            "MISMATCH: policy/HASHES is stale. Run "
            "`python tools/recompute_hashes.py` and commit the result.",
            file=sys.stderr,
        )
        return 1

    HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    HASHES_PATH.write_text(rendered)
    print(f"wrote {HASHES_PATH} ({len(tracked)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
