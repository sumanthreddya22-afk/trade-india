# Trading Bot v4 — Phase 0 Design

**Source plan:** `Best Prompt Design for a Production-Ready Autonomous AI Trading Agent.pdf` (Plan v4, dated 2026-05-13).

**Phase 0 north star (from the plan):** *Stabilize current repo + freeze auto-tune + shrink crypto to ≤15% + classify all positions.* Exit criterion: all open positions classified; crypto cap respected; auto-tune disabled.

Phase 0 is a hard prerequisite for every later phase. It does not add new functionality — it stops the bot from mutating itself live, gates LLM out of the trading hot path, classifies what the bot owns, and lays the on-disk scaffolding (`docs/edge_thesis_v1.md`, `prompts/roles/`, `policy/HASHES` + nine empty `.lock` files) that later phases populate.

## Scope

In-scope deliverables (each backed by a Section 14 P0 acceptance test):

1. **Auto-tune freeze.** Any code path that mutates a live config/threshold/strategy parameter without provenance is either deleted or feature-flagged off. The repo has four candidates: `adaptive_thresholds.py`, `evolution.py`, `calibration.py`, `lesson_loop.py`. Each gets audited; live-mutation write paths are removed. Read-only telemetry (e.g., a calibrator that *reports* what it would change without applying it) is allowed.
2. **LLM-hot-path quarantine.** All LLM-driven entry/hold/scout debates (`entry_debate.py`, `hold_debate.py`, `email_entry_debate.py`, the `pipelines/{crypto,options,stocks}/*_debate.py` files, `personas/scout_judge.py`) gate behind `ENABLE_LLM_HOTPATH` (default `False`). When the flag is off, the orchestrator skips those calls without raising. LLM remains free in L3 research and L8 postmortem code paths (which today is `lab_data.py`, `decision_lessons.py`, `debate_outcome_analyzer.py`).
3. **Crypto exposure ≤15%.** Real Alpaca account, paper-trading. Audit current positions, propose a sell-list to bring crypto gross to ≤15% of equity, present the proposed orders to the user for explicit approval, then submit. This is the only step that touches the broker.
4. **Position classification.** Add a `classification` column to every position surface (DB and dashboard view): `bot|external|manual|unknown`. Backfill: positions opened via `client_order_id` matching the bot's pattern → `bot`; positions present on the broker with no matching `order_master` row → `external`; positions opened by a CLI manual command → `manual`; everything else → `unknown` (these halt new entries per Section 6 risk rules). For Phase 0 we only need the column and a backfill; the runtime risk check that halts on unknown lands in Phase 2.
5. **Edge thesis document.** `docs/edge_thesis_v1.md` carrying the seed thesis from Section 2 (cross-asset time-series momentum on liquid ETFs). One page, falsifiable, with universe / signal / sizing / cost lens / kill criteria. Registered as `thesis_id="edge_thesis_v1"`; later strategy registry rows reference this id.
6. **Eight persona files.** `prompts/roles/{quant_pm, quant_research_lead, risk_validator, trading_systems_engineer, execution_engineer, ai_mlops, sre_ops, compliance}.v1.md`. Each carries the persona content from Section 1A — role identity, decision rights, characteristic questions, forbidden actions, required output schema (the JSON shape on plan page 3).
7. **HASHES + nine empty locks.** `policy/HASHES` listing SHA-256 of nine `.lock` files: `validation_policy.lock`, `risk_policy.lock`, `pdt_policy.lock`, `lane_caps.lock`, `cost_model.lock`, `role_personas.lock`, `source_reliability.lock`, `data_freshness.lock`, `short_policy.lock`. Lock contents are minimal valid JSON (`{"lock_version": "2026-05-13.v4-phase0-skeleton"}`) so the file exists and hashes deterministically; later phases populate with real thresholds. Startup hash-check enforcement also lands in Phase 2.
8. **Project-level `CLAUDE.md`.** A short north-star file at the repo root: v4 direction, autonomy levels, which directories are kernel (no LLM) vs research (LLM allowed), where the plan lives. This survives across sessions.
9. **MEMORY entries.** Save four memory files under `/Users/bharathkandala/.claude/projects/-Users-bharathkandala-Trading/memory/`: (a) v4 direction + plan path, (b) the multi-session execution sequence, (c) wall-clock gates the user must run, (d) feature-flag conventions (`ENABLE_LLM_HOTPATH=false` default).

Out of scope (these belong to later phases, even if tempting now):

- Actual ledger schema / hash chain (Phase 1).
- Risk kernel enforcement of locks / kill switches at runtime (Phase 2).
- Cost model numerical content / Alpaca adapter hardening (Phase 3).
- Strategy registry table / validation_artifact emitter (Phase 4).
- Research factory walk-forward / PBO / DSR utilities (Phase 5).
- Mutation engine + BH-FDR (Phase 6).

## Architecture

No new runtime architecture in Phase 0 — this is a **subtractive + scaffolding** phase. Touch points:

- New top-level dirs created (mostly empty): `policy/`, `prompts/roles/`. (`research/`, `kernel/`, `risk/`, `execution/`, `obs/`, `registry/`, `ledger/` are deferred to their own phases.)
- New file: `src/trading_bot/feature_flags.py` — single source of truth for `ENABLE_LLM_HOTPATH` (env var `TRADING_BOT_ENABLE_LLM_HOTPATH`, default `false`). All debate modules import from here.
- New file: `src/trading_bot/position_classifier.py` — single classifier function `classify(position, order_master_rows) -> Literal["bot","external","manual","unknown"]`. Used by dashboard `data.py` and (in Phase 2) by the risk kernel.
- Modified: every `*_debate.py` module in the hot path gets one early-exit clause: `if not ENABLE_LLM_HOTPATH: return SkippedDebate(reason="hotpath_disabled")`. Existing return contracts preserved so callers don't need conditional handling.
- Modified: `adaptive_thresholds.py`, `evolution.py`, `calibration.py`, `lesson_loop.py` — remove the live-write path; keep telemetry. Each module gets a comment pointing to this design doc.

## Data flow

Phase 0 doesn't change data flow at runtime; it disables paths.

When `ENABLE_LLM_HOTPATH=false` (default):
1. Daemon scheduler ticks. Pipelines (crypto/options/stocks) compute candidates as today.
2. Where today they would call `entry_debate(...)` / `hold_debate(...)` / `scout_debate(...)`, they now hit the early-exit clause and receive a `SkippedDebate` sentinel.
3. Pipeline treats the skipped debate as "no signal" — no order is emitted.
4. Net effect: the bot stops opening new positions via LLM but continues to manage existing ones via deterministic rules (stops, scheduled exits). Exits are unaffected.

When `ENABLE_LLM_HOTPATH=true` (operator's explicit env override, never default):
- Existing behavior preserved. Useful for one-off debugging only.

## Error handling

- Auto-tune deletions: each removed write path is replaced with a no-op + logged warning at first call (`"auto-tune disabled in v4 Phase 0; see docs/edge_thesis_v1.md"`). This prevents callers from silently expecting a side effect.
- LLM-quarantine sentinel: `SkippedDebate` is a dataclass with `reason: str` and `verdict: Literal["skip"]`. Callers that switch on `verdict` already handle non-`open` verdicts.
- Crypto sell: each proposed order is generated as a dict, written to `runs/phase0_crypto_unwind_<ts>.json`, displayed to the user. Only after the user types `yes` does the script call `alpaca.submit_order`. Each fill writes to the existing log path. No new ledger schema in Phase 0 — the existing fill log is the source of truth for this one-off action.
- Lock files: each `.lock` is valid JSON; `policy/HASHES` is regenerated by a tiny script `tools/recompute_hashes.py`. If any lock is hand-edited without re-running the script, the Phase 2 startup check (when it ships) will halt.

## Testing

Phase 0 ships with these tests (Section 14 P0 mapping in parentheses):

1. `tests/test_phase0_llm_quarantine.py`: with `TRADING_BOT_ENABLE_LLM_HOTPATH` unset, importing each `*_debate.py` and calling its entry point returns a `SkippedDebate` sentinel without raising and without touching `anthropic_client` (asserts via mock patch). (P0: Community signals cannot reach the kernel — partial; full check in Phase 1.5.)
2. `tests/test_phase0_auto_tune_frozen.py`: each previously-mutating function in `adaptive_thresholds.py`, `evolution.py`, `calibration.py`, `lesson_loop.py` now returns without writing. Asserts the candidate write functions are absent or are no-ops. (Section 15: "Delete now (Phase 0)".)
3. `tests/test_phase0_position_classification.py`: `classify(...)` handles all four cases (`bot` via client_order_id pattern, `external` via no matching row, `manual` via origin flag, `unknown` as fallback).
4. `tests/test_phase0_persona_hash_check.py`: every persona file in `prompts/roles/` has SHA-256 listed in `policy/HASHES` and the file content matches. (P0: Role-persona hash check, L3/L8.)
5. `tests/test_phase0_hashes_file.py`: `policy/HASHES` lists nine lock files; each listed hash matches the file on disk; running `tools/recompute_hashes.py` is idempotent.

All tests must pass before the Phase 0 commit. Crypto unwind is a one-off operator action and is **not** automated in tests.

## Acceptance (the Section 14 P0 items satisfied by Phase 0)

- ✓ Edge thesis document in repo (`docs/edge_thesis_v1.md`).
- ✓ Three policy lock files + HASHES (Phase 0 ships nine empty locks + HASHES; full content lands in Phases 2–4).
- ✓ Role-persona hash check skeleton (locks + persona files present; runtime hash-check enforcement lands in Phase 2).
- Crypto exposure cap enforced — operator-confirmed sell.
- Auto-tune disabled (test 2 above).

Items deferred to their owning phase: append-only ledger (Phase 1), idempotent client_order_id at the broker layer (Phase 1), mandatory broker-to-ledger reconciliation (Phase 1), strategy_id in every risk decision (Phase 2 onward), runtime PDT block-entries-only (Phase 2), hash chain verified at startup (Phase 1), single-writer guard (Phase 1), off-host append-only mirror (Phase 1), community signals cannot reach the kernel — full enforcement (Phase 1.5), adversarial pair on every hypothesis (Phase 5).

## Risks & mitigations

- **Risk:** Auto-tune deletion silently breaks a caller that relied on a side effect. **Mitigation:** Grep all call sites for each removed function; replace with no-op that logs once per process.
- **Risk:** LLM quarantine breaks a UI flow that displays "last debate result". **Mitigation:** Dashboard reads from `decisions_store`/event_bus; the quarantine doesn't clear stored history. UI shows "LLM hot path disabled (v4 Phase 0)" badge.
- **Risk:** Selling crypto realizes paper losses and triggers spurious alerts. **Mitigation:** The user reviews and approves the sell list; the email alert dedup landed in commit `94a819e` so we won't firehose.
- **Risk:** Backfilling position classification mis-classifies a real bot position as `external` if `client_order_id` format changed historically. **Mitigation:** Backfill is best-effort; the column is informational in Phase 0 (no runtime gate yet). Phase 2 risk kernel can be told to whitelist specific symbol+broker_order_id pairs at boot.

## Non-goals worth naming

- Phase 0 does **not** prove any strategy works. It does not promote anything to live. It does not produce a backtest report. It deliberately makes the bot quieter and slower in exchange for provenance.
- Phase 0 does **not** delete the dashboard, the email infrastructure, the existing pipelines/, or any backtest utility. Those survive intact and become inputs to later phases.
