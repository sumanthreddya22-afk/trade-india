// ============================================================
// components.jsx — small primitives used across surfaces
// ============================================================

const { useState, useEffect, useRef, useMemo } = React;

// ---- formatting helpers ----
function fmtMoney(n, opts = {}) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 && opts.sign ? "+" : "";
  return sign + n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(n, digits = 2, sign = false) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const s = (n * 100).toFixed(digits);
  return (sign && n > 0 ? "+" : "") + s + "%";
}
function fmtNum(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtAge(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h + "h " + m + "m";
}
function fmtCountdown(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60).toString().padStart(2, "0") + "s";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h + "h " + m.toString().padStart(2, "0") + "m";
}

// ---- StatusPill ----
function StatusPill({ state }) {
  const label = state;
  return (
    <span className={`status-pill ${state}`}>
      <span className="sp-dot"></span>
      <span>{label}</span>
    </span>
  );
}

// ---- ClassificationTag ----
function ClassificationTag({ value }) {
  return <span className={`classify ${value}`}>{value}</span>;
}

// ---- LedgerSeqChip ----
function LedgerSeqChip({ seq }) {
  return <span className="ledger-chip" title={`ledger seq ${seq}`}>seq {seq}</span>;
}

// ---- TierBadge ----
function TierBadge({ tier }) {
  return <span className={`tier-badge t${tier}`}>T{tier}</span>;
}

// ---- HashVerifiedCheck ----
function HashVerifiedCheck({ ts, status = "verified", label = "chain verified" }) {
  if (status === "verified") {
    return (
      <span className="hash-check">
        <Icon name="shield-check" />
        <span>{label} {ts}</span>
      </span>
    );
  }
  return (
    <span className="hash-check mismatch">
      <Icon name="shield-alert" />
      <span>HASH MISMATCH</span>
    </span>
  );
}

// ---- LensToggle ----
function LensToggle({ value, onChange }) {
  const opts = [
    { k: "raw", l: "raw" },
    { k: "broker_paper", l: "broker" },
    { k: "pessimistic", l: "pess." },
  ];
  return (
    <span className="lens-toggle" title="Cost lens — pessimistic is the default per policy">
      {opts.map(o => (
        <button key={o.k} className={value === o.k ? "is-active" : ""} onClick={() => onChange(o.k)}>{o.l}</button>
      ))}
    </span>
  );
}

// ---- RiskCapBar ----
function RiskCapBar({ name, used, cap, unit }) {
  const ratio = cap > 0 ? used / cap : 0;
  const cls = ratio >= 1 ? "is-cap" : ratio >= 0.8 ? "is-warn" : "";
  const fmt = (v) => unit === "#" ? v.toString() : fmtPct(v, used < 1 ? 1 : 0);
  return (
    <div className="cap-row">
      <span className="cap-name">{name}</span>
      <span className="cap-val mono">{fmt(used)} <span className="dim">/ {fmt(cap)}</span></span>
      <div className={`cap-bar ${cls}`}>
        <i style={{ width: Math.min(100, ratio * 100) + "%" }}></i>
        <span className="cap-mark" style={{ left: "100%" }}></span>
      </div>
    </div>
  );
}

// ---- Panel ----
function Panel({ title, sub, actions, children, className = "", flush, bodyClass = "" }) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <header className="panel-header">
          <span>{title}</span>
          {sub && <span className="ph-sub">{sub}</span>}
          <span className="ph-spacer" />
          {actions && <span className="ph-actions">{actions}</span>}
        </header>
      )}
      <div className={`panel-body ${flush ? "flush" : ""} ${bodyClass}`}>
        {children}
      </div>
    </section>
  );
}

// ---- Provenance ----
function Prov({ children }) {
  return <span className="prov">{children}</span>;
}

// ---- Icon (lucide line set, hand-rolled) ----
function Icon({ name, size = 14 }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.75, strokeLinecap: "round", strokeLinejoin: "round" };
  switch (name) {
    case "shield-check":
      return <svg {...common}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>;
    case "shield-alert":
      return <svg {...common}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>;
    case "alert":
      return <svg {...common}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>;
    case "check":
      return <svg {...common}><polyline points="20 6 9 17 4 12"/></svg>;
    case "x":
      return <svg {...common}><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>;
    case "chevron-down":
      return <svg {...common}><polyline points="6 9 12 15 18 9"/></svg>;
    case "chevron-right":
      return <svg {...common}><polyline points="9 6 15 12 9 18"/></svg>;
    case "chevron-up":
      return <svg {...common}><polyline points="18 15 12 9 6 15"/></svg>;
    case "up":
      return <svg {...common}><polyline points="6 14 12 8 18 14"/></svg>;
    case "down":
      return <svg {...common}><polyline points="6 10 12 16 18 10"/></svg>;
    case "flat":
      return <svg {...common}><line x1="5" y1="12" x2="19" y2="12"/></svg>;
    case "halt":
      return <svg {...common}><rect x="6" y="6" width="12" height="12" rx="2"/></svg>;
    case "play":
      return <svg {...common}><polygon points="6 4 20 12 6 20 6 4"/></svg>;
    case "pause":
      return <svg {...common}><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg>;
    case "info":
      return <svg {...common}><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>;
    case "search":
      return <svg {...common}><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>;
    case "refresh":
      return <svg {...common}><polyline points="23 4 23 10 17 10"/><path d="M3.51 15a9 9 0 0 0 14.85 3.36L23 14"/><polyline points="1 20 1 14 7 14"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10"/></svg>;
    case "clock":
      return <svg {...common}><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>;
    case "hash":
      return <svg {...common}><line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/></svg>;
    case "boxes":
      return <svg {...common}><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4"/><path d="M21 7v10l-9 4"/></svg>;
    case "activity":
      return <svg {...common}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>;
    case "beaker":
      return <svg {...common}><path d="M9 3v6L4 18a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-5-9V3"/><line x1="8" y1="3" x2="16" y2="3"/></svg>;
    case "cog":
      return <svg {...common}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.65 1.65 0 0 0 15 19.4a1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.36.16.66.42.88.74.22.32.34.7.34 1.09 0 .39-.12.77-.34 1.09-.22.32-.52.58-.88.74z"/></svg>;
    case "external":
      return <svg {...common}><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>;
    case "drag":
      return <svg {...common}><circle cx="9" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="18" r="1"/></svg>;
    case "filter":
      return <svg {...common}><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>;
    default:
      return null;
  }
}

// ---- Sparkline ----
function Sparkline({ values, height = 24, color = "var(--info)", over = false, fill = false }) {
  if (!values || values.length === 0) return null;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const range = max - min || 1;
  const w = 100; const h = height;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 4) - 2;
    return [x, y];
  });
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ");
  const dFill = d + ` L ${w} ${h} L 0 ${h} Z`;
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ height }}>
      {fill && <path d={dFill} fill={color} opacity="0.15" />}
      <path d={d} stroke={over ? "var(--warn)" : color} strokeWidth="1.2" fill="none" />
    </svg>
  );
}

// ---- Equity chart ----
function EquityChart({ data }) {
  if (!data || !data.points || data.points.length < 2) return null;
  const w = 1000, h = 200, pad = { l: 0, r: 0, t: 18, b: 18 };
  const eq = data.points.map(p => p.equity);
  const min = Math.min(...eq), max = Math.max(...eq);
  const range = max - min || 1;
  const pts = data.points.map((p, i) => {
    const x = (i / (data.points.length - 1)) * (w - pad.l - pad.r) + pad.l;
    const y = h - pad.b - ((p.equity - min) / range) * (h - pad.t - pad.b);
    return [x, y];
  });
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ");
  const dFill = d + ` L ${pts[pts.length - 1][0]} ${h - pad.b} L ${pts[0][0]} ${h - pad.b} Z`;
  return (
    <div className="equity-chart">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--info)" stopOpacity="0.30" />
            <stop offset="100%" stopColor="var(--info)" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {/* gridlines */}
        {[0.25, 0.5, 0.75].map(g => (
          <line key={g} x1="0" x2={w} y1={pad.t + g * (h - pad.t - pad.b)} y2={pad.t + g * (h - pad.t - pad.b)} stroke="var(--border)" strokeDasharray="2 4" />
        ))}
        <path d={dFill} fill="url(#eqfill)" />
        <path d={d} stroke="var(--info)" strokeWidth="1.4" fill="none" />
        {/* markers as vertical lines */}
        {data.markers.map(m => {
          const [x] = pts[m.i] || [0, 0];
          return (
            <g key={m.i}>
              <line x1={x} x2={x} y1={pad.t} y2={h - pad.b}
                stroke={m.kind === "halt" ? "var(--halt)" : m.kind === "profile" ? "var(--warn)" : "var(--info)"}
                strokeDasharray="3 3" opacity="0.7" />
              <circle cx={x} cy={pad.t} r="3"
                fill={m.kind === "halt" ? "var(--halt)" : m.kind === "profile" ? "var(--warn)" : "var(--info)"} />
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ---- Donut ----
function Donut({ data, size = 130 }) {
  const total = data.reduce((a, b) => a + b.value, 0) || 1;
  const r = size / 2 - 14;
  const c = 2 * Math.PI * r;
  let off = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--bg-2)" strokeWidth="14"/>
      {data.map((d, i) => {
        const len = (d.value / total) * c;
        const seg = (
          <circle key={i} cx={size / 2} cy={size / 2} r={r}
            fill="none" stroke={d.color} strokeWidth="14"
            strokeDasharray={`${len} ${c}`}
            strokeDashoffset={-off}
            transform={`rotate(-90 ${size/2} ${size/2})`} />
        );
        off += len;
        return seg;
      })}
      <text x="50%" y="50%" textAnchor="middle" dy="0.35em"
        fontFamily="IBM Plex Mono, monospace" fontSize="14"
        fill="var(--text)">100%</text>
    </svg>
  );
}

Object.assign(window, {
  fmtMoney, fmtPct, fmtNum, fmtAge, fmtCountdown,
  StatusPill, ClassificationTag, LedgerSeqChip, TierBadge, HashVerifiedCheck,
  LensToggle, RiskCapBar, Panel, Prov, Icon, Sparkline, EquityChart, Donut,
});
