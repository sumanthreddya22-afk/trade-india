#!/usr/bin/env bash
# Install trading bot launchd plists and start them.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
DAEMON_LABEL="com.bharath.trading.daemon.paper"
SUPERVISOR_LABEL="com.bharath.trading.supervisor"

echo "Installing launchd plists from $REPO_ROOT/ops/launchd to $LAUNCHD_DIR"
mkdir -p "$LAUNCHD_DIR"
mkdir -p "$REPO_ROOT/runs/_launchd"
mkdir -p "$REPO_ROOT/data"

# Copy plists (source of truth in repo; runtime copy under LaunchAgents)
cp "$REPO_ROOT/ops/launchd/${DAEMON_LABEL}.plist" "$LAUNCHD_DIR/"
cp "$REPO_ROOT/ops/launchd/${SUPERVISOR_LABEL}.plist" "$LAUNCHD_DIR/"

# Run Alembic migrations to ensure state.db schema is current
cd "$REPO_ROOT"
"$REPO_ROOT/.venv/bin/alembic" -c migrations/alembic.ini upgrade head

# Unload if already loaded (idempotent), then load
launchctl unload "$LAUNCHD_DIR/${DAEMON_LABEL}.plist" 2>/dev/null || true
launchctl unload "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist" 2>/dev/null || true
launchctl load -w "$LAUNCHD_DIR/${DAEMON_LABEL}.plist"
launchctl load -w "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist"

echo "Installed and loaded:"
launchctl list | grep -E "${DAEMON_LABEL}|${SUPERVISOR_LABEL}" || true
echo
echo "Logs at: $REPO_ROOT/runs/_launchd/"
echo "Heartbeat: $REPO_ROOT/data/heartbeat.json"
echo "Done."
