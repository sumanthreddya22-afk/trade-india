# Migrating trading-bot v4 to a fresh Mac mini

Take this file, the packaged tarball, and your `.env` to the new Mac.
Every step below is copy-pasteable into Terminal. Estimated total time:
60–90 minutes.

---

## What you should have in front of you

Before you start, confirm you have:

- [ ] **The new Mac mini** powered on, signed in with your Apple ID.
- [ ] **The packaged tarball** (`trading_bot_v4_<date>.tar.gz`) on a USB
  stick OR uploaded somewhere you can `curl` from. (Created by
  `tools/package_for_macmini.sh` on the source Mac.)
- [ ] **Your `.env` file** with Alpaca paper credentials. Should *not*
  be inside the tarball — keep secrets separate.
- [ ] **The external SSD** (Samsung T7 Shield 1TB or equivalent),
  plugged in.
- [ ] **A wired Ethernet connection** (or stable Wi-Fi if no Ethernet).
- [ ] **Your Anthropic / Claude Code account login** (you'll re-sign in).

---

## 1. macOS preparation (~10 min)

Open **Terminal.app**.

### 1.1. Install Command Line Tools

```sh
xcode-select --install
```

(Click "Install" in the popup. Wait ~5 min.)

### 1.2. Install Homebrew

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Add brew to your shell after install completes:

```sh
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 1.3. Install Python 3.11, git, and gh

```sh
brew install python@3.11 git gh
```

Confirm:

```sh
python3.11 --version    # should print 3.11.x
git --version
gh --version
```

### 1.4. Sync clock (NTP is on by default, but verify)

```sh
sudo sntp -sS time.apple.com
```

(Plan v4: clock skew > 2s trips a kill switch. macOS keeps this within
ms by default; this command forces a one-shot sync to be safe.)

---

## 2. External SSD setup (~5 min)

Plug in the Samsung T7. Open **Disk Utility.app**.

1. Select the T7 in the left pane → **Erase**.
2. Name: `mirror`. Format: **APFS**. Scheme: **GUID Partition Map**.
3. Click **Erase** and wait.

Now from Terminal:

```sh
ls /Volumes/mirror    # should print the empty mount point
mkdir -p /Volumes/mirror/ledger /Volumes/mirror/archive
```

---

## 3. Restore the code (~5 min)

Choose the path that matches how you packaged the project.

### 3a. From a tarball (preferred)

Assume the tarball is at `~/Downloads/trading_bot_v4_2026-05-14.tar.gz`:

```sh
mkdir -p ~/Trading
cd ~/Trading
tar -xzf ~/Downloads/trading_bot_v4_2026-05-14.tar.gz --strip-components=1
ls    # should show CLAUDE.md, README.md, MIGRATION.md, src/, etc.
```

### 3b. From git (if you have a remote)

```sh
gh auth login    # sign in via browser
cd ~
gh repo clone <your-org>/Trading
cd Trading
```

### 3c. Copy `.env` (you brought this separately)

```sh
cp ~/Downloads/.env ~/Trading/.env    # or wherever you have it
chmod 600 ~/Trading/.env              # readable by you only
ls -la ~/Trading/.env                 # confirm 600 perms
```

### 3d. Restore Claude Code memory files (optional but recommended)

```sh
mkdir -p ~/.claude/projects/-Users-$USER-Trading
cp -R ~/Downloads/claude_memory ~/.claude/projects/-Users-$USER-Trading/memory
```

(The packaging script bundles your memory directory as
`claude_memory/`; rename matches the source Mac's project path.)

---

## 4. Python environment (~10 min)

```sh
cd ~/Trading
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e .
pip install -e ".[dev]"
```

Verify install:

```sh
bot --version
bot version
```

Expected: prints version info as JSON.

---

## 5. Initialize the ledger on the external SSD (~2 min)

Symlink the off-host mirror to the external SSD:

```sh
mkdir -p data/ledger
# Initialise a fresh ledger DB. Mirror lives on /Volumes/mirror.
python tools/init_ledger.py
# Replace the local mirror with a symlink to the external SSD.
mv data/ledger/mirror.db /Volumes/mirror/ledger/mirror.db
ln -s /Volumes/mirror/ledger/mirror.db data/ledger/mirror.db
ls -la data/ledger/    # should show mirror.db as a symlink
```

Verify the ledger:

```sh
python tools/verify_ledger.py
python tools/boot_check.py
```

Both must report `ok=True`. If anything fails, **stop** and read the
error.

---

## 6. Register the seed strategy (~1 min)

```sh
python tools/register_seed_strategy.py
bot strategy list
```

Expected output: one strategy (`ETF_MOMENTUM_v1`) at status
`research_only`.

---

## 7. Smoke test (~2 min)

Run the daemon in `--once` mode. It ticks every job exactly once and
exits with code 0:

```sh
bot daemon --once
```

Expected: a sequence of `INFO` lines, one per job. No `ERROR` lines.

Check heartbeats were written:

```sh
bot status
```

Expected: the `heartbeats` list has 6 rows (boot_check, market_data_ingest,
position_snapshot, orphan_loop, reconciliation, drift_monitor,
mutation_cycle).

Open the dashboard:

```sh
bot dashboard &
```

Then in a browser: **http://127.0.0.1:8765/**

You should see the status page with active kill switches (empty), the
seed strategy, and the heartbeats table.

Kill it:

```sh
kill %1
```

---

## 8. Set up launchd (auto-start on boot) (~5 min)

```sh
mkdir -p ~/Library/LaunchAgents
cp daemon/launchd/com.tradingbot.daemon.plist ~/Library/LaunchAgents/
# Edit the plist to substitute your home directory if needed:
sed -i '' "s|__USER__|$USER|g" ~/Library/LaunchAgents/com.tradingbot.daemon.plist
launchctl load ~/Library/LaunchAgents/com.tradingbot.daemon.plist
```

Confirm the daemon is running:

```sh
launchctl list | grep tradingbot
bot status
```

The `heartbeats` should start updating within 60s.

Also load the dashboard plist:

```sh
cp daemon/launchd/com.tradingbot.dashboard.plist ~/Library/LaunchAgents/
sed -i '' "s|__USER__|$USER|g" ~/Library/LaunchAgents/com.tradingbot.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.tradingbot.dashboard.plist
```

Now the dashboard is always at http://127.0.0.1:8765/.

---

## 9. Optional: re-install Claude Code CLI (~5 min)

If you'll use the mutation cycle, you need the `claude` CLI on PATH.
Follow Anthropic's install instructions for your shell, then sign in:

```sh
claude --version
# Sign in via browser to your account.
```

Without this, the mutation cycle stays in its default "skipped"
state — fine for paper observation.

---

## 10. Final verification

```sh
bot verify              # full boot check + chain verify
bot status              # daemon heartbeats fresh
pytest -x               # full test suite (~30s)
```

All three must be green.

---

## What you should NOT do

- Don't enable `TRADING_BOT_ENABLE_LLM_HOTPATH=1` until you've read the
  mutation cycle runbook.
- Don't flip `bot_mode` to `live` in `.env`. **Phase 9 ramp checklist
  gates that.**
- Don't run `recompute_hashes.py` casually — that's an explicit
  operator action that follows editing a `.lock` file.

---

## Daily routine after migration

Read [docs/runbooks/daily_ops.md](docs/runbooks/daily_ops.md).

## If something breaks

Read [docs/runbooks/incident_response.md](docs/runbooks/incident_response.md).

## Quarterly drill

Read [docs/runbooks/dr_drill.md](docs/runbooks/dr_drill.md).
