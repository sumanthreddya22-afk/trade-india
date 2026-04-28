#!/usr/bin/env bash
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
DAEMON_LABEL="com.bharath.trading.daemon.paper"
SUPERVISOR_LABEL="com.bharath.trading.supervisor"

launchctl unload "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist" 2>/dev/null || true
launchctl unload "$LAUNCHD_DIR/${DAEMON_LABEL}.plist" 2>/dev/null || true
rm -f "$LAUNCHD_DIR/${SUPERVISOR_LABEL}.plist"
rm -f "$LAUNCHD_DIR/${DAEMON_LABEL}.plist"

echo "Unloaded and removed plists. State databases and logs left intact."
