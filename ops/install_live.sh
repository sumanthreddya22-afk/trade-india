#!/usr/bin/env bash
# Install the LIVE trading daemon. SECOND-STEP graduation gate.
# Requires bot promote --target=live to have been run first (data/live_active.json
# must exist). Requires ALPACA_LIVE_API_KEY in your shell environment. Requires a
# typed confirmation. Each layer is independent — no flag bypasses any of them.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LIVE_LABEL="com.bharath.trading.daemon.live"
DAEMON_LABEL="com.bharath.trading.daemon.paper"

CONFIRM_STRING="GRADUATE TO LIVE TRADING"

# Gate 1: live config must exist (operator ran bot promote --target=live).
if [ ! -f "$REPO_ROOT/data/live_active.json" ]; then
  echo "REFUSED: data/live_active.json does not exist." >&2
  echo "  Run: bot promote --target=live --i-know-this-is-real-money first." >&2
  exit 1
fi

# Gate 2: live Alpaca creds in shell env.
if [ -z "${ALPACA_LIVE_API_KEY:-}" ] || [ -z "${ALPACA_LIVE_API_SECRET:-}" ]; then
  echo "REFUSED: ALPACA_LIVE_API_KEY and ALPACA_LIVE_API_SECRET must both be set." >&2
  echo "  Add them to your shell profile (~/.zshrc, ~/.bashrc) and retry." >&2
  exit 1
fi

# Gate 3: paper daemon must already be installed (live presupposes paper exists).
if ! launchctl list | grep -q "$DAEMON_LABEL"; then
  echo "REFUSED: paper daemon ($DAEMON_LABEL) is not loaded." >&2
  echo "  Run: ops/install.sh first to bring up the paper system." >&2
  exit 1
fi

# Banner.
cat <<'BANNER'

======================================================================
  GRADUATING TO LIVE TRADING
======================================================================

This installs the live trading daemon as a launchd-managed process.
It will trade with REAL MONEY using your live Alpaca account.

The daemon will read data/live_active.json and use the credentials in
ALPACA_LIVE_API_KEY / ALPACA_LIVE_API_SECRET. Stricter risk caps apply
automatically (5% max position, 1.5% daily loss, 10% max drawdown).

Real money is at stake.

======================================================================
BANNER

# Gate 4: typed confirmation. Exact string match. NO substring, NO case-insensitive.
read -r -p "Type \"$CONFIRM_STRING\" to proceed (any other input cancels): " RESPONSE
if [ "$RESPONSE" != "$CONFIRM_STRING" ]; then
  echo "REFUSED: confirmation did not match. Live daemon NOT installed." >&2
  exit 1
fi

# All gates passed.
echo "Installing live daemon plist..."
mkdir -p "$LAUNCHD_DIR"
mkdir -p "$REPO_ROOT/runs/_launchd"
cp "$REPO_ROOT/ops/launchd/${LIVE_LABEL}.plist" "$LAUNCHD_DIR/"
launchctl unload "$LAUNCHD_DIR/${LIVE_LABEL}.plist" 2>/dev/null || true
launchctl load -w "$LAUNCHD_DIR/${LIVE_LABEL}.plist"

echo
echo "LIVE daemon loaded:"
launchctl list | grep "$LIVE_LABEL" || true
echo
echo "Live heartbeat: $REPO_ROOT/data/heartbeat_live.json"
echo "Live logs:      $REPO_ROOT/runs/_launchd/daemon_live.std{out,err}.log"
echo
echo "To stop live trading immediately:"
echo "  touch $REPO_ROOT/data/pause_live.flag    # all orders vetoed"
echo "  ops/uninstall_live.sh                    # full unload"
