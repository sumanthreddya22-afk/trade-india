#!/usr/bin/env bash
# Cleanly stop and remove the LIVE trading daemon. No confirmation —
# easy reversal is desirable. Paper daemon is left untouched.
set -euo pipefail

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LIVE_LABEL="com.bharath.trading.daemon.live"

launchctl unload "$LAUNCHD_DIR/${LIVE_LABEL}.plist" 2>/dev/null || true
rm -f "$LAUNCHD_DIR/${LIVE_LABEL}.plist"

echo "Live daemon unloaded and removed."
echo "data/live_active.json and state.db are preserved (manual cleanup if desired)."
