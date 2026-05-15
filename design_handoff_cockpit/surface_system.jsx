// ============================================================
// surface_system.jsx — Surface D
// "Is the plumbing OK — jobs, data freshness, reconciliation,
//  costs, hashes?"
// ============================================================

function SurfaceSystemHealth({ lens, setLens }) {
  return (
    <main className="surface">
      <Panel title="System vitals" sub="last 60s · refreshes every 30s"
        actions={<span className="row" style={{ gap: 6 }}><Icon name="clock" size={11}/><span className="text-mini mono">verified 13:02:11Z</span></span>}>
        <SystemVitals />
      </Panel>

      <div className="cols-2" style={{ marginTop: "var(--gap)" }}>
        <div className="col-stack">
          <JobScheduler />
          <DataFreshness />
          <Reconciliation />
          <DriftMonitor />
        </div>
        <div className="col-stack">
          <CostModel lens={lens} setLens={setLens} />
          <PolicyLocks />
          <Personas />
          <HaltHistory />
          <LedgerHealth />
          <DaemonHeartbeat />
        </div>
      </div>
    </main>
  );
}

// ---- Vitals strip ----
function SystemVitals() {
  const fail = JOBS.filter(j => j.status === "fail").length;
  const stale = FRESHNESS.filter(f => !f.ok).length;
  const mismatches = POLICY_LOCKS.filter(l => l.status === "mismatch").length + PERSONAS.filter(p => p.status === "mismatch").length;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: "var(--gap)" }}>
      <Vital label="Jobs failing"     value={fail}        unit={`/ ${JOBS.length}`} state={fail > 0 ? "warn" : "ok"} />
      <Vital label="Data stale"       value={stale}       unit={`/ ${FRESHNESS.length}`} state={stale > 0 ? "warn" : "ok"} />
      <Vital label="Recon mismatches" value={RECON.mismatches} unit={`/ ${RECON.total}`} state={RECON.unresolved > 0 ? "warn" : "ok"} />
      <Vital label="Hash mismatches"  value={mismatches} unit={"/ 17"} state={mismatches > 0 ? "fail" : "ok"} />
      <Vital label="Drift bps"        value={DRIFT.current_bps} unit={`/ ${DRIFT.threshold_bps}`} state={DRIFT.current_bps > DRIFT.threshold_bps ? "warn" : "ok"} digits={1} />
      <Vital label="Ledger seq"       value={LEDGER_HEALTH.last_seq} unit={LEDGER_HEALTH.last_hash} state={"ok"} mono />
    </div>
  );
}

function Vital({ label, value, unit, state, digits = 0, mono }) {
  const stateColor = state === "fail" ? "var(--danger)" : state === "warn" ? "var(--warn)" : "var(--success)";
  return (
    <div className="digest-stat">
      <div className="label">{label}</div>
      <div className="value" style={{ color: stateColor }}>
        {typeof value === "number" ? value.toFixed(digits) : value}
      </div>
      <div className="sub">{unit}</div>
    </div>
  );
}

// ---- Jobs ----
function JobScheduler() {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <Panel title="Job scheduler" sub={`${JOBS.length} jobs · ${JOBS.filter(j=>j.status==="fail").length} failing`} flush>
      <div className="job-row head">
        <span>Name</span>
        <span>Schedule</span>
        <span>Duration</span>
        <span>Next run</span>
        <span>Status</span>
      </div>
      {JOBS.map(j => {
        const next = Math.max(0, j.next_s - tick);
        const failing = j.status === "fail";
        return (
          <div className={`job-row ${failing ? "is-fail" : ""}`} key={j.name}>
            <span className="job-name">{j.name}</span>
            <span className="dim">{j.schedule}</span>
            <span className="dim">{(j.dur_ms / 1000).toFixed(j.dur_ms < 1000 ? 2 : 1)}s</span>
            <span className="dim">
              {failing ? <span className="warn">retry in {next}s</span> : (next === 0 ? "now…" : fmtCountdown(next))}
            </span>
            <span>
              <span className={`stat-pill ${j.status}`}>{j.status}</span>
              {failing && <div className="text-mini" style={{ color: "var(--danger)", marginTop: 2 }}>{j.err}</div>}
            </span>
          </div>
        );
      })}
    </Panel>
  );
}

// ---- Freshness ----
function DataFreshness() {
  return (
    <Panel title="Data freshness" sub={`${FRESHNESS.length} sources · ${FRESHNESS.filter(f=>!f.ok).length} stale`} flush>
      <div className="fresh-row head">
        <span>Source</span>
        <span>Last tick</span>
        <span>Cadence</span>
        <span>Lag</span>
        <span>Status</span>
      </div>
      {FRESHNESS.map(f => (
        <div className="fresh-row" key={f.src}>
          <span>{f.src}</span>
          <span className="dim">{f.last}</span>
          <span className="dim">{f.cadence}</span>
          <span className={f.lag_s > 60 ? "warn" : "dim"}>{f.lag_s}s</span>
          <span><span className={`stat-pill ${f.ok ? "ok" : "stale"}`}>{f.ok ? "fresh" : "stale"}</span></span>
        </div>
      ))}
    </Panel>
  );
}

// ---- Reconciliation ----
function Reconciliation() {
  return (
    <Panel title="Reconciliation" sub={`last ${RECON.last_run}`}>
      <div className="kv-list">
        <div className="kv"><span className="k">Total reconciled (24h)</span><span className="v">{RECON.total.toLocaleString()}</span></div>
        <div className="kv"><span className="k">Mismatches</span>
          <span className="v">
            {RECON.mismatches > 0
              ? <span className="warn mono">{RECON.mismatches}</span>
              : <span className="up mono">0</span>}
          </span>
        </div>
        <div className="kv"><span className="k">Unresolved</span>
          <span className="v">
            {RECON.unresolved > 0
              ? <span className="warn mono">{RECON.unresolved} <a href="#">→ open</a></span>
              : <span className="up mono">0</span>}
          </span>
        </div>
      </div>
      {RECON.unresolved > 0 && (
        <div className="ar-card med" style={{ marginTop: 10 }}>
          <div className="ar-title"><Icon name="alert" size={12}/> ord_01HK6T4S2P9</div>
          <div className="ar-cause">1 fill local · 2 broker · ∆ qty = +28 · linked from Action Required</div>
          <div className="ar-cta"><button className="btn primary sm">Open in ledger</button></div>
        </div>
      )}
    </Panel>
  );
}

// ---- Drift monitor ----
function DriftMonitor() {
  const over = DRIFT.current_bps > DRIFT.threshold_bps;
  return (
    <Panel title="Drift monitor" sub={`${DRIFT.window}-trade rolling`}>
      <div className="drift-card">
        <div>
          <div className={`big mono ${over ? "over" : ""}`}>{DRIFT.current_bps.toFixed(1)}<span className="text-mini dim"> bps</span></div>
          <div className="thresh">threshold: &lt; {DRIFT.threshold_bps.toFixed(1)} bps · halts on 3× consecutive</div>
        </div>
        <div>
          <Sparkline values={DRIFT.sparkline} height={48} color={over ? "var(--warn)" : "var(--success)"} fill over={over} />
          <div className="text-mini dim">last 20 trades</div>
        </div>
      </div>
    </Panel>
  );
}

// ---- Cost model ----
function CostModel({ lens, setLens }) {
  const c = COST_MODEL.per_trade_bps;
  return (
    <Panel title="Cost model"
      sub={`current lens: ${lens}`}
      actions={<LensToggle value={lens} onChange={setLens} />}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, alignItems: "end" }}>
        {[
          ["raw", c.raw, "broker quote"],
          ["broker_paper", c.broker_paper, "alpaca paper fills"],
          ["pessimistic", c.pessimistic, "default policy"]
        ].map(([k, v, sub]) => (
          <div key={k} style={{
            border: "1px solid var(--border)",
            background: lens === k ? "var(--panel-2)" : "transparent",
            borderRadius: 6, padding: "8px 10px"
          }}>
            <div className="text-mini dim" style={{ textTransform: "uppercase", letterSpacing: "0.06em" }}>{k.replace("_"," ")}</div>
            <div className="mono" style={{ fontSize: 18 }}>{v.toFixed(1)}<span className="text-mini dim"> bps</span></div>
            <div className="text-mini dim">{sub}</div>
          </div>
        ))}
      </div>
      <Prov>source: lock_cost_model v0.9 · calibrator last ran 12:30</Prov>
    </Panel>
  );
}

// ---- Policy locks ----
function PolicyLocks() {
  return (
    <Panel title="Policy locks" sub={`${POLICY_LOCKS.length} lock files · master hash ec1f…77a`} flush>
      <div className="lock-row head">
        <span>Lock file</span>
        <span>Version</span>
        <span>Changed</span>
        <span>Signer</span>
        <span>Status</span>
      </div>
      {POLICY_LOCKS.map(l => (
        <div className="lock-row" key={l.name}>
          <span>{l.name}</span>
          <span className="dim">{l.ver}</span>
          <span className="dim">{l.changed}</span>
          <span className="dim">{l.signer}</span>
          <span><span className={`stat-pill ${l.status}`}><Icon name={l.status === "verified" ? "check" : "alert"} size={9}/> {l.status}</span></span>
        </div>
      ))}
    </Panel>
  );
}

// ---- Personas ----
function Personas() {
  return (
    <Panel title="Personas" sub={`${PERSONAS.length} files · hash-verified`}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 6 }}>
        {PERSONAS.map(p => (
          <div key={p.name} style={{
            display: "grid", gridTemplateColumns: "1fr auto", alignItems: "center",
            padding: "4px 8px", border: "1px solid var(--border)", borderRadius: 4,
            fontSize: 11.5
          }}>
            <span><span className="mono">{p.name}</span> <span className="dim mono text-mini">{p.hash}</span></span>
            <span className={`stat-pill ${p.status}`} title={p.status}>
              <Icon name="check" size={9}/>
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

// ---- Halts ----
function HaltHistory() {
  return (
    <Panel title="Halt history" sub="last 30 days" flush>
      <div className="halt-row head">
        <span>Time</span>
        <span>Reason</span>
        <span>By</span>
        <span>Duration</span>
        <span>Seq</span>
      </div>
      {HALTS.map(h => (
        <div className="halt-row" key={h.seq}>
          <span className="dim">{h.time}</span>
          <span><code style={{ color: "var(--halt)" }}>{h.reason}</code></span>
          <span className="dim">{h.operator}</span>
          <span className="dim">{h.duration}</span>
          <span className="dim">{h.seq}</span>
        </div>
      ))}
    </Panel>
  );
}

// ---- Ledger health (with chain strip) ----
function LedgerHealth() {
  const lh = LEDGER_HEALTH;
  return (
    <Panel title="Ledger health" sub={`last seq ${lh.last_seq}`}
      actions={<HashVerifiedCheck ts={lh.chain_verified_at} />}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 4, marginBottom: 8 }}>
        {lh.tables.map(t => (
          <div className="kv" key={t.name} style={{ borderBottom: "1px dashed var(--border)", padding: "3px 0" }}>
            <span className="k mono">{t.name}</span>
            <span className="v">{t.rows.toLocaleString()}</span>
          </div>
        ))}
      </div>
      <div className="text-mini dim" style={{ letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 4 }}>Last 60 blocks</div>
      <div className="chain-strip">
        {lh.blocks.map((b, i) => (
          <div key={i}
            className={`chain-block ${i === lh.blocks.length - 1 ? "last" : ""} ${b.ok ? "" : "fail"}`}
            title={`seq ${b.seq}`} />
        ))}
      </div>
      <Prov>last_hash: {lh.last_hash} · verified at {lh.chain_verified_at}</Prov>
    </Panel>
  );
}

// ---- Daemon heartbeat ----
function DaemonHeartbeat() {
  const [t, setT] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setT(x => x + 1), 200);
    return () => clearInterval(id);
  }, []);

  // ECG-like wave
  const w = 200, h = 36;
  const cycle = 30; // points per beat
  const pts = [];
  for (let i = 0; i < w; i++) {
    const phase = (i + t * 2) % cycle;
    let y = h / 2;
    if (phase === 12) y = h * 0.18;
    else if (phase === 14) y = h * 0.82;
    else if (phase === 16) y = h / 2 - 4;
    pts.push([i, y]);
  }
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1].toFixed(1)).join(" ");

  return (
    <Panel title="Daemon heartbeat" sub={`uptime ${DAEMON.uptime}`}>
      <div className="heartbeat">
        <div className="hb-ecg">
          <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" width="100%" height="100%">
            <path d={d} stroke="var(--success)" strokeWidth="1.3" fill="none" />
          </svg>
        </div>
        <div className="hb-stats">
          <div>{DAEMON.beats_per_min}/min</div>
          <div className="text-mini dim">pid {DAEMON.pid} · {DAEMON.host}</div>
        </div>
      </div>
      <div className="kv-list" style={{ marginTop: 6 }}>
        <div className="kv"><span className="k">Last beat</span><span className="v">{DAEMON.last_beat}</span></div>
        <div className="kv"><span className="k">Host · PID</span><span className="v">{DAEMON.host} · {DAEMON.pid}</span></div>
      </div>
    </Panel>
  );
}

Object.assign(window, { SurfaceSystemHealth });
