# Mutation cycle setup runbook

The mutation cycle is the heaviest scheduled job — it can call Claude
CLI dozens of times per run. It's **disabled by default**.

## Pre-flight

- [ ] At least one strategy registered at `research_only` or higher.
- [ ] `research/search_space_v1.json` exists and is hashed in
      `policy/HASHES`.
- [ ] You understand the cost: a single cycle can spend hours of
      Claude CLI time and dollars of API budget.

## Enable

1. In `.env`:
   ```
   TRADING_BOT_ENABLE_LLM_HOTPATH=1
   TRADING_BOT_ENABLE_MUTATION_CYCLE=1
   ```
2. Configure the persona-runner command. By default the engine uses a
   subprocess call to `claude --json`; confirm it's on your PATH:
   ```
   which claude
   claude --version
   ```
3. Restart the daemon:
   ```
   launchctl unload ~/Library/LaunchAgents/com.tradingbot.daemon.plist
   launchctl load   ~/Library/LaunchAgents/com.tradingbot.daemon.plist
   ```
4. Confirm the next scheduled run via `bot status` (heartbeats table).

## What it does

1. Reads `research/search_space_v1.json`, enumerates up to 64 candidates
   per family.
2. For each candidate, runs the (operator-supplied) backtest.
3. Applies BH-FDR across the cycle to control false positives.
4. For survivors, runs the adversarial-pair intake (Bull + Bear).
5. Emits a Tier-1 `validation_artifact` for each survivor.

## Disable for vacation

Set `TRADING_BOT_ENABLE_MUTATION_CYCLE=0` in `.env`. The monthly
heartbeat will show `skipped: TRADING_BOT_ENABLE_MUTATION_CYCLE not set`.

---
**Operator sign-off:**
- Date enabled:
- Persona runner command:
- Monthly budget cap ($):
