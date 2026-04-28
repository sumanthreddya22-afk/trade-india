# Phase 6 — `bot promote` CLI + Live Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the manual gate the operator uses to graduate a paper-validated config to live trading. Adds the `bot promote --target=paper|live` CLI + a separate `live_active.json` config + a live daemon launchd plist + tighter Risk Officer caps for live mode. The CLI **never** flips to live without explicit, multi-step operator confirmation. Spec §12 is unambiguous: "The only manual touchpoint in the entire system is the `bot promote` CLI command, which is a deliberate, conscious decision the owner makes."

**Architecture:** Phase 6 is mostly CLI plumbing + config duplication. The daemon code is unchanged — it reads whichever `*_active.json` it's pointed at via `TRADING_BOT_CONFIG`. The live daemon is a second launchd plist with `TRADING_BOT_CONFIG=live_active.json` + `ALPACA_LIVE_API_KEY`/`SECRET` instead of paper creds. Risk Officer reads risk_caps from the active config; live mode just supplies stricter values.

**The hard guardrail (NON-NEGOTIABLE):** `bot promote --target=live` **must refuse** without all three of:
1. `ALPACA_LIVE_API_KEY` AND `ALPACA_LIVE_API_SECRET` set in env.
2. `--i-know-this-is-real-money` flag passed explicitly.
3. Operator types the literal string `"YES, FLIP TO LIVE"` at a confirmation prompt.

Without all three, the CLI exits 1 with an explanation. **No way to bypass via flag, env var, or alias.**

**Bootstrap default:** Live daemon plist is created but **NOT auto-loaded by `ops/install.sh`**. Operator runs a separate `ops/install_live.sh` (with the same multi-step confirmation) when they're ready to graduate. Out of the box, only paper runs.

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md) §12 (autonomy enforcement, the manual touchpoint).

---

## File structure for Phase 6

### New files
```
src/trading_bot/
  promote_cli.py                 # The bot promote command + safety gates

ops/
  live_active.template.json      # Template — operator manually edits before first promote
  install_live.sh                # Manual install of the live daemon, with confirmation
  uninstall_live.sh              # Reverse of above
  launchd/
    com.bharath.trading.daemon.live.plist

tests/
  test_promote_cli.py            # Especially: test_live_promote_refuses_without_*
```

### Files modified
- `src/trading_bot/cli.py` — add `bot promote` subcommand
- `src/trading_bot/risk_manager.py` — read mode-specific caps from active config (already does; just expose `mode` field for digest)
- `src/trading_bot/config.py` — `Settings` reads either paper or live Alpaca creds based on `TRADING_BOT_MODE` env var
- `src/trading_bot/alpaca_client.py` — supports `paper` and `live` REST endpoints
- `src/trading_bot/reports.py` — daily digest header makes mode obvious ("MODE: LIVE" in red)
- `ops/install.sh` — explicitly does NOT load the live plist; references install_live.sh for live setup

---

## Task 1 — Live Alpaca creds + mode-aware config

**Files:** `src/trading_bot/config.py`, `tests/test_config.py`

`Settings` gets a `mode: Literal["paper", "live"]` field driven by `TRADING_BOT_MODE` env (default "paper"). `alpaca_api_key` / `alpaca_api_secret` resolution:
- paper mode: `ALPACA_API_KEY` / `ALPACA_API_SECRET`.
- live mode: `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_API_SECRET` — fail loudly if either missing.

`Settings.endpoint_url` returns `https://paper-api.alpaca.markets` or `https://api.alpaca.markets` accordingly.

- [ ] Tests for both modes (mocked env).
- [ ] Implement.
- [ ] Commit.

---

## Task 2 — `live_active.json` template + risk-cap delta

**Files:** `ops/live_active.template.json`

Same shape as `paper_active.template.json` BUT with stricter risk_caps (locked):
- `max_position_pct`: 5 (vs paper's 10) — half the position sizing.
- `daily_loss_pct`: 1.5 (vs paper's 3) — half the daily loss tolerance.
- `max_drawdown_pct`: 10 (vs paper's 20) — half the drawdown ceiling.
- `bot_mode`: `"live"`.

Phase 6 ships only the template. The first live config is initialized by `bot promote --target=live` from the paper config, with these caps overridden.

- [ ] Add the file.
- [ ] Commit.

---

## Task 3 — `bot promote` CLI

**Files:** `src/trading_bot/promote_cli.py`, `src/trading_bot/cli.py`, `tests/test_promote_cli.py`

```bash
bot promote --target=paper [--leaderboard-id=N]
bot promote --target=live --i-know-this-is-real-money
```

`--target=paper` flow:
- Reads top leaderboard row (or specified id) from state.db.
- Calls `should_promote(...)` from Phase 3's promotion module.
- Calls `promote_atomically(paper_active.json, ...)`.
- Prints what changed.

`--target=live` flow — the gate:
1. Verify env vars: `ALPACA_LIVE_API_KEY` AND `ALPACA_LIVE_API_SECRET` MUST exist.
2. Verify `--i-know-this-is-real-money` flag was passed.
3. Print a dramatic banner with the proposed change, the live caps that will apply, and the current paper config's recent KPIs.
4. Prompt: `Type "YES, FLIP TO LIVE" to proceed (any other input cancels):`
5. If exact string match: copy paper_active.json → live_active.json with risk_caps overridden to the locked live values; record a `ConfigHistory` row (existing table) with `account="live"`, `promoted_by="cli"`.
6. If anything fails: exit 1 with the reason.

Critical implementation note: the CLI uses `click.prompt(..., hide_input=False)` with explicit equality check on the literal string. NO regex. NO substring. NO case-insensitive. Type it exactly or it cancels.

- [ ] Test 1: `--target=paper` happy path (mocked state.db).
- [ ] Test 2: `--target=live` without env vars → exit 1, no file written.
- [ ] Test 3: `--target=live` without flag → exit 1.
- [ ] Test 4: `--target=live` with everything set, but operator types "yes" lowercase → exit 1, no file written.
- [ ] Test 5: `--target=live` with everything set, exact string typed → file written, ConfigHistory recorded.
- [ ] Implement.
- [ ] Commit.

---

## Task 4 — Live daemon plist (built but NOT loaded by default)

**Files:** `ops/launchd/com.bharath.trading.daemon.live.plist`

Mirror of paper plist with:
- Label: `com.bharath.trading.daemon.live`.
- ProgramArguments: same `python -m trading_bot.daemon`.
- EnvironmentVariables:
  - `TRADING_BOT_CONFIG=/Users/bharathkandala/Trading/data/live_active.json`
  - `TRADING_BOT_MODE=live`
  - `TRADING_BOT_HEARTBEAT=/Users/bharathkandala/Trading/data/heartbeat_live.json`
  - `TRADING_BOT_PAUSE=/Users/bharathkandala/Trading/data/pause_live.flag`
  - `TRADING_BOT_RUNS=/Users/bharathkandala/Trading/runs`
  - `TRADING_BOT_STATE_DB=/Users/bharathkandala/Trading/data/state.db` (shared with paper for unified leaderboard/KPIs)
  - `PYTHONPATH=/Users/bharathkandala/Trading/src`
- StandardOutPath / StandardErrorPath: `runs/_launchd/daemon_live.std{out,err}.log`

Note the separate heartbeat + pause flag files so paper and live each have their own supervisor signals (the supervisor's StallDetector watches both when both are loaded — Phase 1's design).

- [ ] Create plist.
- [ ] `plutil -lint` passes.
- [ ] Commit.

---

## Task 5 — `ops/install_live.sh` (gated install)

**Files:** `ops/install_live.sh`, `ops/uninstall_live.sh`

`install_live.sh` script flow:
1. Verify `data/live_active.json` exists (operator must have run `bot promote --target=live` first).
2. Verify env has `ALPACA_LIVE_API_KEY` set in user's shell profile (greps for it).
3. Verify the operator has run `ops/install.sh` first (paper daemon must be live).
4. Print a banner — proposed change, monthly P&L the bot will be operating on, etc.
5. Prompt: `Type "GRADUATE TO LIVE TRADING" to proceed (any other input cancels):`
6. On exact match: copy plist into LaunchAgents, `launchctl load -w`.

`uninstall_live.sh`: `launchctl unload`, `rm` the plist. No prompt — easy reversal is desirable.

- [ ] Both scripts.
- [ ] `bash -n` (syntax check) verification.
- [ ] Commit.

---

## Task 6 — Reporter mode-awareness

**Files:** `src/trading_bot/reports.py`

Daily digest header shows the mode prominently. Email subject line includes the mode. Live mode: red banner. Paper mode: blue banner.

- [ ] Implement.
- [ ] Test rendered HTML contains either "LIVE" or "PAPER" banner.
- [ ] Commit.

---

## Task 7 — Update install.sh / uninstall.sh refs

**Files:** `ops/install.sh`, `ops/uninstall.sh`

Add a footer to `install.sh`:
```
echo
echo "Paper trading is now active."
echo "To graduate to live: read docs/superpowers/specs/...; then run:"
echo "  bot promote --target=live --i-know-this-is-real-money"
echo "  ops/install_live.sh"
echo "Both commands require typed confirmation."
```

`uninstall.sh` should NOT touch the live daemon — operator runs `ops/uninstall_live.sh` separately.

- [ ] Tweak install.sh footer.
- [ ] Verify uninstall.sh doesn't touch live label.
- [ ] Commit.

---

## Task 8 — Phase 6 deployment dry run

- [ ] Full pytest passes.
- [ ] `bot promote --help` shows both subcommands.
- [ ] `bot promote --target=live` (no flag, no creds) exits 1 cleanly with a clear message.
- [ ] `bot promote --target=live --i-know-this-is-real-money` (no creds) exits 1 cleanly.
- [ ] `ops/install_live.sh` (no live config file) exits 1 cleanly.
- [ ] No live plist auto-loaded by `ops/install.sh`.

---

## Acceptance criteria

1. `bot promote --target=paper` works and is reversible.
2. `bot promote --target=live` requires three things AND a typed confirmation. Any single missing element exits 1 with a clear reason and writes no files.
3. The live daemon plist exists but is dormant — only `ops/install_live.sh` loads it.
4. Live mode boots with `TRADING_BOT_MODE=live`, reads `live_active.json`, uses live Alpaca creds, applies the locked stricter risk caps.
5. Reporter makes the mode unmistakable in the daily digest subject + body.
6. `uv run pytest tests/` passes.

---

## Operator's manual graduation checklist (for reference, not implementation)

When you're confident paper has earned trust:
1. `bot promote --target=live --i-know-this-is-real-money` — type the confirmation. Creates `data/live_active.json` from current paper config with stricter caps.
2. `export ALPACA_LIVE_API_KEY=... ALPACA_LIVE_API_SECRET=...` in your shell profile.
3. `ops/install_live.sh` — type the second confirmation. Launches the live daemon.
4. Monitor `runs/_launchd/daemon_live.stderr.log` and the next daily digest for the LIVE banner.
5. To stop live trading immediately: `launchctl unload ~/Library/LaunchAgents/com.bharath.trading.daemon.live.plist` OR `touch data/pause_live.flag` (Risk Officer vetoes all orders when paused).
