# Handoff: Trading Bot Cockpit (v4)

A four-surface internal cockpit for an autonomous systematic trading kernel ("kernel v4"). The operator is a single solo-retail engineer who watches it like an SRE watches a service. Currently L2 autonomy: autonomous research, **no autonomous live trading**. Trades on Alpaca paper today.

> **Important:** the files in this bundle are **design references** written as a React+Babel single-page HTML prototype. They show the intended look, layout, density, motion, and behavior. **Do not ship the HTML directly.** Recreate the designs in your target codebase using its established stack (React, Vue, SvelteKit, etc.) and its existing design-system primitives where they exist. If no codebase environment exists yet, React + Vite + TypeScript + Tailwind/CSS Modules + Recharts is a reasonable default — but match what the operator already uses.

---

## Fidelity

**High-fidelity.** Final colors (oklch values listed below), final typography (Geist Sans + Geist Mono), final spacing, final motion. The hi-fi prototype was iterated against the operator's product brief (see `original_product_brief.pdf` — required reading before you start).

---

## What's in the box

| File | Purpose |
| ---- | ------- |
| `Trading Bot Cockpit (v4).html` | Main shell — open in a browser to see the live prototype. |
| `styles-v4.css` | Complete design system: tokens, layout primitives, all component styles. |
| `app-v4.jsx` | Root React app — tab routing, top-level state, keyboard shortcuts, halt flow. |
| `topbar.jsx` | Persistent top bar + Halt and Resume modals. |
| `topology.jsx` | The SVG system map (Right Now bottom section). |
| `surface_right_now.jsx` | Surface A — positions / orders / posture / equity / exposure. |
| `surface_activity.jsx` | Surface B — daily digest, decision activity, lessons, last scan. |
| `surface_lab.jsx` | Surface C — strategy registry, walk-forward folds, mutations, intake, promotion queue. |
| `surface_system.jsx` | Surface D — jobs, freshness, recon, drift, cost model, policy locks, personas, halts, ledger, heartbeat. |
| `components.jsx` | Reusable primitives (StatusPill, RiskCapBar, ClassificationTag, LedgerSeqChip, TierBadge, HashVerifiedCheck, LensToggle, Panel, Sparkline, EquityChart, Donut, Icon). |
| `data.jsx` | Realistic mock state — replace with API calls. |
| `tweaks-panel.jsx` | In-prototype only; remove in the real build. |
| `original_product_brief.pdf` | The operator's brief. Authoritative for product semantics. |

---

## Non-negotiable product constraints (from the brief)

These come from the kernel's safety model, not taste. **Do not deviate.**

1. **The kill switch is sacred.** Top-right of every page. Halting requires a typed reason (min 4 chars) and two ack checkboxes. While halted, a banner spans the page width, the button becomes a green "Resume" with its own modal, and all new entries stop. The halt color is reserved — nothing else in the UI uses it.
2. **No celebratory styling for P&L.** Up green, down red — but never confetti, gradients, hero numbers, or emoji.
3. **Provenance over polish.** Every non-trivial number must be reachable to a source: ledger seq, lock hash, strategy version, timestamp. There's a `.prov` style for this and a global `data-prov="on|off"` switch.
4. **Research ≠ Trading.** The Strategy Lab surface is visually quarantined — diagonal-stripe background, a "RESEARCH" banner, and the only affordance that touches the kernel (promotion) is rendered but disabled with a "requires typed approval" tag.
5. **Localhost-only aesthetic.** Internal tool. No marketing affordances, hero sections, onboarding, or "what's new" modals.
6. **Information density is a feature.** Compact ≈ 6–10 panels visible without scrolling on 1440 wide.
7. **Pessimistic cost lens is the default.** Strategy performance defaults to the pessimistic backtest. Other lenses (raw, broker-paper) are togglable but secondary.
8. **No invented data.** If a number isn't in the ledger or snapshot API, don't display it.

---

## Information architecture

A persistent top bar over four surfaces:

| Surface | Question it answers | Refresh cadence |
| ------- | ------------------- | ---------------- |
| **Right Now** (default) | Is the kernel healthy and what is it doing this minute? | Live (10–25s) |
| **Recent Activity** | What happened in the last hours / day? | 1–5 min |
| **Strategy Lab** | What is research proposing, and what's the validation state? | On demand |
| **System Health** | Is the plumbing OK — jobs, data freshness, reconciliation, costs, hashes? | 30–60s |

Keyboard: `g r / g a / g l / g s` switch surfaces. `Ctrl + .` opens halt/resume. `?` toggles the shortcut overlay. Halt requires explicit click + typed reason — **no keyboard shortcut to kill.**

---

## Design tokens

### Color (dark, default)

Defined in `styles-v4.css` `:root` / `[data-theme="dark"]`. All colors are `oklch()` so you get perceptual stability.

```
--bg               oklch(0.165 0.006 252)   page background
--bg-2             oklch(0.190 0.006 252)
--panel            oklch(0.205 0.006 252)
--panel-2          oklch(0.230 0.008 252)
--panel-hi         oklch(0.265 0.010 252)
--border           oklch(0.290 0.008 252)
--border-strong    oklch(0.360 0.010 252)

--text             oklch(0.945 0.004 252)
--text-dim         oklch(0.700 0.010 252)
--text-faint       oklch(0.510 0.012 252)
--text-mono        oklch(0.880 0.006 252)

--success          oklch(0.745 0.155 145)   up · ok · verified
--success-dim     ·-bg     · (see file)
--danger           oklch(0.680 0.190 25)    down · fail
--warn             oklch(0.800 0.150 75)    needs attention but not broken
--info / --accent  oklch(0.760 0.110 230)   neutral primary accent; lensed buttons, links

# RESERVED — halt state only. Nothing else uses this hue.
--halt             oklch(0.65 0.16 195)     saturated teal
--halt-dim         oklch(0.50 0.12 195)
--halt-bg          oklch(0.34 0.10 195 / 0.22)
--halt-bg-strong   oklch(0.40 0.13 195 / 0.35)

# Lane colors (used by topology + lane chips)
--lane-stocks      oklch(0.75 0.14 215)
--lane-crypto      oklch(0.78 0.16 100)
--lane-options     oklch(0.68 0.13 160)
--lane-system      oklch(0.55 0.04 250)
```

There is a light theme defined under `[data-theme="light"]` — same tokens, different values. Theme is set on `<html data-theme="dark|light">`.

### Typography

| Family | Use |
| ------ | --- |
| **Geist Sans** (400/500/600/700) | All UI text, panel headers, labels, body. |
| **Geist Mono** (400/500/600) | All numbers, hashes, timestamps, ledger seqs, symbols, code. Tabular numerals everywhere a column has numbers. |

Sizes are controlled by CSS variables (`--fs-body 12.5px`, `--fs-mono 12px`, `--fs-mini 11px`, `--fs-h 13px` in compact density). Comfortable density bumps them up by ~1px each — see the `[data-density="compact|comfortable"]` blocks in `styles-v4.css`.

### Spacing / density

`<html data-density="compact|comfortable">` swaps a set of variables: row height, panel padding, cell padding, font sizes, gaps. Compact ≈ 28px rows, comfortable ≈ 36px rows.

### Borders / radius / shadows

- Standard panel: `border: 1px solid var(--border); border-radius: 8px;`
- Subtle dividers: `border-bottom: 1px solid var(--border)` (solid) or `1px dashed var(--border)` (dashed list separators).
- Shadows: `--shadow` and `--shadow-sm` defined; used sparingly (modals, tooltips).

### Motion

Minimal by design.
- New activity feed rows fade in over 600ms (`@keyframes fadeNew`).
- Risk-cap bars animate width over 600ms.
- The status pill "running" dot has a 2.4s `breath` pulse.
- "Unknown" classification tag has a 1.4s alarm `rowblink`.
- ECG heartbeat sparkline ticks at ~5 fps.
- A `prefers-reduced-motion` block kills all animation.

---

## Surface specifications

### Top bar (every page)

Left → right, 50px tall, sticky:

1. **Logo:** mark + "kernel v4.2.0"
2. **System status pill** — one of `running` / `degraded` / `halted` / `down`. Colored per token; the halted state uses the reserved teal.
3. **Equity + day P&L** — `$54,317.42 ▴ +$184 (+0.34%)`. Click → equity curve drawer (currently a stub).
4. **Active lanes** — chips for ETF Momentum, Crypto, Wheel(off). Each chip has a thin inner exposure bar; chip is amber at ≥80% of cap, red at cap.
5. **Risk profile** — `safe` / `neutral` / `aggressive`. Click → profile drawer (stub).
6. **Clock** — UTC HH:MM:SS, monospace.
7. **KILL button** — top-right. Red, pulsing dot. Click opens halt modal. While halted, becomes green "Resume" and the halted banner appears below the top bar.

### Halt flow (most safety-critical interaction)

`HaltModal` requires:
- Typed reason ≥ 4 characters (written to the ledger as `manual_operator_halt`).
- Two ack checkboxes:
  - "I understand this writes `manual_operator_halt` to the ledger."
  - "I will resume manually — there is no auto-resume timer."
- Confirming sets system_state = halted, logs the event, shows the page-wide teal banner, and converts the kill button to "Resume."

`ResumeModal` requires one ack ("I have reviewed open orders, positions, and the latest reconciliation pass").

### Right Now

3 columns on ≥1440, 2 columns on 1024–1439 (Action Required moves above Positions), 1 column below 1024.

**Column 1 — Posture** (320px):
- Strategy Mode (5 rows: name, state pill — armed/paused/research_only/live/retired)
- Regime (single tag — bullish/chop/risk-off — + 5 macro signals as `name : value` rows)
- Risk caps (6 bars — account exposure, lane caps, PDT, single-name; amber at 80%, red at cap; thin tick mark at cap)

**Column 2 — Positions + open orders** (flex):
- Positions table, **lane-grouped** (Stocks / Crypto / Options group headers always shown). Columns: Symbol, Qty, Entry, Mark, P&L $, P&L %, Classification, Stop, Age.
- Classifications: `bot` (cyan), `external` (gray), `manual` (amber), `unknown` (red, animated). **Unknown is visually loud** — the kernel halts on unknown in Phase 2+.
- Click a row to expand: shows strategy_version, order_uid, opened_at, drift bps. For unknown rows, expand also shows a classify/close action card.
- Open orders table below: symbol, side, qty, type, status, age, idempotency, client_order_id. Strikethrough on canceled; amber row when stuck > 60s.

**Column 3 — Action required + activity feed** (380px):
- Action required: empty state when the kernel is calm ("nothing to act on — boring on purpose"). Each card has severity (high/med), title, cause, and 1–2 CTAs.
- Activity feed: live tail (~50 rows). Each row: `ts · type · lane · message · seq`. Type is a tiny letter chip color-coded by event class. Filterable by lane.

**Footer (full-width 2-up):**
- Equity curve — line chart, range selector `1w / 1m / 3m / ytd / all`. Vertical dashed markers for halts (teal), profile changes (amber), lock changes (accent-blue). Cost-lens toggle in the panel header.
- Exposure donut — by lane, summing to 100% of equity. Cash is a slice.

**System Map section (NEW in v4 — below the footer):**

An SVG topology of the kernel rendered at `viewBox 1300×760`. Nine nodes laid out top→bottom:

- Top row: Research Factory (left), Scheduler (center), Ledger (right)
- Center: Risk Kernel (wide bar)
- Below: Execution → Broker (Alpaca paper)
- Bottom: three lane outputs (Stocks · Crypto · Options)

Edges connect them with explicit kinds:
- `flow` and `primary` — solid accent color, marching 5px,7px dashes (`@keyframes dashmarch` 1.6s linear infinite)
- `research` — dotted, dim
- `dim` — for the "off" path to Options and the reconciliation feedback loop
- `halt` — when halted, all edges flip to dashed teal

Nodes are clickable. Click selects the node and the **detail panel on the right** of the canvas updates to show that node's internals (a `stat`, `caps`, `regime`, `kv table`, `positions list`, `jobs`, etc., per `nodeDetail(id)` in `topology.jsx`). Click × or another node to switch.

Each node has a colored status dot (top-right corner). Failing nodes get an expanding halo animation. Each node has corner-tick brackets that intensify when selected.

### Recent Activity

Single column, reverse-chronological.

- **Daily digest** at top — 6 stat tiles in a row (equity Δ 24h, fills, orders submitted, scans, mutations proposed, halts).
- **Decision activity** — each row is one decision (entry / exit / skip / mut-ok / mut-rej). Click to expand: shows the gate-by-gate evaluation (`gate name : value / threshold`, green check or red ×) and provenance metadata.
- **Lessons** — markdown-ish notes the system or operator wrote. Tagged by lane.
- **Lane summary** — small table of per-lane fills / submits / skips / P&L over the last 24h.

### Strategy Lab

Visually distinct from Right Now: diagonal striped background (`repeating-linear-gradient(135deg, transparent 24px, halt-bg 25px)`) + a "RESEARCH" banner.

- **Strategy registry** table: every version with state, tier badge (T1/T2/T3), pessimistic Sharpe, deflated Sharpe, PBO, last_run, hash.
- **Promotion queue** — the only lab affordance that touches the kernel. Rendered with `disabled` button and a "requires typed approval" tag. Promotion is a multi-step, human-signed process — **do not surface a one-click promote.**
- **Drilldown** for the selected strategy: walk-forward folds (5 + 30% locked holdout, each with a sparkline), parameter plateau heatmap (11×11), mutation log, validation tiers achieved.
- **Hypothesis intake** — three submission modes: `draft` (no run), `intake` (adversarial review), `mutate` (enumerate hash-locked search space). Submission triggers an async research run.
- **LLM spend** — line chart of cost by role: judges on Opus, reviewers on Sonnet, etc. Today / month / monthly budget. **Halts research if today > $20.**

### System Health

Two columns.

**Column 1 — plumbing:**
- Job scheduler — every APScheduler job with last_run, duration, next_run (live countdown), status. Failed runs styled red with the error message inline.
- Data freshness — per-lane data source watermarks with lag in seconds, fresh/stale chip.
- Reconciliation — last run, total reconciled, mismatches, unresolved (links to detail).
- Drift monitor — 20-trade rolling drift, sparkline, current vs threshold.

**Column 2 — trust and accounting:**
- Cost model — per-trade cost in all three lenses (raw / broker_paper / pessimistic) as three tiles; the active lens is highlighted.
- Policy locks — 9 lock files with version, last change, signer, hash status. **Mismatch is a page-level emergency** — `stat-pill.mismatch` blinks.
- Personas — 8 personas with hash status, same treatment as locks.
- Halt history — last 30 days.
- Ledger health — table row counts, last seq, last hash, **chain-verified pill** (the single most reassuring element on the dashboard when green). Below the table: a strip of the last 60 blocks as small chevron-ish tiles.
- Daemon heartbeat — live ECG sparkline + uptime, host, pid.

---

## Component primitives (build these once)

These are in `components.jsx`. Recreate as your codebase's pattern requires:

| Component | Purpose |
| --------- | ------- |
| `<StatusPill state="running\|degraded\|halted\|down" />` | Top-bar / inline status chip. Halt state uses the reserved teal token. |
| `<ClassificationTag value="bot\|external\|manual\|unknown" />` | For positions table. Unknown is animated. |
| `<LedgerSeqChip seq={28411} />` | Small mono chip with a leading `≡` glyph. |
| `<TierBadge tier={1\|2\|3} />` | T1/T2/T3 validation tier. |
| `<HashVerifiedCheck ts="13:02:11Z" status="verified\|mismatch" />` | Reassuring "chain verified at HH:MM:SS" pill. |
| `<LensToggle value="raw\|broker_paper\|pessimistic" />` | Tri-segmented control for the cost lens. |
| `<RiskCapBar name used cap unit />` | Label + value + segmented bar. Amber at 80%, red at 100%. |
| `<Panel title sub actions>` | Universal container — header rule, ellipsis-truncated subtitle, slot for actions on the right. |
| `<Prov>provenance text</Prov>` | Small monospace metadata line. Globally hidden when `data-prov="off"`. |
| `<Icon name size />` | Hand-rolled lucide-style line icons. |
| `<Sparkline values height color />` | Tiny line chart, optional fill, "over" mode tints orange. |
| `<EquityChart data />` | Big equity line + markers. |
| `<Donut data />` | Exposure breakdown. |

---

## Data contracts (back-end API)

The dashboard binds to four JSON endpoints. **Approximate shapes** — see `data.jsx` for the full mock and the brief PDF for the canonical spec.

### `GET /api/status` — drives top bar + Right Now posture (poll every 10–25s)

```ts
type Status = {
  system_state: "running" | "degraded" | "halted" | "down";
  halted: { active: boolean; reason: string | null; since: string | null; operator: string | null };
  risk_profile: "safe" | "neutral" | "aggressive";
  account: {
    equity: number;       // 54317.42
    cash: number;
    day_pl_abs: number;
    day_pl_pct: number;   // 0.0034 = +0.34%
    buying_power: number;
  };
  lanes: Array<{ name: string; enabled: boolean; exposure_pct: number; cap_pct?: number }>;
  kill_switches: Array<{ name: string; active: boolean }>;
  boot_check: { ok: boolean; hash_verified_at: string };
};
```

### `GET /api/snapshot` — drives Right Now panels (poll 30–60s)

```ts
type Snapshot = {
  positions: Position[];
  open_orders: OpenOrder[];
  regime: { label: "bullish" | "chop" | "risk-off"; since: string; signals: Array<{ name: string; val: string; trend: "up"|"down"|"flat" }> };
  risk_caps: Array<{ name: string; used: number; cap: number; unit: "%" | "#" }>;
  activity: ActivityEvent[];
  action_required: ActionItem[];
};

type Position = {
  symbol: string; lane: "stocks" | "crypto" | "options"; qty: number;
  entry: number; mark: number; pl_abs: number; pl_pct: number;
  classification: "bot" | "external" | "manual" | "unknown";
  stop: number | null; opened_at: string;
  order_uid: string | null; strategy_version: string | null; drift_bps: number | null;
};
```

### `GET /api/equity-curve?range=1w|1m|3m|ytd|all`

```ts
type EquityCurve = {
  range: string;
  points: Array<{ ts: string; equity: number }>;
  markers: Array<{ i: number; kind: "halt" | "profile" | "lock"; label: string }>;
};
```

### Per-fragment HTMX-style endpoints

The full list (from the brief, already populated server-side): `action_required`, `header`, `strategy_mode`, `regime`, `kpi`, `risk`, `exposure`, `equity`, `orders`, `activity_feed`, `decision_activity`, `lessons`, `last_scan`, `opportunities`, `stats`, `lab_evolution`, `calibrator`, `threshold_overrides`, `intel_pool`, `proposals`, `llm_spend`, `wheel`, `role_health`, `scheduled`, `freshness`, `email_firehose`, `process_registry`, `sidebar_status`, `halts`, `portfolio_rail`, `macro_alloc`, `node_drilldown`.

### POST endpoints (mutations)

- `POST /api/halt` — body `{ reason: string }`. Server writes `manual_operator_halt` to the ledger and returns the new status. Reason min 4 chars enforced server-side too.
- `POST /api/resume` — no body required, but require auth and write a `policy.resume` event to the ledger.
- `POST /api/positions/{symbol}/classify` — `{ classification: "bot" | "external" | "manual" }`.
- `POST /api/positions/{symbol}/close` — close at market.
- `POST /api/strategies/{version}/promote` — multi-step. Must verify hash match + typed approval. **No one-click promote.**

---

## State management

Top-level state (lifted in `app-v4.jsx`):

- `surface: "right_now" | "activity" | "lab" | "system"` — current tab
- `status: Status` — from `/api/status`, polled
- `snapshot: Snapshot` — from `/api/snapshot`, polled
- `activity: ActivityEvent[]` — appended to (prepend) on each poll or websocket frame
- `equityRange: "1w" | "1m" | "3m" | "ytd" | "all"`
- `costLens: "raw" | "broker_paper" | "pessimistic"` — persisted to localStorage; default `"pessimistic"`
- `theme: "dark" | "light"` — persisted to localStorage; sets `<html data-theme>`
- `density: "compact" | "comfortable"` — persisted to localStorage; sets `<html data-density>`
- `showProvenance: boolean` — persisted; sets `<html data-prov>`
- `haltModalOpen: boolean`
- `resumeModalOpen: boolean`
- `selectedNode: string | null` — for the System Map detail panel

Live updates: HTMX-style endpoints, server-sent events, or a websocket pushing ledger events. Activity feed and ledger seq counter should feel live (sub-5s latency). Risk-cap bars, exposure, equity refresh on a slower cadence.

---

## Interactions & behavior

| Interaction | Spec |
| ----------- | ---- |
| Tab switch | `g r/a/l/s` (after a 900ms prefix timeout) or click. Active tab gets an underline in `--info` (or `--halt` for Strategy Lab). |
| Position row | Click to expand inline. Expanded row shows strategy_version, order_uid, opened_at, drift_bps. Unknown rows additionally show an action card with Classify/Close buttons. |
| Activity feed filter | Buttons for All / Stocks / Crypto / Options / System. Filter applies to the live tail. |
| Equity range | Buttons `1w / 1m / 3m / ytd / all`. Refetches the curve. |
| Cost lens | Toggle in the equity panel header and in System Health → Cost Model. Persisted. |
| Halt button | Opens HaltModal. Required: reason ≥ 4 chars + both ack checkboxes. Esc closes. Submit calls `POST /api/halt`. |
| Resume button (while halted) | Opens ResumeModal. Required: one ack. Submit calls `POST /api/resume`. |
| Kbd Ctrl + . | Opens halt or resume modal depending on current state. |
| ? | Toggles a small floating shortcut-help card bottom-left. |
| Esc | Closes any open modal / overlay. |
| System Map node | Click → side panel updates with that node's internals. Click × or another node to switch. Hover dims unrelated nodes/edges for clarity. |
| Promotion queue button | Renders but `disabled`. Promotion is intentionally not one-click. |

---

## Responsive behavior

- `≥1440`: 3-column Right Now, 2-column System Health.
- `1024–1439`: 2-column; Action Required moves above Positions.
- `<1024` (tablet / phone): single column; the top bar collapses to `status pill + equity + kill button`. The brief explicitly does **not** want a full mobile build — just a glanceable mobile view of top bar + Action Required.

---

## Accessibility

- WCAG AA on all text + chart annotations. Pair every red/green with an arrow or symbol — never rely on color alone.
- Halt requires explicit click + typed reason — **no keyboard shortcut to fire it.**
- Respect `prefers-reduced-motion` — kill the dash-march, breath pulse, rowblink, and shimmer animations.

---

## What NOT to ship

Quoted from the brief:

- One-click strategy promote-to-live.
- Suggested trades or "AI picks" on Right Now.
- A composite "AI confidence" score.
- Charts of LLM responses, conversation transcripts, or chain-of-thought.
- Marketing affordances: changelog modals, "what's new" carousels, achievements, streaks.
- Onboarding, signup, billing, mobile apps.

---

## Implementation notes

- The prototype loads React 18.3.1, ReactDOM 18.3.1, and `@babel/standalone` from unpkg with integrity hashes. In a real codebase, drop the Babel runtime and compile JSX/TSX at build time.
- The `tweaks-panel.jsx` is a prototype-only floating control panel. **Delete it.** Replace with real settings: a small profile menu in the top bar lets the operator toggle theme, density, and the global provenance switch. Persist to localStorage on every change.
- The mock data in `data.jsx` is deterministic and rich enough to exercise every state. Use it as your test fixture set.
- Number formatting: tabular numerals everywhere (`font-variant-numeric: tabular-nums`). Money formats to 2dp with locale `en-US`. Percentages to 2dp with explicit sign on signed values. Ages roll up `Ns` → `Nm Ns` → `Nh Nm` → `Nd`.
- Icons are hand-rolled in `components.jsx` matching the lucide line set. You can replace them with `lucide-react` directly — same visual language.

---

## Build sequence I'd suggest

1. Stand up the design tokens + Geist fonts + dark/light theme switching.
2. Build the top bar with a stubbed status feed. Get the halt → resume flow working end-to-end against a fake `POST /api/halt`.
3. Wire `/api/status` polling and the status pill / equity / lane chips.
4. Right Now column 1 (posture) — strategy mode, regime, risk caps. Static data is fine here.
5. Right Now column 2 — positions table with lane grouping + expansion + classification states (especially `unknown`).
6. Right Now column 3 — action required + activity feed (live tail).
7. Right Now footer — equity curve + exposure donut.
8. The System Map section — port `topology.jsx`. Nodes are static; edges are computed in `pathFor()`; flow animation is pure CSS.
9. System Health surface — start here on day 2; the diagnostic affordances will pay back fast when something breaks in dev.
10. Strategy Lab surface — last, since it's research-only and gated.
11. Recent Activity surface.

---

## Reference: oklch quick decoder

If your toolchain doesn't grok oklch yet (it's well-supported in all evergreen browsers as of mid-2024):

- `oklch(L C H)` — L is lightness 0–1, C is chroma 0–0.4-ish, H is hue degrees 0–360.
- `0.745 0.155 145` ≈ green
- `0.680 0.190 25`  ≈ red
- `0.800 0.150 75`  ≈ amber
- `0.760 0.110 230` ≈ cool cyan-blue (info / accent)
- `0.65 0.16 195`   ≈ saturated teal (halt — reserved)

If you must fall back to hex, sample these in the browser — but oklch is the source of truth.

---

Questions / clarifications for the operator before implementation:

- Confirm the authentication model on these endpoints (single-user, but how — local cookie? SSH-tunnel-only?).
- Confirm the polling vs WebSocket / SSE decision for the activity feed.
- Confirm tablet/phone scope (the brief says glanceable top-bar + action required only — confirm).
- Confirm whether the System Map is its own URL (`/right-now#map`) or pure scroll-down.

Good luck. The brief is the source of truth — re-read it before committing to anything you're unsure about.
