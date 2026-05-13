# Trading Bot v4 — Phase 0 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-0-design.md`
**Phase duration in plan:** 1 calendar week (this session covers code; crypto unwind requires operator confirmation).

## Step 0 — Audits (read-only, foundation for all later steps)

1. **Auto-tune audit.** Grep `adaptive_thresholds`, `evolution`, `calibration`, `lesson_loop` for every public function. Identify which ones *write* to disk / config / DB vs which *report*. Capture the call-site map.
2. **LLM hot-path audit.** Grep `anthropic_client`, `claude_cli`, `entry_debate`, `hold_debate`, `scout_debate` import sites. Build the list of files that need the feature-flag early-exit.
3. **Crypto exposure audit.** Call Alpaca paper account: list_positions, compute crypto gross / equity. Compare to 15% target.

Exit: a clear map of what to touch.

## Step 1 — Feature flag module

File: `src/trading_bot/feature_flags.py`. One module-level constant `ENABLE_LLM_HOTPATH` read from env `TRADING_BOT_ENABLE_LLM_HOTPATH`, default `False`. One helper `is_llm_hotpath_enabled() -> bool`. No other state.

## Step 2 — LLM hot-path quarantine

For each file in the audit's hot-path list:

- Import `is_llm_hotpath_enabled` from `feature_flags`.
- At the top of the entry function (`run_debate`, `entry_debate`, equivalent), add: `if not is_llm_hotpath_enabled(): return SkippedDebate(...)` (or the module's existing no-op return shape — match whatever the caller expects).
- Add a one-line `logger.info("LLM hot-path disabled; skipping debate")` so the operator sees it.

`SkippedDebate` is a tiny dataclass added to `src/trading_bot/feature_flags.py`. Modules that already return rich result objects can return their own no-op (e.g., a `DebateResult(verdict="skip", reason="hotpath_disabled")`) — the goal is "no LLM call made and no order emitted".

## Step 3 — Auto-tune freeze

For each candidate file:

- Identify the write functions (writes to `strategy/config.yaml`, writes back to DB, mutates module-level config, etc.).
- Replace the write body with: log once per process (`"auto-tune disabled in v4 Phase 0; see docs/edge_thesis_v1.md"`) and return the input unchanged (or `None` if no return contract).
- Telemetry / observation functions are untouched.

Each modified file gets one short comment near the disabled function citing this plan: `# v4 Phase 0: live mutation removed — see docs/superpowers/plans/2026-05-13-trading-bot-v4-phase-0.md`.

## Step 4 — Position classifier

File: `src/trading_bot/position_classifier.py`.

```python
ClassificationT = Literal["bot", "external", "manual", "unknown"]

def classify(
    position: BrokerPosition,
    order_master_lookup: Callable[[str], OrderMasterRow | None],
) -> ClassificationT: ...
```

Rules:

- If `position.symbol` has an `order_master` row whose `client_order_id` matches the bot's pattern `YYYYMMDD_<strategy>_<symbol>_<seq>` and `origin == "strategy"` → `bot`.
- Else if `origin == "manual"` → `manual`.
- Else if no `order_master` row exists for the symbol → `external`.
- Else → `unknown`.

Wire into `src/trading_bot/dashboard/data.py` so the existing positions view surfaces the classification.

(`order_master` table doesn't exist yet — it ships in Phase 1. For Phase 0 the lookup is a stub that returns `None` for everything, which means every existing position classifies as `external` until Phase 1 lands. That's acceptable per the spec; we just need the column and the function shape.)

## Step 5 — Edge thesis document

File: `docs/edge_thesis_v1.md`. Content lifted from plan Section 2 (universe, signal, sizing, cost lens, kill criteria, expected behavior). Adds a `thesis_id: edge_thesis_v1` front-matter so later strategy registry rows can reference it.

## Step 6 — Persona files

Directory: `prompts/roles/`. Eight files: `quant_pm.v1.md`, `quant_research_lead.v1.md`, `risk_validator.v1.md`, `trading_systems_engineer.v1.md`, `execution_engineer.v1.md`, `ai_mlops.v1.md`, `sre_ops.v1.md`, `compliance.v1.md`. Each file:

- Role identity (one paragraph from Section 1A "Stance + responsibilities").
- Decision rights (what it can support / block / abstain on).
- Characteristic questions (3–5).
- Forbidden actions (no kernel access, no .lock edits, no broker calls).
- Required output schema — the JSON shape on plan page 3.

## Step 7 — Policy locks + HASHES

Directory: `policy/`. Nine `.lock` files, each containing:

```json
{ "lock_version": "2026-05-13.v4-phase0-skeleton", "_note": "content lands in Phase X" }
```

File `policy/HASHES`: one line per lock, format `<sha256>  <filename>`. Plus the eight persona files (per `role_personas.lock` requirement).

Tool: `tools/recompute_hashes.py` — walks `policy/*.lock` and `prompts/roles/*.md`, emits the HASHES file. Idempotent. Future operator workflow: edit a lock → re-run the tool → commit both files together.

## Step 8 — Project-level CLAUDE.md

File: `CLAUDE.md` at repo root. Content:

- North star (single sentence from plan: "build an autonomous systematic trading laboratory, not a chatbot that trades").
- Autonomy level (currently L2 per plan Section 16 — research only, no autonomous live trading).
- Where the plan lives.
- Which directories are kernel (no LLM, no auto-mutation) vs research (LLM allowed).
- The `ENABLE_LLM_HOTPATH` convention.
- The v4 phase the repo is currently on.

This file is read by every future session and tells the agent the constraints.

## Step 9 — Memory entries

Files written under `/Users/bharathkandala/.claude/projects/-Users-bharathkandala-Trading/memory/`:

- `project_trading_bot_v4_direction.md` — Plan v4 direction, plan PDF path, current phase.
- `project_trading_bot_v4_sequence.md` — the 10-session execution table.
- `project_trading_bot_v4_wall_clock_gates.md` — MVP-OP 60-day, ALPHA 365-day, etc.
- `feedback_trading_bot_v4_conventions.md` — feature-flag convention, kernel-vs-research split, no LLM in hot path.

Add corresponding lines to `MEMORY.md` index.

## Step 10 — Tests

Five test files per the spec. Run them all; all must pass.

## Step 11 — Crypto unwind (operator-gated)

Script: `tools/phase0_crypto_unwind.py`. Behavior:

1. Call Alpaca: list_positions, get_account.
2. Compute crypto gross. Compute target = 15% of equity. Compute reduction needed.
3. For each crypto position, propose a market sell with quantity proportional to its share of the over-cap excess.
4. Write proposals to `runs/phase0_crypto_unwind_<utc_ts>.json`.
5. Print to stdout.
6. If invoked with `--submit` AND the operator types `yes`, submit each order via Alpaca and print fill confirmations. Otherwise dry-run only.

In this session I will run the dry-run, show the proposal, and **wait for the user to type `yes`** before submitting. The submit step is a separate user-confirmed action.

## Step 12 — Commit cadence

Five logical commits:

1. `docs(v4): Phase 0 design + plan + edge thesis + persona files`
2. `feat(v4): feature flag module + LLM hot-path quarantine`
3. `feat(v4): freeze auto-tune live writes`
4. `feat(v4): position classifier + dashboard wiring`
5. `feat(v4): policy/HASHES scaffold + tools/recompute_hashes.py`
6. `test(v4): Phase 0 P0 acceptance tests`

Each commit runs the relevant tests green before the commit lands.

## Step 13 — Operator-gated commit

7. `chore(v4): Phase 0 crypto unwind — <N> orders, <reduction>%` — only after the user confirms the live sell.

## Done definition

- All 5 test files pass.
- `policy/HASHES` matches files on disk.
- `CLAUDE.md` and MEMORY entries written.
- Dry-run crypto unwind generated and shown to operator.
- Live unwind done only with operator's explicit `yes`.
