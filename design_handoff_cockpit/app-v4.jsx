// ============================================================
// app-v4.jsx — v1's dense panels + topology section below
// ============================================================

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "systemState": "running",
  "theme": "dark",
  "density": "compact",
  "showProvenance": true,
  "costLens": "pessimistic"
}/*EDITMODE-END*/;

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  const [surface, setSurface] = useState("right_now");
  const [showHalt, setShowHalt] = useState(false);
  const [showResume, setShowResume] = useState(false);
  const [activity, setActivity] = useState(SEED_ACTIVITY);
  const [equityRange, setEquityRange] = useState("3m");
  const [clock, setClock] = useState(nowClock());
  const [showHelp, setShowHelp] = useState(false);
  const [haltSrc, setHaltSrc] = useState(null);
  const [haltMeta, setHaltMeta] = useState(null);

  useEffect(() => {
    document.documentElement.dataset.theme = t.theme;
    document.documentElement.dataset.density = t.density;
    document.documentElement.dataset.prov = t.showProvenance ? "on" : "off";
  }, [t.theme, t.density, t.showProvenance]);

  const systemState = haltSrc === "manual" ? "halted" : t.systemState;

  useEffect(() => {
    if (t.systemState === "halted" && haltSrc !== "manual") {
      setHaltSrc("tweak");
      setHaltMeta({
        active: true,
        reason: "manual_operator_halt",
        since: "13:02:30Z",
        operator: "operator",
        profile_before: STATUS_BASE.risk_profile
      });
    } else if (t.systemState !== "halted" && haltSrc === "tweak") {
      setHaltSrc(null);
      setHaltMeta(null);
    }
  }, [t.systemState]);

  useEffect(() => {
    const id = setInterval(() => setClock(nowClock()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (systemState === "halted" || systemState === "down") return;
    const id = setInterval(() => {
      setActivity(prev => {
        const next = synthEvent(prev[0]?.seq ?? 28411);
        return [next, ...prev].slice(0, 200);
      });
    }, 4200);
    return () => clearInterval(id);
  }, [systemState]);

  useEffect(() => {
    let prefix = null;
    let prefixTimer = null;
    const onKey = (e) => {
      if (e.ctrlKey && e.key === ".") { e.preventDefault(); openHalt(); return; }
      if (e.key === "Escape") { setShowHalt(false); setShowResume(false); setShowHelp(false); return; }
      if (e.key === "?" && !e.metaKey) { e.preventDefault(); setShowHelp(s => !s); return; }
      if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (prefix === "g") {
        if (e.key === "r") setSurface("right_now");
        else if (e.key === "a") setSurface("activity");
        else if (e.key === "l") setSurface("lab");
        else if (e.key === "s") setSurface("system");
        prefix = null;
        return;
      }
      if (e.key === "g") {
        prefix = "g";
        clearTimeout(prefixTimer);
        prefixTimer = setTimeout(() => { prefix = null; }, 900);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [systemState]);

  const openHalt = () => {
    if (systemState === "halted") setShowResume(true);
    else setShowHalt(true);
  };
  const confirmHalt = (reason) => {
    setHaltSrc("manual");
    setHaltMeta({ active: true, reason, since: clock + "Z", operator: "operator", profile_before: STATUS_BASE.risk_profile });
    setShowHalt(false);
    setTweak("systemState", "halted");
    setActivity(prev => [{ ts: clock, seq: (prev[0]?.seq ?? 28411) + 1, type: "halt", lane: null, msg: `HALT manual_operator_halt — ${reason}` }, ...prev]);
  };
  const confirmResume = () => {
    setHaltSrc(null); setHaltMeta(null); setShowResume(false);
    setTweak("systemState", "running");
    setActivity(prev => [{ ts: clock, seq: (prev[0]?.seq ?? 28411) + 1, type: "policy", lane: null, msg: `resume — by operator` }, ...prev]);
  };

  const status = {
    ...STATUS_BASE,
    system_state: systemState,
    halted: haltMeta || STATUS_BASE.halted
  };

  return (
    <>
      <TopBar status={status} clock={clock}
        onKill={openHalt} onResume={openHalt} onProfileToggle={() => {}} />
      <HaltBanner halted={haltMeta} />

      <nav className="tabs" role="tablist">
        <Tab id="right_now" label="Right Now" kbd="g r" active={surface}
          icon={<Icon name="activity" size={12}/>} onSelect={setSurface}
          badge={ACTION_REQUIRED.filter(a => a.severity === "high").length} badgeDanger />
        <Tab id="activity" label="Recent Activity" kbd="g a" active={surface}
          icon={<Icon name="clock" size={12}/>} onSelect={setSurface} />
        <Tab id="lab" label="Strategy Lab" kbd="g l" active={surface}
          icon={<Icon name="beaker" size={12}/>} onSelect={setSurface} research />
        <Tab id="system" label="System Health" kbd="g s" active={surface}
          icon={<Icon name="cog" size={12}/>} onSelect={setSurface}
          badge={JOBS.filter(j => j.status === "fail").length} />
        <span className="spacer"></span>
        <span className="tab" style={{ borderBottom: "none", cursor: "default" }}>
          <span className="text-mini dim">autonomy</span>
          <span className="mono" style={{ color: "var(--text)" }}>L2</span>
          <span className="text-mini dim">paper · Alpaca</span>
        </span>
      </nav>

      {surface === "right_now" && (
        <>
          <SurfaceRightNow snap={{ account: status.account, lanes: status.lanes }}
            activity={activity}
            equityRange={equityRange} setEquityRange={setEquityRange}
            lens={t.costLens} setLens={(v) => setTweak("costLens", v)}
            degraded={systemState === "degraded"} />
          <main className="surface" style={{ paddingTop: 0 }}>
            <MapSection halted={systemState === "halted"} />
          </main>
        </>
      )}
      {surface === "activity" && <SurfaceRecentActivity />}
      {surface === "lab" && <SurfaceStrategyLab />}
      {surface === "system" && <SurfaceSystemHealth lens={t.costLens} setLens={(v) => setTweak("costLens", v)} />}

      {showHalt && <HaltModal onClose={() => setShowHalt(false)} onConfirm={confirmHalt} />}
      {showResume && haltMeta && (
        <ResumeModal halted={haltMeta} onClose={() => setShowResume(false)} onConfirm={confirmResume} />
      )}
      {showHelp && <KeyboardHelp onClose={() => setShowHelp(false)} />}

      <TweaksPanel>
        <TweakSection label="System" />
        <TweakRadio label="State" value={t.systemState}
          options={["running","degraded","halted","down"]}
          onChange={(v) => setTweak("systemState", v)} />
        <TweakSelect label="Cost lens" value={t.costLens}
          options={[
            { value: "raw", label: "raw (broker quote)" },
            { value: "broker_paper", label: "broker paper" },
            { value: "pessimistic", label: "pessimistic (default)" },
          ]}
          onChange={(v) => setTweak("costLens", v)} />

        <TweakSection label="Display" />
        <TweakRadio label="Theme" value={t.theme}
          options={["dark","light"]} onChange={(v) => setTweak("theme", v)} />
        <TweakRadio label="Density" value={t.density}
          options={["compact","comfortable"]} onChange={(v) => setTweak("density", v)} />
        <TweakToggle label="Show provenance"
          value={t.showProvenance}
          onChange={(v) => setTweak("showProvenance", v)} />

        <TweakSection label="Actions" />
        <TweakButton label="Press kill switch (Ctrl + .)" onClick={openHalt} />
        <TweakButton label="Show keyboard shortcuts (?)" onClick={() => setShowHelp(true)} />
      </TweaksPanel>
    </>
  );
}

// ============================================================
// Map section — topology placed below the dense panels
// ============================================================
function MapSection({ halted }) {
  const [selected, setSelected] = useState(null);
  return (
    <section>
      <div className="map-section-head">
        <span className="label">System Map</span>
        <span className="title">data flow — research → risk kernel → execution → broker → lanes</span>
        <span className="rule"></span>
        <span className="text-mini dim mono">click any node to drill in</span>
      </div>

      <div className="map-canvas">
        <div className="map-canvas-head">
          <div>
            <div className="title">The kernel, right now</div>
            <div className="subtitle">9 nodes · 10 edges · live · L2 autonomy</div>
          </div>
          <div className="legend">
            <span className="sw"><i style={{ background: "var(--success)" }}></i> healthy</span>
            <span className="sw"><i style={{ background: "var(--warn)" }}></i> warn</span>
            <span className="sw"><i style={{ background: "var(--danger)" }}></i> failing</span>
            <span className="sw"><i style={{ background: "var(--text-faint)" }}></i> off</span>
          </div>
        </div>
        <div className="map-canvas-body">
          <div className="map-svg-wrap">
            <Topology selected={selected} onSelect={setSelected} halted={halted} />
          </div>
          <div className="map-detail">
            <MapDetail selected={selected} onClear={() => setSelected(null)} />
          </div>
        </div>
      </div>
    </section>
  );
}

function MapDetail({ selected, onClear }) {
  if (!selected) {
    return (
      <div>
        <div className="md-eyebrow">Map · idle</div>
        <div className="md-title">Pick a node</div>
        <div className="md-meta">Hover a node to highlight its data flows · click to see its internals</div>
        <div className="md-empty" style={{ marginTop: 28 }}>
          <Icon name="boxes" size={28}/>
          <div style={{ marginTop: 8 }}>9 nodes in the kernel</div>
          <div style={{ marginTop: 4 }} className="dim">
            Research, Scheduler, Ledger, Risk Kernel,<br/>
            Execution, Broker, and 3 lanes
          </div>
        </div>
      </div>
    );
  }
  const detail = nodeDetail(selected);
  if (!detail) return null;
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="md-eyebrow">{detail.eyebrow}</div>
          <div className="md-title">{detail.title}</div>
          <div className="md-meta">{detail.meta}</div>
        </div>
        <button className="btn ghost sm" onClick={onClear}><Icon name="x" size={11}/></button>
      </div>
      {detail.blocks.slice(0, 4).map((b, i) => <MapDetailBlock key={i} block={b} />)}
      <Prov>provenance: live node · click another or × to clear</Prov>
    </div>
  );
}

function MapDetailBlock({ block }) {
  switch (block.kind) {
    case "stat":
      return (
        <div className="md-stat">
          <div className="lbl">{block.title}</div>
          <div className={`val ${block.state === "fail" ? "fail" : block.state === "up" ? "up" : ""}`}>{block.value}</div>
          {block.sub && <div className="sub">{block.sub}</div>}
        </div>
      );
    case "warning":
      return (
        <div className="ar-card high" style={{ marginTop: 12 }}>
          <div className="ar-title"><Icon name="alert" size={12}/> {block.text}</div>
        </div>
      );
    case "text":
      return <div className="text-mini" style={{ marginTop: 10, color: "var(--text-dim)", lineHeight: 1.5 }}>{block.text}</div>;
    case "kv":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Table sizes</div></div>
          <div className="kv-list">
            {block.rows.slice(0, 5).map(r => (
              <div className="kv" key={r.name}>
                <span className="k mono" style={{ fontSize: 11 }}>{r.name}</span>
                <span className="v">{r.rows.toLocaleString()}</span>
              </div>
            ))}
          </div>
        </div>
      );
    case "list":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">{block.title}</div></div>
          <div className="kv-list">
            {block.items.slice(0, 4).map((m, i) => (
              <div className="kv" key={i}>
                <span className="k mono" style={{ fontSize: 11 }}>{m.param}</span>
                <span className="v"><span className={`mut-tag ${m.tag}`}>{m.tag}</span></span>
              </div>
            ))}
          </div>
        </div>
      );
    case "jobs":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Jobs</div></div>
          <div className="kv-list">
            {JOBS.slice(0, 4).map(j => (
              <div className="kv" key={j.name}>
                <span className="k mono" style={{ fontSize: 11 }}>{j.name}</span>
                <span className="v"><span className={`stat-pill ${j.status}`}>{j.status}</span></span>
              </div>
            ))}
          </div>
        </div>
      );
    case "orders":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Open orders</div></div>
          <div className="kv-list">
            {OPEN_ORDERS.filter(o => !o.canceled).map(o => (
              <div className="kv" key={o.idempotency}>
                <span className="k mono" style={{ fontSize: 11 }}>{o.symbol} · {o.side} {o.qty}</span>
                <span className="v">{o.stuck ? <span className="warn">stuck · {o.age_s}s</span> : <span className="dim">{o.status}</span>}</span>
              </div>
            ))}
          </div>
        </div>
      );
    case "positions":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Positions</div></div>
          <div className="kv-list">
            {POSITIONS.filter(p => p.lane === block.lane).map(p => {
              const up = p.pl_abs >= 0;
              return (
                <div className="kv" key={p.symbol}>
                  <span className="k mono" style={{ fontSize: 11 }}>
                    {p.symbol} {p.classification === "unknown" && <span style={{ color: "var(--danger)", marginLeft: 4 }}>● unknown</span>}
                  </span>
                  <span className="v">
                    <span className={up ? "up" : "down"}>
                      {(up ? "+" : "") + fmtNum(p.pl_abs, 2)} · {fmtPct(p.pl_pct, 2, true)}
                    </span>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      );
    case "chain":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Last 60 blocks</div></div>
          <div className="chain-strip">
            {LEDGER_HEALTH.blocks.map((b, i) => (
              <div key={i} className={`chain-block ${i === LEDGER_HEALTH.blocks.length - 1 ? "last" : ""} ${b.ok ? "" : "fail"}`} title={`seq ${b.seq}`}/>
            ))}
          </div>
        </div>
      );
    case "caps":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Risk caps</div></div>
          {RISK_CAPS.slice(0, 4).map(c => <RiskCapBar key={c.name} {...c} />)}
        </div>
      );
    case "regime":
      return (
        <div style={{ marginTop: 12 }}>
          <div className="md-stat" style={{ marginTop: 0 }}><div className="lbl">Regime · since {REGIME.since}</div></div>
          <div className="regime"><span className={`r-tag ${REGIME.label}`}>{REGIME.label}</span></div>
        </div>
      );
    default: return null;
  }
}

function Tab({ id, label, kbd, icon, active, onSelect, badge, badgeDanger, research }) {
  return (
    <button className={`tab ${active === id ? "is-active" : ""} ${research ? "is-research" : ""}`}
      onClick={() => onSelect(id)} role="tab" aria-selected={active === id}>
      {icon}
      <span>{label}</span>
      {badge ? <span className={`count ${badgeDanger ? "danger" : ""}`}>{badge}</span> : null}
      <span className="kbd">{kbd}</span>
    </button>
  );
}

function KeyboardHelp({ onClose }) {
  const items = [
    ["g r", "Right Now"], ["g a", "Recent Activity"], ["g l", "Strategy Lab"], ["g s", "System Health"],
    ["Ctrl + .", "Halt / resume"], ["Esc", "Dismiss"], ["?", "Toggle help"],
  ];
  return (
    <div className="kbd-help" onClick={(e) => e.stopPropagation()}>
      <div className="text-mini dim" style={{ gridColumn: "1 / -1", marginBottom: 4 }}>Keyboard shortcuts</div>
      {items.map(([k, d]) => (
        <React.Fragment key={k}>
          <span className="key">{k}</span><span className="desc">{d}</span>
        </React.Fragment>
      ))}
      <div style={{ gridColumn: "1 / -1", marginTop: 6 }}>
        <button className="btn ghost sm" onClick={onClose}>close</button>
      </div>
    </div>
  );
}

function nowClock() {
  const d = new Date();
  return d.toUTCString().split(" ")[4];
}

const SYNTH_EVENTS = [
  { type: "scan",   lane: "stocks",  tpl: () => `scan/stocks: ${300 + Math.floor(Math.random()*200)} considered → ${Math.floor(Math.random()*10)} passed → 0 submitted` },
  { type: "scan",   lane: "crypto",  tpl: () => `scan/crypto: 18 considered → ${Math.floor(Math.random()*3)} passed → 0 submitted (cap reached)` },
  { type: "heart",  lane: null,      tpl: () => `daemon heartbeat ok · pid 41882` },
  { type: "skip",   lane: "stocks",  tpl: () => `skip XLF — drift gauge ${(2.0 + Math.random()*1.6).toFixed(1)}bps cooldown` },
  { type: "skip",   lane: "crypto",  tpl: () => `skip ETH/USD — lane at cap (15.0%)` },
  { type: "heart",  lane: null,      tpl: () => `ledger chain verified · hash ${randHash()}` },
];
function synthEvent(lastSeq) {
  const e = SYNTH_EVENTS[Math.floor(Math.random() * SYNTH_EVENTS.length)];
  return { ts: nowClock(), seq: lastSeq + 1, type: e.type, lane: e.lane, msg: e.tpl() };
}
function randHash() {
  const c = "0123456789abcdef"; let s = "";
  for (let i = 0; i < 4; i++) s += c[Math.floor(Math.random() * 16)];
  return s + "…" + c[Math.floor(Math.random() * 16)] + c[Math.floor(Math.random() * 16)];
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
