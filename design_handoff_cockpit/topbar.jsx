// ============================================================
// topbar.jsx — persistent top bar + kill/halt modal + halt banner
// ============================================================

function TopBar({ status, onKill, onResume, onProfileToggle, onStatusToggle, clock }) {
  const acct = status.account;
  const up = acct.day_pl_abs >= 0;
  const halted = status.system_state === "halted";

  return (
    <header className={`topbar ${halted ? "is-halted" : ""}`}>
      <div className="tb-left">
        <div className="tb-logo">
          <span className="tb-mark">K</span>
          <span className="tb-name">kernel</span>
          <span className="tb-version">v4.2.0</span>
        </div>
        <StatusPill state={status.system_state} />
      </div>

      <div className="tb-mid">
        <div className="tb-equity" title="Click to open equity curve drawer">
          <span className="eq-val">{fmtMoney(acct.equity)}</span>
          <span className={`eq-delta ${up ? "up" : "down"}`}>
            <Icon name={up ? "up" : "down"} size={11} />
            {(up ? "+" : "") + fmtMoney(acct.day_pl_abs)} ({fmtPct(acct.day_pl_pct, 2, true)})
          </span>
        </div>

        <div className="tb-lanes">
          {status.lanes.map(l => {
            const ratio = l.cap_pct ? l.exposure_pct / l.cap_pct : 0;
            const cls = !l.enabled ? "is-off" : ratio >= 1 ? "is-cap" : ratio >= 0.8 ? "is-warn" : "is-active";
            return (
              <span key={l.key} className={`lane-chip ${cls}`} title={`${l.name} — exposure ${fmtPct(l.exposure_pct, 1)} / cap ${fmtPct(l.cap_pct || 0, 1)}`}>
                <span className="dot"></span>
                <span>{l.short}{!l.enabled && " (off)"}</span>
                {l.enabled && (
                  <span className="exposure-bar"><i style={{ width: Math.min(100, ratio * 100) + "%" }}></i></span>
                )}
              </span>
            );
          })}
        </div>
      </div>

      <div className="tb-right">
        <span className="tb-time mono">{clock}</span>
        <button className="tb-profile" onClick={onProfileToggle} title="Risk profile drawer">
          <span className="label">profile</span>
          <span className="value">{status.risk_profile}</span>
          <Icon name="chevron-down" size={11} />
        </button>
        {/* dev: status state toggle for demo only */}
        {/* The actual kill button */}
        {halted ? (
          <button className="kill-btn is-resume" onClick={onResume} title="Open resume modal">
            <span className="pulse-dot"></span>
            <Icon name="play" size={12} /> Resume
          </button>
        ) : (
          <button className="kill-btn" onClick={onKill} title="Open halt modal (Ctrl + .)">
            <span className="pulse-dot"></span>
            <Icon name="halt" size={12} /> Halt
          </button>
        )}
      </div>
    </header>
  );
}

// ---- Halt banner (page-wide) ----
function HaltBanner({ halted }) {
  if (!halted || !halted.active) return null;
  return (
    <div className="halt-banner">
      <strong>● Halted</strong>
      <span>All new entries are blocked. Existing positions still update.</span>
      <span className="reason">reason: <code>{halted.reason}</code></span>
      <span className="since">since {halted.since} · by {halted.operator}</span>
    </div>
  );
}

// ---- Halt modal ----
function HaltModal({ onClose, onConfirm }) {
  const [reason, setReason] = useState("");
  const [ack1, setAck1] = useState(false);
  const [ack2, setAck2] = useState(false);
  const valid = reason.trim().length >= 4 && ack1 && ack2;
  const refInput = useRef(null);
  useEffect(() => { setTimeout(() => refInput.current?.focus(), 30); }, []);

  return (
    <div className="modal-overlay" onClick={(e) => { if (e.target.classList.contains("modal-overlay")) onClose(); }}>
      <div className="modal danger" role="dialog" aria-modal="true">
        <div className="modal-head">
          <div className="m-eyebrow danger">● Halt all new entries</div>
          <div className="m-title">This stops the kernel from opening new positions.</div>
        </div>
        <div className="modal-body">
          <div>
            Existing positions, stops, and reconciliation jobs <strong>continue to run</strong>.
            The halt is logged to the ledger and broadcast to every surface.
          </div>
          <div className="field">
            <label htmlFor="halt-reason">Reason (required, min 4 chars · written to ledger)</label>
            <input ref={refInput} id="halt-reason" type="text" placeholder="e.g. manual_check_macro_print"
              value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          <div className="checks">
            <label><input type="checkbox" checked={ack1} onChange={e => setAck1(e.target.checked)} /><span>I understand this writes <code>manual_operator_halt</code> to the ledger.</span></label>
            <label><input type="checkbox" checked={ack2} onChange={e => setAck2(e.target.checked)} /><span>I will resume manually — there is no auto-resume timer.</span></label>
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={!valid}
            onClick={() => onConfirm(reason.trim())}>
            Halt now
          </button>
        </div>
      </div>
    </div>
  );
}

// ---- Resume modal ----
function ResumeModal({ halted, onClose, onConfirm }) {
  const [ack, setAck] = useState(false);
  return (
    <div className="modal-overlay" onClick={(e) => { if (e.target.classList.contains("modal-overlay")) onClose(); }}>
      <div className="modal halt" role="dialog" aria-modal="true">
        <div className="modal-head">
          <div className="m-eyebrow halt">● Resume kernel</div>
          <div className="m-title">Halt active: <code>{halted.reason}</code></div>
        </div>
        <div className="modal-body">
          <div>
            Resuming returns the kernel to its previous risk profile (<code>{halted.profile_before || "neutral"}</code>).
            New entries will be considered on the next scan cycle.
          </div>
          <div className="kv-list" style={{ marginTop: 10 }}>
            <div className="kv"><span className="k">Halted by</span><span className="v">{halted.operator}</span></div>
            <div className="kv"><span className="k">Halted since</span><span className="v">{halted.since}</span></div>
            <div className="kv"><span className="k">Reason on ledger</span><span className="v">{halted.reason}</span></div>
          </div>
          <div className="checks" style={{ marginTop: 10 }}>
            <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} /><span>I have reviewed open orders, positions, and the latest reconciliation pass.</span></label>
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!ack} onClick={onConfirm}>Resume kernel</button>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { TopBar, HaltBanner, HaltModal, ResumeModal });
