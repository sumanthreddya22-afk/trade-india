#!/usr/bin/env bash
# Cross-pipeline drift pre-commit hook (delegates to Python module).
#
# Install:
#   cp src/trading_bot/shared/automation/pre_commit.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or wire from any existing pre-commit framework.
#
# Always exits 0 — drift is a nudge, not a block.

set -e

cd "$(git rev-parse --show-toplevel)"

if command -v uv >/dev/null 2>&1; then
  uv run python -m trading_bot.shared.automation.pre_commit "$@" || true
else
  python -m trading_bot.shared.automation.pre_commit "$@" || true
fi

exit 0
