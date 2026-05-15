// ============================================================
// surface_right_now.jsx — Surface A
// "Is the bot healthy and what is it doing this minute?"
// ============================================================

function SurfaceRightNow({ snap, activity, equityRange, setEquityRange, lens, setLens, degraded }) {
  return (
    <main className="surface">
      <div className="cols-3" style={{ marginBottom: "var(--gap)" }}>
        <PostureColumn snap={snap} />
        <PositionsAndOrdersColumn snap={snap} degraded={degraded} />
        <ActionAndFeedColumn snap={snap} activity={activity} />
      </div>
      <div className="cols-2">
        <Panel title="Equity curve"
          sub={`${snap.account.equity.toLocaleString("en-US", {style:"currency", currency:"USD"})} · range ${equityRange}`}
          actions={
            <>
              <LensToggle value={lens} onChange={setLens} />
              <span className="equity-range">
                {["1w","1m","3m","ytd","all"].map(r =>
                  <button key={r} className={equityRange === r ? "is-active" : ""}
                    onClick={() => setEquityRange(r)}>{r}</button>)}
              </span>
            </>
          }>
          <EquityChart data={EQUITY_CURVE} />
          <div className="row" style={{ marginTop: 6, gap: 14, flexWrap: "wrap" }}>
            <span className="text-mini dim">Markers:</span>
            <span className="text-mini" style={{ color: "var(--halt)" }}>● halt</span>
            <span className="text-mini" style={{ color: "var(--warn)" }}>● profile change</span>
            <span className="text-mini" style={{ color: "var(--info)" }}>● policy lock change</span>
            <span className="spacer"></span>
            <Prov>source: /api/equity-curve · pessimistic lens applied · last update 13:00:00Z</Prov>
          </div>
        </Panel>

        <Panel title="Exposure" sub="by lane · % of equity">
          <div className="donut-wrap">
            <Donut data={EXPOSURE_BREAKDOWN} />
            <div className="donut-legend">
              {EXPOSURE_BREAKDOWN.map(d => (
                <div className="leg-row" key={d.name}>
                  <span className="leg-sw" style={{ background: d.color }}></span>
                  <span className="leg-name">{d.name}</span>
                  <span className="leg-val">{fmtPct(d.value, 1)}</span>
                </div>
              ))}
            </div>
          </div>
          <Prov>source: /api/snapshot.positions × marks · {snap.lanes.length} lanes</Prov>
        </Panel>
      </div>
    </main>
  );
}

// ============================================================
// Column 1 — Posture (strategy mode, regime, risk caps)
// ============================================================
function PostureColumn({ snap }) {
  return (
    <div className="col-stack">
      <Panel title="Strategy mode" sub={`${STRATEGY_MODE.length} versions`}>
        <div className="kv-list">
          {STRATEGY_MODE.map(s => (
            <div className="kv" key={s.name}>
              <span className="k mono" style={{ fontSize: 11.5 }}>{s.name}</span>
              <span className="v">
                <span className={`classify ${s.state === "armed" ? "bot" : s.state === "paused" ? "manual" : "external"}`}>
                  {s.state.replace("_", " ")}
                </span>
              </span>
            </div>
          ))}
        </div>
        <Prov>source: registry · hash-locked at boot</Prov>
      </Panel>

      <Panel title="Regime" sub={REGIME.since}>
        <div className="regime">
          <span className={`r-tag ${REGIME.label}`}>{REGIME.label}</span>
          <span className="r-since">since {REGIME.since}</span>
        </div>
        {REGIME.signals.map(sig => (
          <div className="signal-row" key={sig.name}>
            <span className="name">{sig.name}</span>
            <span className="val">
              <Icon name={sig.trend} size={10} /> {sig.val}
            </span>
          </div>
        ))}
        <Prov>source: regime_detector v2.1 · 5 signals</Prov>
      </Panel>

      <Panel title="Risk caps" sub="account & lane">
        {RISK_CAPS.map(c => (
          <RiskCapBar key={c.name} {...c} />
        ))}
        <Prov>source: lock_risk_caps_v2 · last verify 13:02:11</Prov>
      </Panel>
    </div>
  );
}

// ============================================================
// Column 2 — Positions + open orders (LANE-GROUPED)
// ============================================================
function PositionsAndOrdersColumn({ snap, degraded }) {
  const [expanded, setExpanded] = useState(new Set());
  const toggle = (sym) => {
    const s = new Set(expanded);
    s.has(sym) ? s.delete(sym) : s.add(sym);
    setExpanded(s);
  };

  // group positions by lane (in lane order)
  const byLane = useMemo(() => {
    const out = LANES.map(l => ({
      lane: l,
      positions: POSITIONS.filter(p => p.lane === l.key)
    }));
    return out;
  }, []);

  const orderByLane = useMemo(() => {
    return LANES.map(l => ({
      lane: l,
      orders: OPEN_ORDERS.filter(o => o.lane === l.key)
    }));
  }, []);

  return (
    <div className="col-stack">
      <Panel title="Positions"
        sub={`${POSITIONS.length} open · ${POSITIONS.filter(p => p.classification === "unknown").length} unknown`}
        flush>
        <div style={{ overflowX: "auto" }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="num">Qty</th>
                <th className="num">Entry</th>
                <th className="num">Mark</th>
                <th className="num">P&L $</th>
                <th className="num">P&L %</th>
                <th>Class.</th>
                <th className="num">Stop</th>
                <th className="num">Age</th>
              </tr>
            </thead>
            <tbody>
              {byLane.map((grp, gi) => (
                <React.Fragment key={grp.lane.key}>
                  <tr className={`lane-group-row ${gi === 0 ? "first" : ""}`}>
                    <td colSpan={9}>
                      {grp.lane.short} · {grp.lane.name}
                      <span className="lane-summary">
                        {grp.positions.length === 0 ? "no positions" :
                          `exposure ${fmtPct(grp.lane.exposure_pct, 1)} / cap ${fmtPct(grp.lane.cap_pct || 0, 1)}`}
                      </span>
                    </td>
                  </tr>
                  {grp.positions.length === 0 && (
                    <tr><td colSpan={9} className="dim" style={{ textAlign: "center", padding: 10, fontFamily: "IBM Plex Sans" }}>—</td></tr>
                  )}
                  {grp.positions.map(p => (
                    <PositionRow key={p.symbol} pos={p}
                      expanded={expanded.has(p.symbol)}
                      onToggle={() => toggle(p.symbol)} />
                  ))}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Open orders"
        sub={`${OPEN_ORDERS.filter(o => !o.canceled).length} working · ${OPEN_ORDERS.filter(o => o.stuck).length} stuck`}
        flush>
        <div style={{ overflowX: "auto" }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th className="num">Qty</th>
                <th>Type</th>
                <th>Status</th>
                <th>Age</th>
                <th>Idempotency</th>
                <th>client_order_id</th>
              </tr>
            </thead>
            <tbody>
              {orderByLane.map((grp, gi) => (
                <React.Fragment key={grp.lane.key}>
                  <tr className={`lane-group-row ${gi === 0 ? "first" : ""}`}>
                    <td colSpan={8}>{grp.lane.short}<span className="lane-summary">{grp.orders.length === 0 ? "no open orders" : grp.orders.length + " orders"}</span></td>
                  </tr>
                  {grp.orders.length === 0 && (
                    <tr><td colSpan={8} className="dim" style={{ textAlign: "center", padding: 10, fontFamily: "IBM Plex Sans" }}>—</td></tr>
                  )}
                  {grp.orders.map(o => (
                    <tr key={o.idempotency} className={o.canceled ? "is-canceled" : o.stuck ? "is-row-warn" : ""}>
                      <td className="symbol">{o.symbol}</td>
                      <td>{o.side}</td>
                      <td className="num">{o.qty}</td>
                      <td>{o.type}</td>
                      <td>
                        <span className={`classify ${o.stuck ? "manual" : o.canceled ? "external" : "bot"}`}>
                          {o.status}
                        </span>
                      </td>
                      <td className={`num ${o.stuck ? "warn" : ""}`}>{fmtAge(o.age_s)}</td>
                      <td className="dim">{o.idempotency}</td>
                      <td className="dim">{o.client_order_id}</td>
                    </tr>
                  ))}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function PositionRow({ pos, expanded, onToggle }) {
  const up = pos.pl_abs >= 0;
  const unknown = pos.classification === "unknown";
  return (
    <>
      <tr className={`${unknown ? "is-row-danger" : ""} ${expanded ? "is-row-expanded" : ""}`}
        onClick={onToggle} style={{ cursor: "pointer" }}>
        <td className="symbol">
          <Icon name={expanded ? "chevron-down" : "chevron-right"} size={10} /> {pos.symbol}
        </td>
        <td className="num">{pos.qty}</td>
        <td className="num">{fmtNum(pos.entry, 2)}</td>
        <td className="num shimmer-mark" key={pos.mark}>{fmtNum(pos.mark, 2)}</td>
        <td className={`num ${up ? "up" : "down"}`}>{(up ? "+" : "") + fmtNum(pos.pl_abs, 2)}</td>
        <td className={`num ${up ? "up" : "down"}`}>{fmtPct(pos.pl_pct, 2, true)}</td>
        <td><ClassificationTag value={pos.classification} /></td>
        <td className="num dim">{pos.stop ? fmtNum(pos.stop, 2) : "—"}</td>
        <td className="num dim">{ageFrom(pos.opened_at)}</td>
      </tr>
      {expanded && (
        <tr className="expanded-detail">
          <td colSpan={9}>
            <div style={{ padding: "10px 16px", fontFamily: "IBM Plex Mono", fontSize: 11.5, color: "var(--text-dim)" }}>
              <div className="row" style={{ gap: 18, flexWrap: "wrap" }}>
                <div><span className="dim">strategy</span> <span style={{ color: "var(--text)" }}>{pos.strategy_version || "—"}</span></div>
                <div><span className="dim">order_uid</span> <span style={{ color: "var(--text)" }}>{pos.order_uid || "—"}</span></div>
                <div><span className="dim">opened</span> <span style={{ color: "var(--text)" }}>{pos.opened_at}</span></div>
                <div><span className="dim">drift</span> <span style={{ color: "var(--text)" }}>{pos.drift_bps != null ? pos.drift_bps + "bps" : "—"}</span></div>
              </div>
              {unknown && (
                <div className="ar-card high" style={{ marginTop: 10 }}>
                  <div className="ar-title">Unknown classification</div>
                  <div className="ar-cause">No <span className="mono">order_uid</span> in ledger. Phase 2+ kernels halt on unknown — currently in Phase 1 (paper).</div>
                  <div className="ar-cta">
                    <button className="btn primary sm">Classify as bot</button>
                    <button className="btn warn sm">Classify as manual</button>
                    <button className="btn sm">Mark external</button>
                    <button className="btn danger sm">Close position</button>
                  </div>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function ageFrom(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h";
  return Math.floor(h / 24) + "d";
}

// ============================================================
// Column 3 — Action required + activity feed
// ============================================================
function ActionAndFeedColumn({ snap, activity }) {
  const [filter, setFilter] = useState("all"); // all | lane | type
  const [laneFilter, setLaneFilter] = useState("all");

  const visible = activity.filter(a => {
    if (laneFilter === "all") return true;
    return a.lane === laneFilter || (laneFilter === "system" && a.lane === null);
  });

  return (
    <div className="col-stack">
      <Panel title="Action required" sub={`${ACTION_REQUIRED.length} open`}
        actions={
          ACTION_REQUIRED.length === 0
            ? <span className="text-mini up"><Icon name="check" size={11}/> all clear</span>
            : <span className="text-mini warn">{ACTION_REQUIRED.filter(a => a.severity === "high").length} high</span>
        }>
        <div className="actreq">
          {ACTION_REQUIRED.length === 0 && (
            <div className="ar-empty">
              <div className="check"><Icon name="check" size={16}/></div>
              <div>Nothing to act on.</div>
              <div className="dim text-mini" style={{ marginTop: 2 }}>Boring on purpose. The bot is fine.</div>
            </div>
          )}
          {ACTION_REQUIRED.map(a => (
            <div className={`ar-card ${a.severity}`} key={a.id}>
              <div className="ar-title">
                <Icon name="alert" size={13}/> {a.title}
              </div>
              <div className="ar-cause">{a.cause}</div>
              <div className="ar-cta">
                {a.cta.map((c, i) => (
                  <button key={i} className={`btn sm ${c.primary ? "primary" : ""}`}>{c.label}</button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="Activity feed"
        sub={`live · ledger seq ${activity[0]?.seq ?? "—"}`}
        flush
        actions={
          <span className="text-mini dim row" style={{ gap: 4 }}>
            <span className="sp-dot" style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--success)", display: "inline-block" }}></span>
            tail
          </span>
        }>
        <div className="feed">
          <div className="feed-list">
            {visible.slice(0, 50).map((a, i) => (
              <div key={a.seq} className={`feed-row ${i === 0 ? "is-new" : ""}`}>
                <span className="f-ts">{a.ts}</span>
                <span className={`f-type ${a.type}`}>{a.type[0].toUpperCase()}</span>
                <span className="f-lane">{a.lane ? a.lane : "sys"}</span>
                <span className="f-msg" title={a.msg}>{a.msg}</span>
                <span className="f-seq">{a.seq}</span>
              </div>
            ))}
          </div>
          <div className="feed-filter">
            <span className="text-mini dim row" style={{ alignItems: "center", marginRight: 4 }}><Icon name="filter" size={11}/></span>
            {["all", "stocks", "crypto", "options", "system"].map(l => (
              <button key={l} className={laneFilter === l ? "is-active" : ""} onClick={() => setLaneFilter(l)}>{l}</button>
            ))}
          </div>
        </div>
      </Panel>
    </div>
  );
}

Object.assign(window, { SurfaceRightNow });
