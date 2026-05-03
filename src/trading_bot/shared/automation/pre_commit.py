"""Cross-pipeline drift pre-commit hook.

When you stage changes under ``src/trading_bot/pipelines/<asset>/<file>`` and a
structurally-symmetric file exists in another pipeline, this hook prints a
one-line nudge naming the symmetric files. Doesn't block — just surfaces
the cross-pollination question while it's cheap to act on.

Run as: ``python -m trading_bot.shared.automation.pre_commit``

Wire as a git hook from project root:
    cp src/trading_bot/shared/automation/pre_commit.sh .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
(or invoke directly from any existing pre-commit framework).

Zero LLM cost. Pure file-existence comparison.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Set, Tuple

PIPELINES = ("stocks", "crypto", "options")
PIPELINE_ROOT = Path("src/trading_bot/pipelines")


def staged_files() -> List[Path]:
    """Return paths git is about to commit (added, copied, modified, renamed)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def _pipeline_relative(path: Path) -> Tuple[str, Path] | None:
    """If ``path`` is under ``pipelines/<asset>/...`` return (asset, rel-to-asset)."""
    try:
        rel = path.relative_to(PIPELINE_ROOT)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2 or parts[0] not in PIPELINES:
        return None
    return parts[0], Path(*parts[1:])


def find_siblings(touched: Path) -> Set[Tuple[str, Path]]:
    """Return existing structurally-symmetric files in the OTHER pipelines.

    A sibling has the same ``<rel-to-asset>`` path under a different
    pipeline. Only siblings that actually exist on disk are returned.
    """
    info = _pipeline_relative(touched)
    if not info:
        return set()
    asset, rel = info
    siblings: Set[Tuple[str, Path]] = set()
    for other in PIPELINES:
        if other == asset:
            continue
        candidate = PIPELINE_ROOT / other / rel
        if candidate.exists():
            siblings.add((other, candidate))
    return siblings


def emit_nudge(touched: Path, siblings: Set[Tuple[str, Path]]) -> None:
    """Print a single-line nudge for one touched file."""
    info = _pipeline_relative(touched)
    if not info:
        return
    asset, rel = info
    sibling_str = ", ".join(sorted(f"{p}" for _, p in siblings))
    print(
        f"[drift-nudge] {touched} touched. Sibling(s) exist in: {sibling_str}.\n"
        f"             Should the change apply there? (Reply 'intentional' in PR if not.)"
    )


def main(argv: List[str] | None = None) -> int:
    files = staged_files()
    if not files:
        return 0
    nudges = 0
    for f in files:
        siblings = find_siblings(f)
        if siblings:
            emit_nudge(f, siblings)
            nudges += 1
    if nudges:
        # Hint, never block.
        print(f"[drift-nudge] {nudges} cross-pipeline nudge(s); commit continuing.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
