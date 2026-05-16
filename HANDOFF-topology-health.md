# System Topology — Health-Coded Flows

**Change summary:** every flow edge is now **green** by default. Anomalies surface
as **amber (warn)** or **red (broken)** edges with brighter strokes, a glowing
drop-shadow, pulse animation, and a small `!` medallion at the midpoint. Edge
labels recolor to match. Hard halt forces every flow into the broken/halt state.

Per-edge status is computed from your existing `anomalies` flags
(`recon_mismatch`, `lock_mismatch`, `unknown_nvda`, `over_cap`) so no new data
is required — just pass `anomalies` to `<Topology />`.

You can also override per edge by setting `status: "warn" | "broken"` directly
on any edge in `TOPO.edges`.

---

## 1. CSS — `v5/styles.css`

**Find** the existing topology edge block (search for `.topo .edge.flow {`) and
replace from `.topo .edge.flow` through the `dashmarch` keyframes with:

```css
/* Default: every flow is green & healthy. Anomalies pop. */
.topo .edge.flow {
  stroke: var(--up);
  stroke-width: 1.2;
  stroke-dasharray: 4 3;
  animation: dashmarch 14s linear infinite;
}
.topo .edge.flow.dim { stroke: var(--rule); opacity: 0.5; animation: none; }

.topo .edge.flow.warn {
  stroke: var(--warn);
  stroke-width: 1.5;
  stroke-dasharray: 4 3;
  animation: dashmarch 9s linear infinite, edgePulseWarn 2.4s ease-in-out infinite;
}
.topo .edge.flow.broken {
  stroke: var(--down);
  stroke-width: 1.8;
  stroke-dasharray: 5 3;
  animation: dashmarch 5s linear infinite, edgePulseBroken 1.1s ease-in-out infinite;
  filter: drop-shadow(0 0 5px var(--down));
}

.topo .edge.research {
  stroke: var(--info-dim);
  stroke-dasharray: 2 4;
  opacity: 0.55;
}
.topo .edge.research.broken {
  stroke: var(--down);
  opacity: 1;
  filter: drop-shadow(0 0 5px var(--down));
  animation: edgePulseBroken 1.1s ease-in-out infinite;
}
.topo .edge.halt {
  stroke: var(--down);
  stroke-width: 1.6;
  stroke-dasharray: 5 4;
  animation: dashmarch 6s linear infinite, edgePulseBroken 1.1s ease-in-out infinite;
  filter: drop-shadow(0 0 5px var(--down));
}
@keyframes dashmarch       { to { stroke-dashoffset: -28; } }
@keyframes edgePulseBroken { 0%, 100% { opacity: 1; }   50% { opacity: 0.45; } }
@keyframes edgePulseWarn   { 0%, 100% { opacity: 0.95; } 50% { opacity: 0.6; } }

.topo .edge-label {
  font-family: "Geist Mono", monospace;
  font-size: 9.5px;
  fill: var(--text-ghost);
  letter-spacing: 0.04em;
}
.topo .edge-label.warn   { fill: var(--warn); font-weight: 600; }
.topo .edge-label.broken { fill: var(--down); font-weight: 600; }

/* Fault badge (!) at midpoint of a broken edge */
.topo .edge-fault circle {
  fill: var(--down);
  filter: drop-shadow(0 0 4px var(--down));
  animation: edgePulseBroken 1.1s ease-in-out infinite;
}
.topo .edge-fault text {
  fill: var(--bg);
  font-family: "Geist", sans-serif;
  font-size: 9px;
  font-weight: 700;
  text-anchor: middle;
  dominant-baseline: central;
}
```

Notes:
- Uses your existing `--up`, `--warn`, `--down` tokens (no new vars).
- The previous `.topo .edge.halt` used `--halt` (teal) — now it uses `--down`
  (red) so a halted system reads as alarming, not informational.

---

## 2. Topology component — `v5/topology.jsx`

**Replace** the `Topology` function signature + body (down through the edges
`.map(...)` block) with:

```jsx
function Topology({ focus, onFocus, halted, anomalies }) {
  const W = TOPO.width;
  const H = TOPO.height;
  const nodeMap = Object.fromEntries(TOPO.nodes.map(n => [n.id, n]));

  // Map anomalies → per-edge health overrides. Default is "ok" (green).
  // Returns "ok" | "warn" | "broken".
  const a = anomalies || {};
  const edgeStatus = (e) => {
    // explicit status on the edge wins
    if (e.status) return e.status;
    // Reconciliation mismatch: broker→ledger fill_events suspect
    if (a.recon_mismatch && e.to === "ledger" && e.label === "fill_event") return "broken";
    // Lock mismatch: kernel ↔ policy hash check fails
    if (a.lock_mismatch && e.from === "kernel" && e.to === "policy") return "broken";
    // Unknown position: exec → stocks flow can't be trusted
    if (a.unknown_nvda && e.from === "exec" && e.to === "stocks") return "warn";
    // Crypto cap touched: the unwind path is hot
    if (a.over_cap && e.from === "exec" && e.to === "crypto") return "warn";
    return "ok";
  };

  return (
    <div className="topo">
      <div className="topo-canvas">
        <svg className="topo-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
          {/* Frame */}
          <rect x="0.5" y="0.5" width={W - 1} height={H - 1} fill="none" stroke="var(--hair)" strokeDasharray="2 4" />
          {/* Axis labels */}
          <text className="axis-label" x="14" y="20">FACTORY → KERNEL → EXEC → LANES → LEDGER</text>
          <text className="axis-label" x={W - 12} y="20" textAnchor="end">{halted.active ? "STATE · HALTED" : "STATE · RUNNING"}</text>
          <text className="axis-label" x="14" y={H - 8}>L2 · paper · no live capital</text>

          {/* Crosshair midline */}
          <line className="crosshair" x1="0" y1={H / 2} x2={W} y2={H / 2} opacity="0.4" />

          {/* Edges */}
          {TOPO.edges.map((e, i) => {
            const from = nodeMap[e.from], to = nodeMap[e.to];
            if (!from || !to) return null;
            const d = edgePath(from, to, e.kind, e.curve);
            const status = edgeStatus(e);
            // Hard halt forces every "flow" edge into the halt state visually
            const isHalt = halted.active && e.kind === "flow";
            const cls = isHalt ? "halt" : `${e.kind} ${status === "ok" ? "" : status}`.trim();
            const [fx, fy] = nodeCenter(from);
            const [tx, ty] = nodeCenter(to);
            const lx = (fx + tx) / 2;
            const ly = (fy + ty) / 2 - 4;
            const showFault = status === "broken" && !isHalt;
            return (
              <g key={i}>
                <path d={d} className={`edge ${cls}`} />
                {/* edge label at midpoint */}
                {e.label ? (
                  <text className={`edge-label ${status !== "ok" ? status : ""}`} x={lx} y={ly} textAnchor="middle">
                    {e.label}
                  </text>
                ) : null}
                {/* fault badge — small ! medallion at midpoint */}
                {showFault ? (
                  <g className="edge-fault" transform={`translate(${lx + 28},${ly + 4})`}>
                    <circle r="8" />
                    <text>!</text>
                  </g>
                ) : null}
              </g>
            );
          })}
```

Everything after that point (node rendering, side detail panel, exports) stays
exactly the same.

---

## 3. Caller — `v5/surface_right_now.jsx`

The `SurfaceRightNow` component already has `anomalies` in scope. **Find** the
existing `<Topology …>` call and add the `anomalies` prop:

```diff
- <Topology focus={focusNode} onFocus={setFocusNode} halted={status.halted} />
+ <Topology focus={focusNode} onFocus={setFocusNode} halted={status.halted} anomalies={anomalies} />
```

---

## Verifying

Use the Tweaks panel → **Anomaly injectors**:

| Toggle              | Edge that should turn red/amber                |
| ------------------- | ----------------------------------------------- |
| Recon mismatch      | `stocks → ledger`, `crypto → ledger`, `options → ledger` (all `fill_event`) — **broken/red** |
| Lock mismatch       | `kernel → policy` (`hash-check`) — **broken/red**     |
| Unknown NVDA        | `exec → stocks` (`fill`) — **warn/amber**            |
| Crypto cap touched  | `exec → crypto` (`unwind`) — **warn/amber**          |
| Halted              | every `flow` edge — **broken/red** (halt class)      |

With all anomaly toggles off, every flow should be a healthy green.

---

## Optional — extend `edgeStatus` for more failure modes

If you wire additional health signals (freshness lag, drift breach, kill-switch
trip), add them to `edgeStatus`. Pattern is just `if (signal) return "warn" | "broken";`.
For one-off demos you can also set `status: "broken"` directly on any entry in
`TOPO.edges` (in `v5/data.jsx`) — explicit status wins over anomaly mapping.
