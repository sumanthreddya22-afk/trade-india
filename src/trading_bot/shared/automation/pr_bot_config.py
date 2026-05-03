"""PR-bot drift-comment runner — wires the Reza Karim persona to PR diffs.

Invoked by CI on every PR that touches files under
``src/trading_bot/pipelines/``. Builds the diff + structurally-symmetric
sibling file contents into a Reza Karim prompt; posts the resulting
comment to the PR (or the literal "NO_DRIFT_DETECTED" suppresses the
post).

Designed to be called from a GitHub Action / CI script:

    uv run python -m trading_bot.shared.automation.pr_bot_config \\
        --pr-diff-file /tmp/pr.diff --output-file /tmp/comment.md

The CI step then conditionally posts ``/tmp/comment.md`` as a PR comment
when its contents are not "NO_DRIFT_DETECTED".

This module never talks to GitHub directly — it only assembles the
prompt and writes the comment text. PR posting is the CI step's job.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Set, Tuple

PIPELINES = ("stocks", "crypto", "options")
PIPELINE_ROOT = Path("src/trading_bot/pipelines")
PIPELINE_DIFF_RE = re.compile(
    r"^\+\+\+ b/(src/trading_bot/pipelines/(?:stocks|crypto|options)/[\w/]+\.py)$",
    re.MULTILINE,
)


def changed_pipeline_files(diff_text: str) -> List[Path]:
    """Return the pipeline files referenced in a unified diff."""
    seen: Set[Path] = set()
    for m in PIPELINE_DIFF_RE.finditer(diff_text):
        seen.add(Path(m.group(1)))
    return sorted(seen)


def sibling_files(touched: Path) -> List[Path]:
    """Return existing sibling files in OTHER pipelines for one touched file."""
    try:
        rel = touched.relative_to(PIPELINE_ROOT)
    except ValueError:
        return []
    parts = rel.parts
    if len(parts) < 2 or parts[0] not in PIPELINES:
        return []
    asset, sub_path = parts[0], Path(*parts[1:])
    out: List[Path] = []
    for other in PIPELINES:
        if other == asset:
            continue
        candidate = PIPELINE_ROOT / other / sub_path
        if candidate.exists():
            out.append(candidate)
    return out


def build_sibling_block(touched_files: Iterable[Path]) -> str:
    """Render each touched file's sibling contents as a single text block."""
    chunks: List[str] = []
    for touched in touched_files:
        siblings = sibling_files(touched)
        if not siblings:
            continue
        for sib in siblings:
            chunks.append(f"=== {sib} ===\n{sib.read_text()}\n")
    return "\n".join(chunks) if chunks else "(no siblings found)"


def run_pr_bot(*, pr_diff: str, output_path: Path, dry_run: bool = False) -> str:
    """Produce the PR comment text. Returns the comment body."""
    sys.path.insert(0, str(Path("src").resolve()))
    from trading_bot.shared.personas.pr_bot import PERSONA
    from trading_bot.shared.personas._base import parse, render_prompt

    persona = parse(PERSONA)
    touched = changed_pipeline_files(pr_diff)
    if not touched:
        body = "NO_DRIFT_DETECTED"
        output_path.write_text(body)
        return body

    sibling_block = build_sibling_block(touched)
    rendered = render_prompt(
        persona,
        pr_diff_block=pr_diff,
        sibling_files_block=sibling_block,
    )

    if dry_run:
        print(rendered[:2000])
        body = "NO_DRIFT_DETECTED"
        output_path.write_text(body)
        return body

    from trading_bot.shared.llm_transport import get_transport

    transport = get_transport(role_name=persona.debate_role)
    response = transport.complete(
        system=rendered,
        messages=[{"role": "user", "content": "Produce the PR comment now."}],
    )
    body = response.text.strip()
    output_path.write_text(body)
    return body


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-diff-file", required=True, help="Path to the unified diff text")
    parser.add_argument("--output-file", required=True, help="Where to write the assembled comment body")
    parser.add_argument("--dry-run", action="store_true", help="Skip the LLM call; output 'NO_DRIFT_DETECTED'")
    args = parser.parse_args(argv)

    diff = Path(args.pr_diff_file).read_text()
    body = run_pr_bot(pr_diff=diff, output_path=Path(args.output_file), dry_run=args.dry_run)
    print(f"[pr-bot] wrote {args.output_file} ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
