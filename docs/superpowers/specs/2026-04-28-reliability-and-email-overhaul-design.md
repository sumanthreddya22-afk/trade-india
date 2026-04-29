# Reliability Fixes + Email System Overhaul — Design

**Status:** ready for review
**Date:** 2026-04-28
**Scope:** ~15 tasks split across two modules. One spec, one plan, executed in two phases.

## Background

Today's day-end review (2026-04-28) surfaced seven distinct operational issues:

1. The stock scanner job was registered but never fired.
2. The reconciler is missing — `closed_trades.db` has zero rows ever; yesterday's AMD/CLS/AMDL closes weren't journaled.
3. The verify-stops cron runs `0 9-16 * * 1-5` (8x weekday only), but the architecture doc and the auto-protect feature both assumed `20,50 * * * *` (48x, 24/7).
4. `trade_journal` has identical-row duplicates (yesterday at 13:07 + 20:43 ET).
5. Two daemon stalls fired CRITICAL emails despite auto-recovering in seconds — alert noise without action items.
6. The lab promoted a new strategy at 06:01 ET with no first-24h validation gate.
7. Daemon `EmailSender.send` calls don't emit structured logs; only the supervisor does.

In parallel, the email surface is fragmented (11+ subject variants), visually inherits the dashboard's palette but not its information architecture, and is light on detail.

This spec fixes all seven issues and rebuilds the email system around four consolidated email types with dashboard-style visual elements.

## Goals

- Every silent failure becomes loud (schedule self-test).
- Reconciliation closes the P&L reporting hole.
- Verify-stops actually runs 24/7, matching the auto-protect contract.
- One denser, dashboard-styled daily digest replaces 11 inconsistent subjects.
- Real-time alerts batched at 20-min cadence to prevent inbox flooding.
- Mobile email clients (Gmail, iOS Mail) render correctly; older Outlook degrades gracefully.

## Non-Goals

- No changes to strategy logic, lab evolution, or sentiment scoring.
- No new data sources; this spec uses what the bot already collects.
- No backwards-compatibility shims for the deleted email builders. Old subjects are gone after this lands.
- No SMS/Slack/Discord — email only, same recipient (`bharath8887@gmail.com`).

---

## Module 1: Reliability & Observability

### A1. Schedule self-test

**New job:** `schedule_audit` at 21:55 ET daily — 5 min before the digest at 22:00 ET so the digest can include its findings.

**Behavior:**

For each registered scheduler job (excluding `heartbeat`, `log_rotation`):

1. Compute the expected fire count for the day from the cron expression (e.g., `20,50 * * * *` → 48; `30 9-15 * * 1-5` on a weekday → 7).
2. Count actual fires by grep'ing today's daemon JSON logs for the matching `<job>_start` events.
3. If actual / expected < 0.5, write a `schedule_audit_warning` event with the job name, expected, actual.
4. Persist the audit summary to a new `schedule_audits` SQLite table (`date, job_id, expected, actual, ratio`).

**Surface:** the daily digest's "System Health" section reads the latest row from this table. If any warnings, the digest's overall status pulse downgrades from green → amber.

### A2. Scheduler resilience

In `scheduler_jobs.py`, add `misfire_grace_time=300, coalesce=True` to every `add_job` call. Daemon-startup change: on `register_jobs`, compute each cron job's most recent past fire time. If that fire is within the last 60 minutes AND no `<job>_start` event exists in today's logs after that time, run the job once immediately (catch-up).

Today's stock-scanner gap (daemon restarted at 09:04, 09:17, 10:02, 10:18 — all within 1h of 09:30 fire) would have triggered a startup catch-up.

### A3. Reconciler

**New job:** `reconciler` at 16:05 ET (5 min post-close) and 21:55 ET (just before audit + digest).

**New module:** `src/trading_bot/reconciler.py` with a single function `reconcile(client, trade_journal_path, closed_trades_path) -> ReconcileReport`.

**Behavior:**

1. Load all open `trade_journal` entries (entry rows where the symbol+entry_order_id has no matching `closed_trades` row).
2. Fetch current `client.get_positions()` symbols.
3. For each open journal entry whose symbol is NOT in current positions:
   a. Query Alpaca order history for the original `entry_order_id` and any subsequent fills on the same symbol.
   b. Identify the closing fill (opposite side, post-entry timestamp).
   c. Compute realized P&L = `qty × (exit_price − entry_price)` for longs (mirror for shorts).
   d. Compute hold_hours.
   e. Insert into `closed_trades` (UNIQUE on `entry_order_id` enforced).
4. Return a `ReconcileReport` with counts: `(reconciled_count, unmatched_count, errors_count)`.
5. The first run will backfill yesterday's AMD/CLS/AMDL closes if Alpaca order history retains them; otherwise mark them as `exit_reason="reconciled_from_audit"` and leave exit_price blank.

The CLI command `bot reconcile` runs the same function on demand.

### A4. Verify-stops cron — 24/7

In `scheduler_jobs.py`:

```python
# OLD
trigger=CronTrigger(hour="9-16", minute="0" if os_min >= 60 else f"*/{os_min}",
                    day_of_week="mon-fri", timezone=et)
# NEW
trigger=CronTrigger(minute="20,50", timezone=et)  # 48x/day, every day
```

Update `dashboard/templates/architecture.html` — already says "24/7" in prose; just confirm the cron table cell matches.

The auto-protect feature merged earlier today (`feature/open-position-auto-protect`) was specified for 24/7 cadence. This change makes the cron match the spec.

### A5. Journal de-dupe

**Migration:** new alembic revision adding a unique index on `trade_journal.trades.entry_order_id`.

**One-time cleanup** in the migration's upgrade:

```sql
DELETE FROM trades WHERE id NOT IN (
  SELECT MIN(id) FROM trades GROUP BY entry_order_id
);
```

**Code change:** in `journal_alpha.py`, change inserts from `INSERT INTO ... VALUES` to `INSERT OR IGNORE INTO ... VALUES`. Add a test that records the same trade twice and asserts the table count stays at 1.

### A6. Stall-alert dedupe / downgrade

In `supervisor.py`:

- When `_kickstart()` succeeds and the daemon heartbeat resumes within 60s, do NOT call `_send_alert(kind="daemon_stall", ...)`.
- Instead emit a `daemon_blip_recovered` log event with `stall_duration_seconds` and `recovery_method`.
- If `_kickstart()` fails OR the stall persists past 5 min, the existing CRITICAL email path runs as before.

Today's two stalls (00:18 with 11s recovery, 11:35 with 1s recovery) become silent log events; the digest's System Health section shows "2 daemon blips, all auto-recovered."

### A7. Lab-promotion validation gate

**New SQLite table:** `lab_promotions` in `state.db`:

```sql
CREATE TABLE lab_promotions (
    id INTEGER PRIMARY KEY,
    promoted_at DATETIME NOT NULL,
    version VARCHAR(64) NOT NULL UNIQUE,
    template VARCHAR(32) NOT NULL,
    git_sha VARCHAR(64) NOT NULL,
    fitness_at_promotion FLOAT NOT NULL,
    params_json TEXT NOT NULL,
    risk_caps_json TEXT NOT NULL,
    -- Validation rolling counts (updated by digest pre-pass)
    scans_since_promote INTEGER NOT NULL DEFAULT 0,
    entries_since_promote INTEGER NOT NULL DEFAULT 0,
    near_misses_since_promote INTEGER NOT NULL DEFAULT 0,
    validated_at DATETIME  -- NULL until 24h passed
);
```

When `lab_promoter` writes `paper_active.json`, it also inserts a row here. The digest's "New Strategy" section reads any row where `validated_at IS NULL` AND `promoted_at + 24h > now()`. If 24h passes with `entries_since_promote == 0` AND `near_misses_since_promote == 0`, the digest flags "STRATEGY MAY BE TOO RESTRICTIVE — zero scans engaged it."

Pre-pass: before the digest runs, refresh the rolling counts by counting trades and stock-scanner decisions since the promotion timestamp.

### A8. Universal email-send logging

**New module:** `src/trading_bot/email_log.py`:

```python
def send_logged(sender: EmailSender, *, subject: str, html_body: str,
                kind: str, log: StructuredLogger | None = None) -> None:
    """Wrap EmailSender.send() with a structured log event.

    `kind` is the email category: 'digest' | 'midday' | 'alert' | 'promotion'.
    Emits an `email_sent` event with subject, recipient, kind, ts, outcome.
    Persists to `state.db` table `emails_sent` for digest reporting.
    """
```

Refactor every `EmailSender(...).send(...)` call site (in `cli.py`, `supervisor.py`) to go through `send_logged`. Drop the supervisor's bespoke `_send_alert` log emission — it's redundant once `send_logged` exists.

New table:

```sql
CREATE TABLE emails_sent (
    id INTEGER PRIMARY KEY,
    sent_at DATETIME NOT NULL,
    kind VARCHAR(32) NOT NULL,
    subject TEXT NOT NULL,
    recipient TEXT NOT NULL,
    outcome VARCHAR(16) NOT NULL  -- 'ok' | 'failed'
);
```

The digest's "Emails Today" section reads the count by kind from this table.

---

## Module 2: Email System Overhaul

### B1. Consolidated email types — 4

| Type | Trigger | Cadence |
|---|---|---|
| **Daily Digest** | `daily_digest` job | 22:00 ET daily |
| **Midday Snapshot** | `midday_snapshot` job (renamed from `midday_report`) | 12:00 ET weekdays |
| **Action Alert** | Real-time, batched at 20-min cadence | Variable |
| **Strategy Promotion** | When `lab_promoter` writes `paper_active.json` | Real-time, single email per promote |

**Deleted email builders** (in `reports.py`, `cli.py`):
- `build_daily_report_html` (replaced by digest)
- `build_rich_report_html` (replaced by digest)
- `build_alert_email_html` (replaced by Action Alert)
- All `cli.py` ad-hoc subject lines: "Trading Bot — Status", "— Daily Report", "— EOD Report", "— Rich Report", "— Intel Scan", "— Portfolio Alert"

**Surviving / repurposed:**
- `build_open_positions_email_html` (auto-protect summary) → migrated into Action Alert framework
- `build_vip_alert_email_html` → migrated into Action Alert framework

### B2. Visual shell — `email_shell.py` (new module)

A single shared shell module so every email type uses the same look:

```python
def render_shell(*, title: str, status: Literal["ok", "warn", "bad"],
                 timestamp_et: str, body_sections: list[str]) -> str: ...

def gradient_header(title: str, status: Literal["ok", "warn", "bad"]) -> str: ...
def kpi_card(label: str, value: str, *, color: str = ...,
             delta: str | None = None, sparkline_svg: str | None = None) -> str: ...
def kpi_grid(cards: list[str]) -> str: ...
def section(title: str, glyph: str, body: str, *, severity: str = "neutral") -> str: ...
def progress_bar(value_pct: float, *, color: str, label: str) -> str: ...
def sparkline_svg(values: list[float], *, width: int = 120, height: int = 32,
                  color: str = "#22d3ee") -> str: ...
def data_table(headers: list[str], rows: list[list[str]], *,
               zebra: bool = True, right_align_cols: list[int] | None = None) -> str: ...
def severity_pill(text: str, kind: Literal["good", "warn", "bad", "info"]) -> str: ...
def pulse_dot(status: Literal["ok", "warn", "bad"]) -> str: ...
def footer(version: str, git_sha: str, dashboard_url: str | None = None) -> str: ...
```

Visual elements:

- **Gradient brand bar at top** — 100% width, 6px tall, `linear-gradient(90deg, #22d3ee 0%, #a78bfa 100%)`. Matches the dashboard's `.gradient-text` style.
- **Header** — title in 22px white, status pulse-dot to the right (8px, glow shadow), timestamp in muted secondary color.
- **Pulse-dot** — `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981">`. Color from status (green/amber/red).
- **KPI cards** — 4-up grid (2x2 on mobile via media query), 32px monospace numbers, 11px cyan labels, optional sparkline below the value (60×20 SVG).
- **Section headers** — 11px cyan label + emoji glyph + 1.65px letter-spacing uppercase, matches dashboard `.label`.
- **Progress bars** — `<div>` with width % for "loss vs cap" gauges, color-coded (green ≤ 50%, amber 50-80%, red > 80%).
- **Sparklines** — inline SVG `<polyline>` with `stroke="#22d3ee"`, no fill, no axis. Renders in Gmail/Apple Mail/iOS Mail; degrades to a hidden element in Outlook 2007–2019 (acceptable per user).
- **Status pills** — existing `_pill` helper, sized down to 10px font.
- **Data tables** — zebra rows (alternating `rgba(15,23,42,0.5)` and `rgba(15,23,42,0.85)`), right-aligned monospace numbers, color-coded P&L cells (green/red).
- **Footer** — `version • git_sha • view dashboard →` in 11px muted, gradient-text underline on the link.

All inline styles. No `<style>` block. No flexbox. No CSS grid. Tables for layout. 640px max-width.

### B3. Daily Digest — full content

**Builder:** `build_daily_digest_email(ctx: DigestContext) -> Email` in `email_digest.py` (the file already exists; rewrite its body).

**Subject:** `Daily Digest · Apr 28 · +0.21% · $14,953` (use middle dot `·` instead of pipe; matches dashboard typography).

**Sections (order):**

1. **Header bar** — date in long form ("Tuesday, April 28"), regime pill, daemon status pulse, account version.
2. **KPI grid (4 cards)** — Equity (with sparkline) · Today's P&L · Realized · Unrealized.
3. **Equity 30d sparkline** — full-width inline SVG line chart, 600×80, with min/max value labels and today's marker. Data from `state.db` equity_high_water_mark + portfolio_snapshot history.
4. **Risk gauges** — 4 horizontal progress bars: daily loss / cap, weekly loss / cap, drawdown / cap, consecutive losing days / cap.
5. **Regime & indicators** — current regime + arrow if changed today, VIX value, vol_threshold, regime_score (a single composite number).
6. **Positions** — full table: Symbol · Qty · Side · Entry · Current · Today % · Total % · Stop · Distance · Sentiment · Sector. Color-coded by P&L direction.
7. **Today's trades** — opens + closes with per-trade realized/unrealized P&L. Empty state: "No trades today."
8. **Closed trades (last 7d)** — symbol, hold time, realized $, % return, exit reason. From `closed_trades` (now populated by A3).
9. **Lab activity** — only if active promotion in last 24h: params diff table (old → new), fitness, validation status (`X scans engaged · Y entries · Z near-misses since promote`).
10. **Watchlist movers** — top 5 winners + 5 losers in active universe, with sentiment scores. Pulled from `last_scan.json` decisions plus market-data percent moves.
11. **Sentiment heatmap** — held + watchlist symbols, color-coded by score. Score below sentiment_floor highlighted.
12. **System health** — schedule audit summary (jobs that fired < 50% of expected), daemon blips, errors today, emails sent (count by kind). Hidden if all green.
13. **Footer** — version · git_sha · "Tomorrow's first job: massive_refresh @ 06:30 ET" · dashboard link.

### B4. Midday Snapshot — light

**Builder:** `build_midday_snapshot_email(ctx: SnapshotContext) -> Email`.

**Subject:** `Midday Snapshot · Apr 28 · +0.05% · $14,962`.

**Sections:**

1. Header bar
2. KPI grid (4 cards, no sparkline)
3. Today's trades so far
4. Open positions with intraday move column
5. Watchlist signals — any name within 2% of triggering an entry condition (informational, no email is itself an action)
6. Risk gauges (smaller — only daily loss + drawdown)
7. Footer

**Cron:** `12:00 ET, weekdays` (`hour=12, minute=0, day_of_week=mon-fri`). Today's misfire to 16:31 ET was wrong; the existing job's cron is currently `30 12 * * *` according to one of the daemon manifests (need to verify in code) — the new spec uses `0 12 * * 1-5`.

### B5. Action Alert — common shell, severity-banded, batched

**New module:** `src/trading_bot/alerts.py` with:

```python
@dataclass
class AlertEvent:
    kind: Literal["fill", "stop_hit", "auto_protect_summary",
                  "vip_tweet", "daemon_critical", "portfolio_anomaly"]
    severity: Literal["info", "warn", "bad"]
    title: str           # "Stop hit AAPL — −$184 (−4.2%)"
    detail_html: str     # rendered body content
    fired_at: datetime
    deduplication_key: str  # e.g., f"{kind}:{symbol}:{order_id}" — prevents same alert sent twice

def queue_alert(event: AlertEvent) -> None: ...   # writes to alerts_pending table
def drain_alerts() -> None: ...                   # called by cron + on-demand
```

**Throttling logic** (per user requirement: 1 digest per 20 minutes):

1. When `queue_alert()` is called:
   - Insert into `alerts_pending` table.
   - Read `last_alert_sent_at` from a new key-value meta table `bot_meta` (key `last_alert_sent_at`, value ISO-8601 string) — same pattern the codebase already uses elsewhere.
   - If `last_alert_sent_at IS NULL` OR `now() − last_alert_sent_at >= 20 min` → call `drain_alerts()` immediately.
   - Else → done; alert sits in queue.

2. **New scheduler job:** `alert_drain` runs every 1 min:
   - If `alerts_pending` non-empty AND `now() − last_alert_sent_at >= 20 min`, drain.

3. `drain_alerts()`:
   - Atomically claim all pending rows (delete-then-insert-into-sent-history).
   - If 1 alert: render single-event email.
   - If N>1 alerts: render multi-event email with one section per event, severity = max(individual severities).
   - Subject for single: `[{severity}] {title}` (e.g., `[BAD] Stop hit AAPL — −$184`).
   - Subject for batch: `[{max_severity}] {N} alerts · {top_kinds}` (e.g., `[BAD] 3 alerts · stop_hit, fill`).
   - Send via `send_logged(kind="alert")`.
   - Update `last_alert_sent_at`.

**Result:** instant delivery for the first alert in any quiet window; bursts batched. Worst-case latency is 20 min for follow-up alerts during a busy stretch.

**Dedup:** `deduplication_key` is unique-on-insert in `alerts_pending`. Same fill or stop won't be queued twice.

**Migration of existing alert sources:**

- `verify_stops` Open Positions auto-protect summary → `queue_alert(kind="auto_protect_summary", ...)` if any actions taken; nothing if all green.
- `vip_scan` high-severity → `queue_alert(kind="vip_tweet", ...)`.
- `portfolio_monitor` events → `queue_alert(kind="portfolio_anomaly", ...)`.
- Fill notifications (currently `email_fill.py`) → `queue_alert(kind="fill", ...)`.
- Stop-hit notifications (currently part of fill flow) → `queue_alert(kind="stop_hit", ...)`.
- `supervisor` daemon stall after A6 only emails on persistent stalls → `queue_alert(kind="daemon_critical", severity="bad", ...)`.

### B6. Strategy Promotion — single email per promote

**Builder:** `build_promotion_email(promo: LabPromotionRow, prev: LabPromotionRow | None) -> Email`.

**Subject:** `Strategy Promoted · {version} · fitness {fitness:.2f}`.

**Sections:**

1. Header bar (status: info)
2. Summary card — version, template, git_sha, promoted_at, fitness, "promoted by lab-promoter"
3. Params diff — old vs new for every parameter (rsi_lower, rsi_upper, ema_period, stop_pct, sentiment_floor, etc.). Cells highlighted on change.
4. Risk caps diff — same treatment for daily_loss_pct, max_drawdown_pct, max_position_pct.
5. Universe — crypto pairs + stocks_filter
6. "Watch first 24h" — placeholder validation status; the digest will track it from there.
7. Footer

**Trigger:** in `lab_promoter`, after `paper_active.json` is written and the `lab_promotions` row is inserted, call `send_logged` with this email. Only sent once per promotion (idempotent on `version`).

---

## Implementation Order

**Phase 1 — Module 1 (8 tasks):**

1. **A8** — `email_log.py` wrapper + `emails_sent` table. *Foundational; everything else uses `send_logged`.*
2. **A5** — `trade_journal` UNIQUE constraint + `INSERT OR IGNORE` + cleanup migration.
3. **A2** — Scheduler resilience (`misfire_grace_time`, `coalesce`, startup catch-up).
4. **A4** — Verify-stops cron change to `20,50 * * * *`.
5. **A6** — Stall-alert dedupe / downgrade in supervisor.
6. **A3** — `reconciler.py` + new cron jobs at 16:05 ET / 21:55 ET.
7. **A7** — `lab_promotions` table + `lab_promoter` instrumentation.
8. **A1** — `schedule_audit` job + `schedule_audits` table.

**Phase 2 — Module 2 (5–6 tasks):**

9. **B2** — `email_shell.py` module with all visual helpers (sparkline, kpi_card, progress_bar, gradient_header, severity_pill, pulse_dot, footer, etc.). Tested in isolation.
10. **B3** — Rebuild `email_digest.py` to use `email_shell` + new content (13 sections). Old `build_daily_report_html` and `build_rich_report_html` deleted from `reports.py`.
11. **B4** — `email_midday.py` (new) for the lighter snapshot. Cron move to 12:00 ET.
12. **B5** — `alerts.py` module + `alerts_pending`/`alerts_sent` tables + `alert_drain` cron job + migrations of all existing alert call-sites.
13. **B6** — `email_promotion.py` (new) + lab_promoter integration.
14. **Cleanup** — delete obsolete builders (`build_alert_email_html`, ad-hoc subjects in cli.py); update tests; verify no orphaned imports.

---

## Testing

Each task gets unit tests (TDD per the executing-plans flow). Cross-cutting test requirements:

- **Visual rendering**: Each email type rendered to a fixture HTML file (`tests/fixtures/email_*.html`) so visual regressions are easy to eyeball during development. Not asserted in CI.
- **Mobile rendering** (manual, in `docs/superpowers/specs/email_screenshots/`): ship a brief manual checklist for the user to spot-check Gmail mobile + Apple Mail iOS after Phase 2.
- **Alert throttling**: integration test asserts (a) instant delivery for first alert, (b) batching for follow-ups within 20 min, (c) dedup on identical `deduplication_key`.
- **Reconciler idempotency**: running it twice on the same Alpaca state produces no duplicates.
- **Schedule audit**: simulate today's data — assert the audit flags stock_scanner.

---

## Out of Scope / Open Questions

- **Strategy logic untouched.** The lab promotion flow keeps writing `paper_active.json`; we just add observability around it.
- **Alpaca order-history API** — A3's reconciler depends on Alpaca returning historical orders. If they only retain N days, old closes can't be reconstructed; the spec acknowledges this with `exit_reason="reconciled_from_audit"`.
- **Outlook 2007–2019 sparkline degradation** — accepted per user. We don't add a fallback `<img>` tag.
- **Watchlist movers data source** — pulls from existing scanner output (`last_scan.json` + market_data). If the scanner didn't run that day (e.g., today's bug), this section shows "No scan data."
- **Email send failures** — if SMTP fails, `email_log` records `outcome="failed"` and the next digest surfaces it; we don't retry.

---

## Risks

1. **Daemon scheduler resilience (A2)** — adding `misfire_grace_time + coalesce` is generally safe but could in theory replay a job mid-state-change. Worst case: a stock_scanner runs twice in a minute. The journal de-dupe (A5) keeps duplicates out of the trade table; the scanner itself should be idempotent.
2. **Reconciler accuracy (A3)** — for a fresh implementation, the first run on yesterday's positions may misclassify some closes if Alpaca's order history has gaps. Mark them `exit_reason="reconciled_from_audit"` and continue. User can manually edit if needed.
3. **Email rebuild scope (Phase 2)** — replacing 11 email types with 4 means the user's expectations need to migrate. We delete the old subjects entirely; if the user has Gmail filters keyed to "Trading Bot — Daily Report", they break. Mitigation: list all old subjects in the spec migration notes.
4. **Alert throttling latency (B5)** — first alert in a quiet window: 0 latency. Follow-up: up to 20 min. For truly-critical events (e.g., daemon down 10 min), the supervisor still uses the bypass path that doesn't wait for alert drain. So worst-case for "you need to act now" is whatever the alert source's cadence is, not 20 min.
