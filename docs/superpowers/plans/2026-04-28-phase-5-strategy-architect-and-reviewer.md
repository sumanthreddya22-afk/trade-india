# Phase 5 — Strategy Architect + Code Reviewer + Tone Analyst Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the three LLM-driven lab roles that let the bot evolve *new* strategy templates beyond MomentumStrategy. Strategy Architect (Role 18) calls Claude weekly to propose 1–3 templates as Python modules. Code Reviewer (Role 19) validates them via deterministic checks (AST allowlist, lookahead-bias scan, sandboxed runtime test) plus an LLM second opinion. Tone Analyst (Role 7) extends sentiment analysis with tone signals.

**Architecture:** Three new lab/daemon roles, an `anthropic_client.py` wrapper around the Anthropic SDK with retries + cost tracking, a `cost_tracker` table in `state.db`, and an extension of ResourceGuardian to halt Architect when monthly Anthropic spend hits the cap.

**Activation gate:** All three roles boot DISABLED unless `ANTHROPIC_API_KEY` is set in env. The lab logs `architect_disabled_no_creds` once and skips. Code Reviewer's deterministic checks run regardless (they don't need the API). This means installing Phase 5 with no creds is safe and harmless.

**Cost ceiling:** Hard $20/month per spec §7.7 Role 25. Configurable via `ANTHROPIC_MONTHLY_BUDGET_USD` env (default 20). At 80% spent: warn in daily digest. At 100%: ResourceGuardian sets a `cost_halt` flag that Architect checks on entry. Tone Analyst is far cheaper (Haiku-class single-token calls); separate small allocation.

**Reference spec:** [docs/superpowers/specs/2026-04-27-autonomous-evolving-system-design.md](../specs/2026-04-27-autonomous-evolving-system-design.md) §7.2 Role 7, §7.6 Roles 18 & 19, §7.7 Role 25, §13 (full LLM prompts).

---

## File structure for Phase 5

### New files
```
src/trading_bot/
  anthropic_client.py            # SDK wrapper + retry + cost log
  cost_tracker.py                # Read/write Anthropic usage rows
  ast_validator.py               # AST allowlist enforcement (deterministic)
  lookahead_validator.py         # Static analysis for future-bar access patterns
  sandbox_runner.py              # subprocess + resource.setrlimit + 30s walltime
  template_loader.py             # Discover + load _evolved/ templates dynamically
  roles/strategy_architect.py    # Role 18 (Saturday 06:00 ET, LLM)
  roles/code_reviewer.py         # Role 19 (immediate after Architect, LLM + AST)
  roles/tone_analyst.py          # Role 7 (lab; Haiku-class)

src/trading_bot/strategies/
  __init__.py                    # central registry — populated dynamically
  _pending/                      # Architect's raw output (gitignored runtime)
  _evolved/                      # Reviewer-accepted templates (committed by promotion)
  _archive/                      # Reviewer-rejected with rationale (gitignored)

migrations/versions/
  006_anthropic_cost_log_and_template_proposals.py

tests/
  test_anthropic_client.py
  test_cost_tracker.py
  test_ast_validator.py
  test_lookahead_validator.py
  test_sandbox_runner.py
  test_template_loader.py
  roles/test_strategy_architect.py
  roles/test_code_reviewer.py
  roles/test_tone_analyst.py
```

### Files modified
- `pyproject.toml` — add `anthropic>=0.40.0`
- `src/trading_bot/state_db.py` — add `AnthropicCostLog`, `TemplateProposal`, `CostHalt` ORM models
- `src/trading_bot/strategy.py` — extract `BaseStrategy` Protocol so generated templates conform
- `src/trading_bot/lab.py` — wire Saturday 06:00 + immediate-after Architect → Reviewer pipeline
- `src/trading_bot/roles/sentiment_analyst.py` — invoke Tone Analyst as a sub-step
- `src/trading_bot/roles/resource_guardian.py` — Anthropic budget enforcement
- `src/trading_bot/param_space.py` — accept new template entries from Reviewer-accepted proposals
- `.gitignore` — `src/trading_bot/strategies/_pending/`, `src/trading_bot/strategies/_archive/`

---

## Task 1 — Anthropic SDK + cost tracker scaffolding

**Files:** `pyproject.toml`, `state_db.py`, `cost_tracker.py`, migration 006

ORM:
```python
class AnthropicCostLog(Base):
    __tablename__ = "anthropic_cost_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    called_at = Column(DateTime(timezone=True), nullable=False, index=True)
    role_name = Column(String(64), nullable=False)
    model = Column(String(64), nullable=False)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cost_usd = Column(Float, nullable=False)
    request_id = Column(String(128), nullable=True)


class CostHalt(Base):
    __tablename__ = "cost_halts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    halted_until = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text, nullable=False)
    set_at = Column(DateTime(timezone=True), nullable=False)
```

`cost_tracker.py`:
- `record_call(session, role_name, model, in_tokens, out_tokens, request_id) -> float` — computes cost via per-model rate table, appends, returns the per-call USD.
- `monthly_spend(session, *, year, month) -> float`.
- `is_halted(session) -> bool`.

Per-model rates (locked, lab-only):
- `claude-opus-4-7`: $15/$75 per Mtok in/out — Architect default (best reasoning for code).
- `claude-haiku-4-5`: $0.80/$4 per Mtok in/out — Tone Analyst default.

Override via `ANTHROPIC_ARCHITECT_MODEL` / `ANTHROPIC_TONE_MODEL` env vars.

- [ ] Add anthropic dep + uv lock.
- [ ] ORM + migration.
- [ ] cost_tracker tests + impl.
- [ ] Commit.

---

## Task 2 — Anthropic client wrapper

**Files:** `src/trading_bot/anthropic_client.py`, `tests/test_anthropic_client.py`

`AnthropicClient(*, role_name, model)`:
- `complete(*, system, messages, max_tokens) -> AnthropicResponse` (with `text`, `input_tokens`, `output_tokens`, `request_id`).
- Auto-records cost via `cost_tracker.record_call`.
- Retries on `RateLimitError` and `APIStatusError 5xx` with exponential backoff (max 3).
- Raises `CredentialError` (existing exception) on 401.
- Raises `BudgetExceededError` if `cost_tracker.is_halted()` returns True.

- [ ] Tests: mock `anthropic.Anthropic.messages.create`, verify retry logic, cost recording, halt enforcement.
- [ ] Implement.
- [ ] Commit.

---

## Task 3 — BaseStrategy Protocol extraction

**Files:** `src/trading_bot/strategy.py`

Extract a runtime-checkable Protocol:
```python
@runtime_checkable
class BaseStrategy(Protocol):
    def evaluate(self, symbol: str, ind: Indicators, equity: Decimal) -> Signal: ...

    @classmethod
    def from_params(cls, params: dict) -> "BaseStrategy": ...
```

Keep MomentumStrategy as the canonical example. New templates from Architect must satisfy this Protocol — Code Reviewer enforces.

- [ ] Define Protocol.
- [ ] Test isinstance(MomentumStrategy(), BaseStrategy) is True.
- [ ] Commit.

---

## Task 4 — AST allowlist validator

**Files:** `src/trading_bot/ast_validator.py`, `tests/test_ast_validator.py`

`validate_ast(source: str) -> AstReport` returns:
- `allowed_imports`: set of imports the module uses.
- `forbidden_imports`: list of imports outside the allowlist (`pandas`, `numpy`, `ta`, `math`, `datetime`, `dataclasses`, `typing`, `decimal`, `enum`).
- `forbidden_calls`: list of any `eval`/`exec`/`compile`/`__import__` usages.
- `passes`: bool — True iff no forbiddens.

Pure-Python AST walk, no LLM.

- [ ] Tests: 5 cases (clean module, forbidden import, eval call, dynamic import, allowed-only).
- [ ] Implement.
- [ ] Commit.

---

## Task 5 — Lookahead-bias static analyzer

**Files:** `src/trading_bot/lookahead_validator.py`, `tests/test_lookahead_validator.py`

Heuristic checks against the strategy module:
- Any indexing pattern like `bars[i+1]`, `bars.iloc[future_idx]`, `df.shift(-N)` flagged.
- Any reference to a date later than the current bar's date.
- Any function from `ta` known to use future bars (none in standard `ta` lib but maintain a denylist for safety).

`validate_lookahead(source: str) -> LookaheadReport` with `findings: list[str]` and `passes: bool`. False positives are acceptable; false negatives are not — Reviewer pairs this with an LLM second-opinion call.

- [ ] Tests: 4 cases (clean, future index, negative shift, fancy slice).
- [ ] Implement.
- [ ] Commit.

---

## Task 6 — Sandboxed runtime check

**Files:** `src/trading_bot/sandbox_runner.py`, `tests/test_sandbox_runner.py`

`run_in_sandbox(source: str, test_source: str, *, walltime_s=30, mem_mb=512) -> SandboxResult`:
- Writes source + test to a tempdir.
- Forks a subprocess with `resource.setrlimit(RLIMIT_CPU, (30, 30))` and `RLIMIT_AS = 512MB`.
- Runs `python -m pytest tempdir/test_module.py -x --tb=short`.
- Returns stdout/stderr, exit code, walltime.

- [ ] Tests with a trivial pytest module that passes; one that fails; one that infinite-loops (verify CPU rlimit kills it).
- [ ] Implement.
- [ ] Commit.

---

## Task 7 — TemplateProposal ORM + storage paths

**Files:** `state_db.py`, `template_loader.py`

```python
class TemplateProposal(Base):
    __tablename__ = "template_proposals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposed_at = Column(DateTime(timezone=True), nullable=False)
    name = Column(String(64), nullable=False)
    rationale = Column(Text, nullable=False)
    expected_regime = Column(String(32), nullable=False)
    code = Column(Text, nullable=False)
    tests = Column(Text, nullable=False)
    params_to_search_json = Column(Text, nullable=False)
    review_status = Column(String(32), nullable=False)  # pending | accepted | rejected
    review_findings_json = Column(Text, nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
```

`template_loader.py`:
- `discover_evolved_templates() -> dict[str, type]` — walks `_evolved/`, imports, registers in PARAM_SPACE.
- Called once at lab boot + once per ParamOptimizer run (cheap import).

- [ ] ORM + migration.
- [ ] template_loader tests with synthetic _evolved/ modules.
- [ ] .gitignore entries for _pending/ and _archive/.
- [ ] Commit.

---

## Task 8 — StrategyArchitectRole

**Files:** `src/trading_bot/roles/strategy_architect.py`, `tests/roles/test_strategy_architect.py`

Charter:
```python
class StrategyArchitectRole(BaseRole):
    name = "strategy_architect"
    tier = 5
    process = "lab"
    job_description = "Weekly Anthropic call: propose 1-3 strategy templates conforming to BaseStrategy. Output stored in _pending/."
    sla_seconds = 5 * 60
    upstream_roles = []
    downstream_roles = ["code_reviewer"]
```

`_do_work(ctx)` flow:
1. Check `ANTHROPIC_API_KEY` — if missing, return `{"skipped": True, "reason": "no_anthropic_creds"}`.
2. Check cost halt — if halted, return `{"skipped": True, "reason": "anthropic_budget_exceeded"}`.
3. Build context payload (per spec §13.1):
   - Top 10 leaderboard rows.
   - Last 30 days' worst 3 trades.
   - 90-day regime distribution from `regime_history`.
   - Role charters dump.
4. Call AnthropicClient with system prompt from spec §13.1.
5. Parse JSON response — expect array of `{name, rationale, expected_regime, code, tests, params_to_search}`.
6. For each: write a TemplateProposal row + a file under `_pending/<name>/<name>.py` and `_pending/<name>/test_<name>.py`.
7. Return `{"n_proposals": int, "names": [...]}`.

- [ ] Tests with mocked AnthropicClient (canned JSON response).
- [ ] Implement.
- [ ] Commit.

---

## Task 9 — CodeReviewerRole

**Files:** `src/trading_bot/roles/code_reviewer.py`, `tests/roles/test_code_reviewer.py`

Charter:
```python
class CodeReviewerRole(BaseRole):
    name = "code_reviewer"
    tier = 5
    process = "lab"
    job_description = "Reviews each pending proposal: AST allowlist, lookahead scan, sandbox runtime, optional LLM second opinion."
    sla_seconds = 10 * 60   # 30s sandbox × up to 3 proposals + LLM time
    upstream_roles = ["strategy_architect"]
    downstream_roles = []
```

`_do_work(ctx)` flow:
For each TemplateProposal with `review_status = "pending"`:
1. AST validation — fail-fast if forbidden imports/calls.
2. Lookahead validation — fail-fast on positive findings.
3. Sandbox runtime — must pass tests AND stay within 30s/512MB.
4. (If creds available) LLM second opinion call — system prompt from spec §13.2 — return one of `accept`/`reject` + 1-line reason.
5. If all pass: copy proposal into `_evolved/<name>/`, update review_status="accepted", call template_loader.discover_evolved_templates() to register.
6. If any fail: copy to `_archive/<name>/` with findings.json, update review_status="rejected".

- [ ] Tests for each gate, plus a full happy-path acceptance.
- [ ] Implement.
- [ ] Commit.

---

## Task 10 — ToneAnalystRole

**Files:** `src/trading_bot/roles/tone_analyst.py`, `tests/roles/test_tone_analyst.py`

Charter:
```python
class ToneAnalystRole(BaseRole):
    name = "tone_analyst"
    tier = 1
    process = "lab"
    job_description = "Per-symbol tone classification (urgency/hedging/insider-confidence) from news + filings text. Haiku model."
    sla_seconds = 60
    upstream_roles = ["sentiment_analyst"]
    downstream_roles = ["stock_scanner"]
```

For each symbol with fresh news/filings text in `news_sentiment.db`, call Anthropic Haiku with a tight prompt → `{"tone_score": float ∈ [-1,1], "urgency": float, "hedging": float}`. Cache in `state.db.role_kpis` keyed by `(symbol, recorded_at)`.

- [ ] Tests with mocked client.
- [ ] Implement.
- [ ] Commit.

---

## Task 11 — ResourceGuardian Anthropic budget enforcement

**Files:** `src/trading_bot/roles/resource_guardian.py`, `tests/roles/test_resource_guardian.py`

Add to `_do_work`:
- Compute `monthly_spend = cost_tracker.monthly_spend(session, year=now.year, month=now.month)`.
- If spend > 0.8 × cap → write a `RoleKpi` warning row (surfaces in next digest).
- If spend ≥ cap → write a `CostHalt` row with `halted_until = next month's first day`, reason = "anthropic monthly cap exceeded ($X / $Y)".

- [ ] Tests for warn + halt thresholds.
- [ ] Implement.
- [ ] Commit.

---

## Task 12 — Lab schedule wires Architect → Reviewer pipeline

**Files:** `src/trading_bot/lab.py`, `tests/test_lab.py`

Add Saturday 06:00 ET job:
```python
def saturday_evolve():
    architect_result = architect.safe_run(ctx={})
    if architect_result.outputs.get("n_proposals", 0) > 0:
        reviewer.safe_run(ctx={})
```

Note: NOT a chain via APScheduler — both run inline so a failed Architect doesn't leave Reviewer with stale state.

- [ ] Wire job.
- [ ] Update test_lab.py job-count assertion.
- [ ] Commit.

---

## Task 13 — Phase 5 deployment dry run

- [ ] Full pytest passes.
- [ ] Lab boots; with no ANTHROPIC_API_KEY, both Architect and Tone Analyst log skip events.
- [ ] AST validator + sandbox + lookahead validator can be exercised manually with a trivial test template.
- [ ] No production install change beyond optional `ANTHROPIC_API_KEY` + `ANTHROPIC_MONTHLY_BUDGET_USD` env vars in lab plist's EnvironmentVariables.

---

## Acceptance criteria

1. `state.db` has `anthropic_cost_log`, `template_proposals`, `cost_halts` tables.
2. With `ANTHROPIC_API_KEY` set: Architect runs Saturdays at 06:00 ET, generates 1–3 proposals, Reviewer auto-validates, accepted templates land in `_evolved/`.
3. Without the key: both LLM roles boot disabled, log a skip event, no errors.
4. Cost cap enforces: spend warnings surface in daily digest at 80%, hard halt at 100%.
5. AST/lookahead/sandbox checks reject any proposal with forbidden imports, future-bar access, or runtime > 30s.
6. `uv run pytest tests/` passes.
