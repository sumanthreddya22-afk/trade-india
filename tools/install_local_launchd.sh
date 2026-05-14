#!/usr/bin/env bash
# Install + load the local-Mac launchd units for the daemon and
# dashboard. Idempotent — re-run safely after pulling new code.
#
#   bash tools/install_local_launchd.sh         # install + load
#   bash tools/install_local_launchd.sh stop    # unload only

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

DAEMON_NAME="com.tradingbot.local.daemon"
DASH_NAME="com.tradingbot.local.dashboard"
DAEMON_PLIST="$LA/$DAEMON_NAME.plist"
DASH_PLIST="$LA/$DASH_NAME.plist"

unload_if_loaded() {
  local plist="$1"
  local name="$2"
  if launchctl list 2>/dev/null | grep -q "$name"; then
    echo "==> Unloading $name"
    launchctl unload "$plist" 2>/dev/null || true
  fi
}

if [[ "${1:-}" == "stop" ]]; then
  unload_if_loaded "$DAEMON_PLIST" "$DAEMON_NAME"
  unload_if_loaded "$DASH_PLIST" "$DASH_NAME"
  echo "stopped."
  exit 0
fi

# Pre-flight: confirm .venv/bin/bot exists.
if [[ ! -x "$REPO_ROOT/.venv/bin/bot" ]]; then
  echo "ERROR: $REPO_ROOT/.venv/bin/bot not found." >&2
  echo "Run: source .venv/bin/activate && pip install -e .[dev]" >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/data"

# Copy + substitute.
cp "$REPO_ROOT/daemon/launchd/$DAEMON_NAME.plist" "$DAEMON_PLIST"
cp "$REPO_ROOT/daemon/launchd/$DASH_NAME.plist"   "$DASH_PLIST"

USER_NAME="$(id -un)"
PY_BIN="$(command -v python3.11 || command -v python3)"
sed -i '' "s|__USER__|$USER_NAME|g" "$DAEMON_PLIST" "$DASH_PLIST"
sed -i '' "s|__PYTHON_BIN__|$PY_BIN|g" "$DAEMON_PLIST" "$DASH_PLIST"

# Reload (unload first to pick up plist edits).
unload_if_loaded "$DAEMON_PLIST" "$DAEMON_NAME"
unload_if_loaded "$DASH_PLIST" "$DASH_NAME"

echo "==> Loading $DAEMON_NAME"
launchctl load "$DAEMON_PLIST"
echo "==> Loading $DASH_NAME"
launchctl load "$DASH_PLIST"

sleep 1
echo
echo "Status:"
launchctl list | grep -E "tradingbot|PID" || true
echo
echo "Tail logs:"
echo "  tail -F $REPO_ROOT/data/daemon.log"
echo "  tail -F $REPO_ROOT/data/dashboard.stderr.log"
echo
echo "Dashboard: http://127.0.0.1:8765/"
