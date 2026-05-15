// ============================================================
// surface_activity.jsx — Surface B
// "What happened in the last hours / day?"
// ============================================================

function SurfaceRecentActivity() {
  return (
    <main className="surface">
      <Panel title="Daily digest"
        sub={DAILY_DIGEST.date + " · auto-generated"}
        actions={<span className="text-mini dim mono">generated 13:02:11Z by judge_v1</span>}>
        <div className="digest">
          {DAILY_DIGEST.stats.map(s => (
            <div className="digest-stat" key={s.label}>
              <div className="label">{s.label}</div>
              <div className={`value ${s.up ? "up" : ""}`}>{s.value}</div>
              <div className="sub">{s.sub}</div>
            </div>
          ))}
        </div>
      </Panel>

      <div className="cols-2" style={{ marginTop: "var(--gap)" }}>
        <div className="col-stack">
          <DecisionActivity />
          <LastScan />
        </div>
        <div className="col-stack">
          <Lessons />
          <Panel title="Lane summary" sub="last 24h">
            <LaneSummary />
          </Panel>
        </div>
      </div>
    </main>
  );
}

function DecisionActivity() {
  const [open, setOpen] = useState(new Set(["d1"]));
  const [lane, setLane] = useState("all");
  const toggle = (id) => {
    const s = new Set(open);
    s.has(id) ? s.delete(id) : s.add(id);
    setOpen(s);
  };
  const filtered = DECISIONS.filter(d => {
    if (lane === "all") return true;
    const strat = STRATEGY_MODE.find(s => s.name === d.strategy);
    return strat?.lane === lane;
  });

  return (
    <Panel title="Decision activity" sub={`${DECISIONS.length} decisions today`}
      flush
      actions={
        <span className="row" style={{ gap: 4 }}>
          {["all","stocks","crypto","options"].map(l =>
            <button key={l} className={`ph-action ${lane === l ? "is-active" : ""}`} onClick={() => setLane(l)}>{l}</button>
          )}
        </span>
      }>
      <div>
        {filtered.length === 0 && <div className="ar-empty" style={{ margin: 14 }}>No decisions for this lane in the window.</div>}
        {filtered.map(d => (
          <React.Fragment key={d.id}>
            <div className={`decision-row ${open.has(d.id) ? "is-open" : ""}`} onClick={() => toggle(d.id)}>
              <span className="d-time">{d.time}</span>
              <span className={`d-action ${d.action}`}>{d.action.replace("-", " ")}</span>
              <span className="d-strat" title={d.strategy}>{d.strategy === "n/a" ? "—" : d.strategy.split("#")[0]}</span>
              <span className="d-symbol">{d.symbol}</span>
              <span className="d-reason">{d.reason}</span>
              <span className="d-ledger">
                <LedgerSeqChip seq={d.seq} />
              </span>
            </div>
            {open.has(d.id) && d.gates && (
              <div className="decision-detail">
                <div>
                  <div className="text-mini dim" style={{ marginBottom: 6, letterSpacing: 0.06 + "em", textTransform: "uppercase" }}>Gates evaluated</div>
                  <div className="gate-list">
                    {d.gates.map((g, gi) => (
                      <div className="gate-row" key={gi}>
                        <span className={g.ok ? "g-ok" : "g-no"}>
                          <Icon name={g.ok ? "check" : "x"} size={11} />
                        </span>
                        <span className="g-name">{g.name}</span>
                        <span className="g-val">{g.val} <span className="dim">/ {g.thresh}</span></span>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <div className="text-mini dim" style={{ marginBottom: 6, letterSpacing: 0.06 + "em", textTransform: "uppercase" }}>Provenance</div>
                  <div className="kv-list">
                    <div className="kv"><span className="k">Ledger seq</span><span className="v">{d.seq}</span></div>
                    <div className="kv"><span className="k">Strategy</span><span className="v">{d.strategy}</span></div>
                    <div className="kv"><span className="k">Decision UID</span><span className="v">dec_01HK{(28000+d.seq).toString(16)}</span></div>
                    <div className="kv"><span className="k">Cost lens</span><span className="v">pessimistic (default)</span></div>
                  </div>
                </div>
              </div>
            )}
          </React.Fragment>
        ))}
      </div>
    </Panel>
  );
}

function LastScan() {
  const max = Math.max(...LAST_SCAN.funnel.map(f => f.val));
  return (
    <Panel title="Last scan" sub={`cycle ${LAST_SCAN.cycle_ms}ms · ${LAST_SCAN.ts}`}>
      <div className="scan-funnel">
        {LAST_SCAN.funnel.map(f => (
          <div className="scan-row" key={f.name}>
            <span className="name">{f.name}</span>
            <span className="bar"><i style={{ width: (f.val / max * 100) + "%" }}></i></span>
            <span className="val">{f.val}</span>
          </div>
        ))}
      </div>
      <Prov>source: scan/stocks · gates evaluated in order</Prov>
    </Panel>
  );
}

function Lessons() {
  return (
    <Panel title="Lessons" sub={`${LESSONS.length} notes`}
      actions={<button className="btn ghost sm"><Icon name="external" size={11}/> view all</button>}>
      <div>
        {LESSONS.map((l, i) => (
          <div className="lesson" key={i}>
            <div className="lesson-head">
              <span className="ts">{l.ts}</span>
              <span className="tag">{l.tag}</span>
            </div>
            <div className="lesson-body">{l.body}</div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function LaneSummary() {
  // small per-lane summary numbers
  const lanes = [
    { key: "stocks", fills: 5, submits: 9, skips: 14, pl: "+$118.88" },
    { key: "crypto", fills: 2, submits: 3, skips: 22, pl: "+$4.15"   },
    { key: "options", fills: 0, submits: 0, skips: 0, pl: "$0.00", off: true },
  ];
  return (
    <div>
      {lanes.map(l => {
        const meta = LANES.find(x => x.key === l.key);
        return (
          <div key={l.key} style={{
            display: "grid", gridTemplateColumns: "100px repeat(4, 1fr)", gap: 10, padding: "8px 0",
            borderBottom: "1px dashed var(--border)", alignItems: "center", fontSize: 12
          }}>
            <div className="row">
              <span className={`status-pill ${l.off ? "halted" : "running"}`} style={{ height: 22, padding: "0 8px" }}>
                <span className="sp-dot"></span>{meta.short}
              </span>
            </div>
            <div><div className="dim text-mini">fills</div><div className="mono">{l.fills}</div></div>
            <div><div className="dim text-mini">submits</div><div className="mono">{l.submits}</div></div>
            <div><div className="dim text-mini">skips</div><div className="mono">{l.skips}</div></div>
            <div><div className="dim text-mini">p&l</div><div className="mono up">{l.pl}</div></div>
          </div>
        );
      })}
      <Prov>source: /api/snapshot.decisions aggregated · last 24h</Prov>
    </div>
  );
}

Object.assign(window, { SurfaceRecentActivity });
