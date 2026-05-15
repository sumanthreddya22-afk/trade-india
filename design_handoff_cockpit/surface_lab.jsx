// ============================================================
// surface_lab.jsx — Surface C
// Strategy Lab. Visually quarantined from trading.
// ============================================================

function SurfaceStrategyLab() {
  const [selected, setSelected] = useState("ETF_MOMENTUM_v1");
  const strat = STRATEGIES.find(s => s.name === selected) || STRATEGIES[0];

  return (
    <main className="surface lab">
      <div className="lab-banner">
        <span className="pill">RESEARCH</span>
        <span>This surface cannot move capital. All affordances are sandboxed — promotion requires typed approval and a kernel hash match.</span>
      </div>

      <div className="cols-2" style={{ marginBottom: "var(--gap)" }}>
        <Panel title="Strategy registry" sub={`${STRATEGIES.length} versions`} flush>
          <div style={{ overflowX: "auto" }}>
            <table className="tbl">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Lane</th>
                  <th>State</th>
                  <th className="num">Tier</th>
                  <th className="num">P-Sharpe</th>
                  <th className="num">D-Sharpe</th>
                  <th className="num">PBO</th>
                  <th>Last run</th>
                  <th>Hash</th>
                </tr>
              </thead>
              <tbody>
                {STRATEGIES.map(s => (
                  <tr key={s.name + s.hash}
                    onClick={() => setSelected(s.name)}
                    className={selected === s.name ? "is-row-expanded" : ""}
                    style={{ cursor: "pointer" }}>
                    <td className="symbol">{s.name}</td>
                    <td className="dim">{s.lane}</td>
                    <td>
                      <span className={`classify ${stateToClass(s.state)}`}>{s.state.replace("_", " ")}</span>
                    </td>
                    <td className="num"><TierBadge tier={s.tier} /></td>
                    <td className={`num ${s.p_sharpe >= 1 ? "up" : "dim"}`}>{fmtNum(s.p_sharpe, 2)}</td>
                    <td className={`num ${s.d_sharpe >= 0.8 ? "up" : "dim"}`}>{fmtNum(s.d_sharpe, 2)}</td>
                    <td className={`num ${s.pbo <= 0.3 ? "up" : s.pbo >= 0.5 ? "down" : "warn"}`}>{fmtNum(s.pbo, 2)}</td>
                    <td className="dim">{s.last_run}</td>
                    <td className="dim">{s.hash}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Promotion queue" sub={`${PROMOTION_QUEUE.length} awaiting`}
          actions={<span className="text-mini halt-c">requires typed approval</span>}>
          {PROMOTION_QUEUE.map(p => (
            <div className="promo-row" key={p.name}>
              <div>
                <div className="strat">{p.name}</div>
                <div className="text-mini dim mono">{p.lane} · Tier 3 passed · 14-day cooldown</div>
              </div>
              <div className="pess">{fmtNum(p.p_sharpe, 2)} <div className="text-mini dim">p-sharpe</div></div>
              <div className="dsr">{fmtNum(p.d_sharpe, 2)} <div className="text-mini dim">deflated</div></div>
              <div className="pbo">{fmtPct(p.pbo, 0)}<div className="text-mini dim">PBO</div></div>
              <button className="btn ghost" disabled title="Type 'promote' in approval modal">
                <Icon name="alert" size={11}/> review &amp; sign
              </button>
            </div>
          ))}
          <Prov>source: validation/tier3.jsonl · only Tier-3 survivors appear here</Prov>
        </Panel>
      </div>

      {/* Strategy drilldown */}
      <Panel title={<span>Drilldown · <span className="mono" style={{ fontWeight: 400 }}>{strat.name}#{strat.hash}</span></span>}
        sub={`lane ${strat.lane} · ${strat.state.replace("_"," ")}`}
        actions={<span className="row"><TierBadge tier={strat.tier}/></span>}>
        <div className="cols-2" style={{ gap: "var(--gap)" }}>
          <div>
            <div className="text-mini dim" style={{ letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 6 }}>Walk-forward folds</div>
            <div className="wf-folds">
              {WF_FOLDS.map((f, i) => (
                <div className={`wf-fold ${f.locked ? "locked" : ""}`} key={i}>
                  <div className="wf-name">{f.name}</div>
                  <div className={`wf-val ${f.up ? "up" : "down"} mono`}>{fmtNum(f.sharpe, 2)}</div>
                  <div className="wf-spark">
                    <Sparkline values={genWalk(f.sharpe)} height={20} color={f.locked ? "var(--halt)" : "var(--success)"} fill />
                  </div>
                </div>
              ))}
            </div>
            <Prov>5 folds + 30% locked holdout · seed 0x42 · last run 13:02</Prov>

            <div style={{ marginTop: 14 }}>
              <div className="text-mini dim" style={{ letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 6 }}>Parameter plateau (lookback × vol_thresh)</div>
              <Heatmap />
              <Prov>11×11 grid · color = pessimistic Sharpe · max marked with ◦</Prov>
            </div>
          </div>

          <div>
            <div className="text-mini dim" style={{ letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 6 }}>Validation tiers achieved</div>
            <div className="kv-list" style={{ marginBottom: 12 }}>
              <div className="kv"><span className="k">Tier 1 — single fit</span><span className="v"><span className="hash-check"><Icon name="check" size={11}/> 2026-04-12 · art_v1#a3f2_t1</span></span></div>
              <div className="kv"><span className="k">Tier 2 — walk-forward</span><span className="v"><span className="hash-check"><Icon name="check" size={11}/> 2026-04-29 · art_v1#a3f2_t2</span></span></div>
              <div className="kv"><span className="k">Tier 3 — pessimistic + BH-FDR</span><span className="v"><span className="hash-check"><Icon name="check" size={11}/> 2026-05-09 · art_v1#a3f2_t3</span></span></div>
              <div className="kv"><span className="k">Cooldown until</span><span className="v">2026-05-23 (14d)</span></div>
            </div>

            <div className="text-mini dim" style={{ letterSpacing: "0.06em", textTransform: "uppercase", marginBottom: 6 }}>Mutation log</div>
            <div className="mutation-log">
              {MUTATIONS.map((m, i) => (
                <div className="mut-row" key={i}>
                  <span className="dim">{m.time}</span>
                  <span className={`mut-tag ${m.tag}`}>{m.tag}</span>
                  <span>{m.param}</span>
                  <span className="dim">p={m.p}</span>
                  <button className="btn ghost sm">view</button>
                </div>
              ))}
            </div>
            <Prov>BH-FDR α=0.10 · 90-day rolling failure memory · {MUTATIONS.filter(m=>m.tag==="rejected").length} rejected stored</Prov>
          </div>
        </div>
      </Panel>

      <div className="cols-2" style={{ marginTop: "var(--gap)" }}>
        <HypothesisIntake />
        <LLMSpend />
      </div>
    </main>
  );
}

function stateToClass(state) {
  switch (state) {
    case "live":  return "manual";
    case "paper": return "bot";
    case "retired": return "external";
    case "research_only": return "external";
    default: return "external";
  }
}

function genWalk(end) {
  const arr = [];
  let v = end * 0.6;
  for (let i = 0; i < 24; i++) {
    v += (Math.sin(i * 1.3) + Math.cos(i * 0.7)) * 0.04 + 0.02;
    arr.push(v);
  }
  arr[arr.length - 1] = end;
  return arr;
}

function Heatmap() {
  const max = Math.max(...HEATMAP.flat());
  let argmax = [0, 0];
  HEATMAP.forEach((row, y) => row.forEach((v, x) => { if (v === max) argmax = [y, x]; }));
  return (
    <div className="heatmap">
      {HEATMAP.map((row, y) =>
        row.map((v, x) => {
          const t = v / max;
          const isMax = y === argmax[0] && x === argmax[1];
          return (
            <div key={`${y}-${x}`} className="heat-cell"
              title={`(${x},${y}) sharpe ${v.toFixed(2)}`}
              style={{
                background: `oklch(${0.30 + t * 0.45} ${0.06 + t * 0.10} 145)`,
                boxShadow: isMax ? "inset 0 0 0 1.5px var(--text)" : "none"
              }}>
            </div>
          );
        })
      )}
    </div>
  );
}

function HypothesisIntake() {
  const [mode, setMode] = useState("intake");
  return (
    <Panel title="Hypothesis intake"
      sub="submission triggers an async research run"
      actions={<span className="text-mini dim">queue: 3 pending</span>}>
      <div className="intake-mode" style={{ marginBottom: 10 }}>
        {[
          ["draft", "Draft", "no run"],
          ["intake", "Intake", "adversarial review"],
          ["mutate", "Mutate", "enumerate search space"]
        ].map(([k, l, hint]) => (
          <button key={k} className={mode === k ? "is-active" : ""} onClick={() => setMode(k)}>
            <div style={{ fontWeight: 600 }}>{l}</div>
            <div className="text-mini dim">{hint}</div>
          </button>
        ))}
      </div>
      <div className="intake-form">
        <div className="field span-all">
          <label>Title</label>
          <input placeholder="e.g. ETF momentum gated by yield curve" defaultValue="Crypto trend gated on macro VIX" />
        </div>
        <div className="field">
          <label>Lane</label>
          <select defaultValue="crypto">
            <option>stocks</option>
            <option>crypto</option>
            <option>options</option>
          </select>
        </div>
        <div className="field">
          <label>Search-space hash</label>
          <input placeholder="auto" defaultValue="ss_01HK_92ce…" />
        </div>
        <div className="field span-all">
          <label>Hypothesis (one paragraph)</label>
          <textarea defaultValue="When VIX > 22 AND BTC-SPY correlation > 0.4, trend signals fail in crypto by mean-reversion in 1.5 sessions. Propose gating BTC_TREND_v2 entries on this conjunction."></textarea>
        </div>
        <div className="field">
          <label>Expected Sharpe (prior)</label>
          <input defaultValue="0.9 ± 0.3" />
        </div>
        <div className="field">
          <label>Adversary budget</label>
          <input defaultValue="$2.50 · Opus judge × 1" />
        </div>
        <div className="span-all row" style={{ justifyContent: "flex-end", marginTop: 4 }}>
          <button className="btn ghost sm">Save draft</button>
          <button className="btn primary sm">Submit for research run</button>
        </div>
      </div>
    </Panel>
  );
}

function LLMSpend() {
  return (
    <Panel title="LLM spend"
      sub={`$${LLM_SPEND.today_total.toFixed(2)} today · $${LLM_SPEND.month_total.toFixed(2)} mo / $${LLM_SPEND.budget_month.toFixed(0)} budget`}>
      <div style={{ marginBottom: 10 }}>
        <Sparkline values={[3.1, 2.4, 5.8, 4.2, 6.1, 3.9, 8.4]} height={36} color="var(--info)" fill />
        <div className="text-mini dim row" style={{ marginTop: 4 }}>
          <span>last 7 days</span>
          <span className="spacer"></span>
          <span className="mono">${(LLM_SPEND.month_total / 30 * 7).toFixed(2)} avg/wk</span>
        </div>
      </div>
      {LLM_SPEND.roles.map(r => (
        <div className="llm-spend-row" key={r.role}>
          <span className="name">{r.role} <span className="dim text-mini">({r.model})</span></span>
          <span className="bar"><i className={r.color} style={{ width: r.share * 100 + "%" }}></i></span>
          <span className="val">${r.today.toFixed(2)}</span>
        </div>
      ))}
      <Prov>source: anthropic_api_usage.jsonl · cost cap halts research if today &gt; $20</Prov>
    </Panel>
  );
}

Object.assign(window, { SurfaceStrategyLab });
