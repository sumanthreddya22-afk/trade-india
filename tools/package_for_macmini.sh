#!/usr/bin/env bash
# Bundle the repo (sans secrets and build artifacts) plus the Claude
# Code memory directory into a tarball ready to copy to the Mac mini.
#
# Output: ../trading_bot_v4_YYYY-MM-DD.tar.gz + SHA256
#
# The script refuses to include .env and *.db so secrets and live state
# never leave this machine accidentally. The operator copies .env
# separately (USB stick, encrypted volume, password manager export).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DATE="$(date +%Y-%m-%d)"
OUT_DIR="$(cd "$REPO_ROOT/.." && pwd)"
OUT_NAME="trading_bot_v4_${DATE}"
OUT_PATH="$OUT_DIR/${OUT_NAME}.tar.gz"
SHA_PATH="$OUT_DIR/${OUT_NAME}.sha256"

echo "==> Packaging $OUT_PATH"

# Safety check: refuse to package if there are uncommitted changes
# unless --allow-dirty is passed.
ALLOW_DIRTY="${1:-}"
if [[ -z "$ALLOW_DIRTY" || "$ALLOW_DIRTY" != "--allow-dirty" ]]; then
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "ERROR: uncommitted changes. Commit them or rerun with --allow-dirty." >&2
    exit 1
  fi
fi

# Build the tarball. Exclude:
#   - virtualenvs
#   - any .env*
#   - all .db / .sqlite (live ledger state)
#   - data/ (regenerated on the Mac mini)
#   - .pytest_cache / __pycache__
#   - .claude/ (worktrees + scheduled tasks lock)
#   - .git/ is INCLUDED so the operator gets full history; comment out
#     the include below to ship a code-only bundle.
TAR_EXCLUDES=(
  --exclude=".venv"
  --exclude="venv"
  --exclude=".env"
  --exclude=".env.*"
  --exclude="*.db"
  --exclude="*.db-shm"
  --exclude="*.db-wal"
  --exclude="*.sqlite"
  --exclude="*.sqlite3"
  --exclude="data"
  --exclude=".pytest_cache"
  --exclude="__pycache__"
  --exclude=".claude"
  --exclude=".scratch"
  --exclude=".codex-inspiration"
  --exclude="archive"
  --exclude="runs"
  --exclude="strategy/opportunities.md"
  --exclude="strategy/latest_intelligence.md"
  --exclude="strategy/backtest_results.md"
)

# Stage the bundle in a temp dir so we can include sibling files like
# the operator's memory dump.
STAGE="$(mktemp -d -t tradingbot_pkg_XXXX)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> Staging repo..."
mkdir -p "$STAGE/$OUT_NAME"
tar -cf - "${TAR_EXCLUDES[@]}" . | tar -xf - -C "$STAGE/$OUT_NAME"

# Optionally bundle Claude Code memory if it exists at the canonical path.
MEMORY_DIR="$HOME/.claude/projects/-Users-$USER-Trading/memory"
if [[ -d "$MEMORY_DIR" ]]; then
  echo "==> Including Claude Code memory directory"
  mkdir -p "$STAGE/$OUT_NAME/claude_memory"
  cp -R "$MEMORY_DIR"/* "$STAGE/$OUT_NAME/claude_memory/" 2>/dev/null || true
else
  echo "==> No Claude Code memory at $MEMORY_DIR (skipping)"
fi

# Write a manifest with git SHA + python version for future audit.
cat > "$STAGE/$OUT_NAME/PACKAGE_MANIFEST.txt" <<EOF
Package:      $OUT_NAME
Built:        $(date -u +"%Y-%m-%dT%H:%M:%SZ")
Built on:     $(uname -a)
Git SHA:      $(git rev-parse HEAD 2>/dev/null || echo "no-git")
Git branch:   $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "no-git")
Python:       $(python3 --version 2>&1)
Repo size:    $(du -sh "$STAGE/$OUT_NAME" | cut -f1)
EOF

echo "==> Creating tarball..."
tar -czf "$OUT_PATH" -C "$STAGE" "$OUT_NAME"

echo "==> Computing SHA-256..."
shasum -a 256 "$OUT_PATH" > "$SHA_PATH"

echo
echo "DONE."
echo "  Tarball: $OUT_PATH"
echo "  SHA:     $SHA_PATH"
echo "  Size:    $(du -sh "$OUT_PATH" | cut -f1)"
echo
echo "Next steps:"
echo "  1. Verify on this Mac: shasum -a 256 -c $SHA_PATH"
echo "  2. Copy $OUT_PATH (+ .env separately) to the Mac mini."
echo "  3. Follow MIGRATION.md."
