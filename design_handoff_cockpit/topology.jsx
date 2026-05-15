// ============================================================
// topology.jsx — the system map
// Renders the kernel as a live SVG topology.
// ============================================================

// Nodes: layout in viewBox coords (1300 × 740). The server's
// /api/cockpit/data-overlay.js sets window.TOPOLOGY_NODES with live
// status + metric per node; we fall back to a documented baseline if
// the overlay didn't run. Layout retains the original positions so
// existing edge anchors line up; regime + mutation are tucked into
// the previously-free horizontal corridor at y=370.
const _TOPO_NODES_BASELINE = [
  { id: "research",  title: "Research Factory", sub: "scout · intake · codegen", x:  60, y:  60, w: 260, h: 100, status: "ok",   metric: "—",                 group: "research" },
  { id: "scheduler", title: "Scheduler",        sub: "jobs",                     x: 520, y:  60, w: 260, h: 100, status: "ok",   metric: "—",                 group: "kernel"   },
  { id: "ledger",    title: "Ledger",           sub: "append-only · hash chain", x: 980, y:  60, w: 260, h: 100, status: "ok",   metric: "—",                 group: "kernel"   },

  { id: "risk",      title: "Risk Kernel",      sub: "caps · regime overlay · kill switches",
                                                                                 x: 220, y: 220, w: 820, h: 130, status: "ok",   metric: "—",                 group: "kernel", primary: true },

  // Phase A — Regime classifier (tucked left of execution).
  { id: "regime",    title: "Regime",           sub: "5-regime · 3 classes",     x:  60, y: 405, w: 220, h:  70, status: "ok",   metric: "normal",            group: "research" },
  // Phase C — Mutation engine (tucked right of execution).
  { id: "mutation",  title: "Mutation",         sub: "nightly · BH-FDR",         x: 1020, y: 405, w: 220, h:  70, status: "ok",   metric: "—",                 group: "research" },

  { id: "execution", title: "Execution",        sub: "idempotent order router",  x: 470, y: 400, w: 320, h:  80, status: "ok",   metric: "—",                 group: "kernel"   },
  { id: "broker",    title: "Broker · Alpaca",  sub: "paper",                    x: 470, y: 510, w: 320, h:  70, status: "ok",   metric: "—",                 group: "broker"   },

  { id: "lane-stocks",  title: "Stocks",        sub: "ETF / Dual Momentum v3",   x:  60, y: 620, w: 280, h: 100, status: "ok", metric: "—",     group: "lane-stocks",  lane: "stocks"  },
  { id: "lane-crypto",  title: "Crypto",        sub: "Crypto Momentum v3",       x: 510, y: 620, w: 280, h: 100, status: "ok", metric: "—",     group: "lane-crypto",  lane: "crypto"  },
  { id: "lane-options", title: "Options",       sub: "SPY Wheel v3",             x: 960, y: 620, w: 280, h: 100, status: "ok", metric: "—",     group: "lane-options", lane: "options" },
];
const TOPO_NODES = (typeof window !== "undefined" && window.TOPOLOGY_NODES)
  ? window.TOPOLOGY_NODES
  : _TOPO_NODES_BASELINE;

// Edges: each has explicit anchor points + a kind that styles it.
//   kind: "flow" | "research" | "primary" | "warn" | "fail" | "dim" | "halt"
const TOPO_EDGES = [
  // Research → Risk (proposals — research style, dashed thin)
  { id: "e-research-risk",  from: "research",  fromAnchor: [190, 160], to: "risk",     toAnchor: [330, 220], kind: "research", label: "proposals" },
  // Scheduler → Risk (jobs — active flow)
  { id: "e-sched-risk",     from: "scheduler", fromAnchor: [650, 160], to: "risk",     toAnchor: [630, 220], kind: "flow",     label: "jobs" },
  // Ledger ↔ Risk (bidirectional)
  { id: "e-ledger-risk",    from: "ledger",    fromAnchor: [1080, 160], to: "risk",    toAnchor: [950, 220], kind: "flow",     label: "seq" },
  { id: "e-risk-ledger",    from: "risk",      fromAnchor: [990, 220], to: "ledger",   toAnchor: [1140, 160], kind: "flow",     label: "write" },
  // Risk → Execution (primary flow)
  { id: "e-risk-exec",      from: "risk",      fromAnchor: [630, 350], to: "execution",toAnchor: [630, 400], kind: "primary",  label: "approved" },
  // Execution → Broker
  { id: "e-exec-broker",    from: "execution", fromAnchor: [630, 480], to: "broker",   toAnchor: [630, 510], kind: "primary",  label: "orders" },
  // Broker → 3 lanes
  { id: "e-broker-stocks",  from: "broker",    fromAnchor: [530, 580], to: "lane-stocks",  toAnchor: [200, 620], kind: "flow", label: "fills · stocks",  lane: "stocks" },
  { id: "e-broker-crypto",  from: "broker",    fromAnchor: [630, 580], to: "lane-crypto",  toAnchor: [650, 620], kind: "flow", label: "fills · crypto",  lane: "crypto" },
  { id: "e-broker-options", from: "broker",    fromAnchor: [730, 580], to: "lane-options", toAnchor: [1100, 620], kind: "dim",  label: "(off)",           lane: "options" },
  // Reconciliation feedback: lanes → ledger (right-side curve)
  { id: "e-recon",          from: "lane-options", fromAnchor: [1240, 670], to: "ledger", toAnchor: [1240, 110], kind: "dim", label: "reconciliation", curve: "right" },
];

function pathFor(edge) {
  const [fx, fy] = edge.fromAnchor;
  const [tx, ty] = edge.toAnchor;
  if (edge.curve === "right") {
    // far-right curving up
    return `M ${fx} ${fy} C ${fx + 30} ${fy}, ${tx + 30} ${ty + 20}, ${tx} ${ty}`;
  }
  // Default: smooth vertical-leading bezier
  const dy = ty - fy;
  const c1y = fy + Math.min(80, Math.abs(dy) * 0.55);
  const c2y = ty - Math.min(80, Math.abs(dy) * 0.55);
  return `M ${fx} ${fy} C ${fx} ${c1y}, ${tx} ${c2y}, ${tx} ${ty}`;
}

function laneColor(lane) {
  return lane === "stocks"  ? "var(--lane-stocks)" :
         lane === "crypto"  ? "var(--lane-crypto)" :
         lane === "options" ? "var(--lane-options)" :
                              "var(--text-faint)";
}
function statusColor(s) {
  return s === "ok"   ? "var(--success)" :
         s === "warn" ? "var(--warn)"    :
         s === "fail" ? "var(--danger)"  :
         s === "halt" ? "var(--halt)"    :
                        "var(--text-faint)";
}

function Topology({ selected, onSelect, halted }) {
  const [hover, setHover] = useState(null);

  // edges incident to hover/selected (for highlight)
  const focus = hover || selected;
  const incident = useMemo(() => new Set(
    TOPO_EDGES.filter(e => e.from === focus || e.to === focus).map(e => e.id)
  ), [focus]);

  return (
    <svg viewBox="0 0 1300 740" className="map-svg" preserveAspectRatio="xMidYMid meet">
      <defs>
        {/* Arrowheads in each kind */}
        {["flow", "primary", "research", "warn", "fail", "dim", "halt"].map(k => (
          <marker key={k} id={`ar-${k}`} viewBox="0 -5 10 10" refX="9" refY="0"
            markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0,-4 L9,0 L0,4 z"
              fill={
                k === "flow"     ? "var(--accent)"     :
                k === "primary"  ? "var(--accent)"     :
                k === "research" ? "var(--text-dim)"   :
                k === "warn"     ? "var(--warn)"       :
                k === "fail"     ? "var(--danger)"     :
                k === "halt"     ? "var(--halt)"       :
                                   "var(--text-faint)"
              } />
          </marker>
        ))}
        {/* Subtle grid pattern handled in CSS via background-image */}
      </defs>

      {/* EDGES */}
      <g>
        {TOPO_EDGES.map(e => {
          const isFocus = incident.has(e.id);
          const kindCls = halted ? "halt" : e.kind;
          let stroke = "var(--line)";
          if (kindCls === "flow")     stroke = "var(--accent)";
          if (kindCls === "primary")  stroke = "var(--accent)";
          if (kindCls === "research") stroke = "var(--text-dim)";
          if (kindCls === "warn")     stroke = "var(--warn)";
          if (kindCls === "fail")     stroke = "var(--danger)";
          if (kindCls === "halt")     stroke = "var(--halt)";
          if (kindCls === "dim")      stroke = "var(--line-faint)";

          // override broker→lane edges by lane color
          if (e.lane) {
            stroke = laneColor(e.lane);
          }

          const isFlow = (kindCls === "flow" || kindCls === "primary") && !halted;
          const isResearch = kindCls === "research";
          const opacity = focus ? (isFocus ? 1 : 0.20) : 0.85;

          const d = pathFor(e);

          // Label midpoint
          const [fx, fy] = e.fromAnchor;
          const [tx, ty] = e.toAnchor;
          const mx = (fx + tx) / 2;
          const my = (fy + ty) / 2;

          return (
            <g key={e.id} opacity={opacity} style={{ transition: "opacity 200ms" }}>
              <path
                d={d}
                fill="none"
                stroke={stroke}
                strokeWidth={kindCls === "primary" ? 1.6 : 1.2}
                strokeDasharray={isResearch ? "2 6" : (isFlow ? "5 7" : (kindCls === "halt" ? "8 6" : "none"))}
                strokeLinecap="round"
                style={isFlow ? { animation: "dashmarch 1.6s linear infinite" } : {}}
                markerEnd={`url(#ar-${kindCls})`}
              />
              {e.label && (
                <text x={mx} y={my - 6}
                  className="edge-label"
                  textAnchor="middle"
                  style={{
                    paintOrder: "stroke",
                    stroke: "var(--bg)",
                    strokeWidth: 4,
                  }}>
                  {e.label}
                </text>
              )}
            </g>
          );
        })}
      </g>

      {/* NODES */}
      <g>
        {TOPO_NODES.map(n => (
          <TopoNode key={n.id} node={n}
            hover={hover === n.id}
            selected={selected === n.id}
            anyFocus={Boolean(focus)}
            onEnter={() => setHover(n.id)}
            onLeave={() => setHover(null)}
            onClick={() => onSelect(n.id === selected ? null : n.id)} />
        ))}
      </g>
    </svg>
  );
}

function TopoNode({ node, hover, selected, anyFocus, onEnter, onLeave, onClick }) {
  const dim = anyFocus && !(hover || selected);
  const isLane = node.id.startsWith("lane-");
  const accent = isLane ? laneColor(node.lane) : "var(--accent)";
  const statusFill = statusColor(node.status);

  const rectFill = selected ? "var(--panel-hi)" : (hover ? "var(--panel-hi)" : "var(--panel-2)");
  const rectStroke = selected ? accent : (hover ? "var(--line-strong)" : "var(--line)");
  const titleFill = selected ? accent : "var(--text)";

  return (
    <g transform={`translate(${node.x}, ${node.y})`}
      onMouseEnter={onEnter} onMouseLeave={onLeave}
      onClick={onClick}
      style={{ cursor: "pointer", opacity: dim ? 0.4 : 1, transition: "opacity 200ms" }}>
      {/* card */}
      <rect width={node.w} height={node.h} rx="6" ry="6"
        fill={rectFill} stroke={rectStroke} strokeWidth="1" />
      {/* left accent edge for lanes */}
      {isLane && (
        <rect x="0" y="0" width="3" height={node.h} fill={accent} />
      )}
      {/* primary node — subtle inner edge */}
      {node.primary && (
        <rect x="0.5" y="0.5" width={node.w - 1} height={node.h - 1} rx="6" ry="6"
          fill="none" stroke="var(--accent-dim)" strokeOpacity="0.5" strokeWidth="1" />
      )}

      {/* status dot */}
      <circle cx={node.w - 18} cy="18" r="5" fill={statusFill}>
        {node.status === "ok" && (
          <animate attributeName="opacity" values="1;0.5;1" dur="2.4s" repeatCount="indefinite" />
        )}
        {node.status === "fail" && (
          <animate attributeName="r" values="5;6.5;5" dur="0.8s" repeatCount="indefinite" />
        )}
      </circle>
      {/* faint halo for failing node */}
      {node.status === "fail" && (
        <circle cx={node.w - 18} cy="18" r="8" fill="none" stroke={statusFill} strokeOpacity="0.4">
          <animate attributeName="r" values="8;14;8" dur="1.4s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0.4;0;0.4" dur="1.4s" repeatCount="indefinite" />
        </circle>
      )}

      {/* labels */}
      <text x={isLane ? 18 : 16} y="28" className="node-title" fill={titleFill}
        style={{ fontFamily: "Instrument Sans, sans-serif", fontWeight: 600, fontSize: 14 }}>
        {node.title}
      </text>
      <text x={isLane ? 18 : 16} y="46" className="node-sub"
        style={{ fontFamily: "JetBrains Mono, monospace", fontSize: 11, fill: "var(--text-dim)" }}>
        {node.sub}
      </text>
      <text x={isLane ? 18 : 16} y={node.h - 14}
        style={{ fontFamily: "JetBrains Mono, monospace", fontSize: 13, fill: "var(--text)", fontWeight: 500 }}>
        {node.metric}
      </text>

      {/* corner brackets — subtle */}
      <path d={`M 0 8 L 0 0 L 8 0`} stroke={accent} strokeWidth="1.2" fill="none" opacity={selected ? 1 : 0.3} />
      <path d={`M ${node.w - 8} 0 L ${node.w} 0 L ${node.w} 8`} stroke={accent} strokeWidth="1.2" fill="none" opacity={selected ? 1 : 0.3} />
      <path d={`M 0 ${node.h - 8} L 0 ${node.h} L 8 ${node.h}`} stroke={accent} strokeWidth="1.2" fill="none" opacity={selected ? 1 : 0.3} />
      <path d={`M ${node.w - 8} ${node.h} L ${node.w} ${node.h} L ${node.w} ${node.h - 8}`} stroke={accent} strokeWidth="1.2" fill="none" opacity={selected ? 1 : 0.3} />
    </g>
  );
}

// ---- Node detail data — what to show on the side when a node is selected ----
function nodeDetail(id) {
  switch (id) {
    case "research": return {
      eyebrow: "node · research",
      title: "Research Factory",
      meta: "mutator · judge (Opus) · reviewer (Sonnet)",
      blocks: [
        { kind: "spend", title: "Spend today", value: `$${LLM_SPEND.today_total.toFixed(2)}`, sub: `of $20.00 daily cap` },
        { kind: "queue", title: "Runs", value: "3", sub: "1 mutate · 2 judge passes · ETA ~14m" },
        { kind: "list",  title: "Recent decisions", items: MUTATIONS.slice(0, 4) },
      ]
    };
    case "scheduler": return {
      eyebrow: "node · scheduler",
      title: "Scheduler",
      meta: "APScheduler · 10 jobs · 1 failing",
      blocks: [
        { kind: "stat",  title: "Failing", value: "1", sub: "alpaca/heartbeat — TLS timeout", state: "fail" },
        { kind: "jobs", limit: 6 }
      ]
    };
    case "ledger": return {
      eyebrow: "node · ledger",
      title: "Ledger",
      meta: "append-only · hash-chain verified",
      blocks: [
        { kind: "stat",  title: "Last seq", value: LEDGER_HEALTH.last_seq.toLocaleString(), sub: "hash " + LEDGER_HEALTH.last_hash },
        { kind: "chain" },
        { kind: "kv", rows: LEDGER_HEALTH.tables },
      ]
    };
    case "risk": return {
      eyebrow: "node · risk kernel",
      title: "Risk Kernel",
      meta: "deterministic · hash-locked at boot",
      blocks: [
        { kind: "stat",  title: "Throughput", value: "412/h", sub: "evaluated decisions" },
        { kind: "caps" },
        { kind: "regime" },
      ]
    };
    case "execution": return {
      eyebrow: "node · execution",
      title: "Execution",
      meta: "idempotent order router · 1 stuck",
      blocks: [
        { kind: "stat",  title: "Open orders", value: String(OPEN_ORDERS.filter(o => !o.canceled).length), sub: "1 stuck, 1 canceled today" },
        { kind: "orders" }
      ]
    };
    case "broker": return {
      eyebrow: "node · broker",
      title: "Alpaca Paper",
      meta: "paper-trading · real equity tracking",
      blocks: [
        { kind: "stat",  title: "Heartbeat", value: "FAIL", sub: "TLS handshake timeout (8s)", state: "fail" },
        { kind: "stat",  title: "Last fill", value: "13:02:11", sub: "SPY 28 @ 521.10" },
      ]
    };
    case "lane-stocks": return {
      eyebrow: "lane · stocks",
      title: "ETF Momentum",
      meta: "armed · 4 positions · exposure 41.2 %",
      blocks: [
        { kind: "stat",  title: "Day P&L", value: "+ $118.88", sub: "across 4 positions", state: "up" },
        { kind: "positions", lane: "stocks" },
        { kind: "warning", text: "1 unknown position — AAPL × 50 — classify or close" }
      ]
    };
    case "lane-crypto": return {
      eyebrow: "lane · crypto",
      title: "BTC Trend",
      meta: "armed · 2 positions · AT CAP 15.0 %",
      blocks: [
        { kind: "stat",  title: "Day P&L", value: "+ $4.15", sub: "across 2 positions", state: "up" },
        { kind: "positions", lane: "crypto" },
        { kind: "warning", text: "Lane at exposure cap — new entries skipped" }
      ]
    };
    case "lane-options": return {
      eyebrow: "lane · options",
      title: "Wheel",
      meta: "scaffolded · not active",
      blocks: [
        { kind: "stat", title: "Status", value: "off", sub: "WHEEL_SPY_v0 in research_only" },
        { kind: "text", text: "Strategy registered but no live deployment. Promotion requires Tier-3 validation + typed approval." }
      ]
    };
    default: return null;
  }
}

Object.assign(window, { Topology, nodeDetail, TOPO_NODES, TOPO_EDGES, laneColor, statusColor });
