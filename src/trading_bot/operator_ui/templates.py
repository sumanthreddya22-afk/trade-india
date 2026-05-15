"""Inline HTML templates for the operator UI.

Kept in a Python module instead of jinja files so:
  * packaging is one less moving part (no template directory to find),
  * tests can import the templates as strings,
  * the dashboard works from any cwd as long as the package is
    installed.

Style is intentionally bare — no external CSS, no JS framework. The
operator wants information density, not animation.
"""
from __future__ import annotations

BASE_CSS = """
:root {
  color-scheme: light dark;
  --fg: #1a1a1a; --bg: #fafafa; --card: #ffffff;
  --muted: #666; --border: #ddd; --accent: #0066cc;
  --ok: #228822; --warn: #b07000; --bad: #c00000;
}
@media (prefers-color-scheme: dark) {
  :root { --fg: #e8e8e8; --bg: #1a1a1a; --card: #2a2a2a;
          --muted: #999; --border: #444; --accent: #4ea1ff; }
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--fg); margin: 0;
       padding: 1.5rem; line-height: 1.4; }
header { display: flex; justify-content: space-between; align-items: baseline;
         margin-bottom: 1.5rem; }
h1 { margin: 0; font-size: 1.4rem; }
h2 { font-size: 1.05rem; margin: 0 0 0.6rem 0; border-bottom: 1px solid var(--border); padding-bottom: 0.25rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 1rem; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 1rem; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 0.8rem; font-size: 0.92rem; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { padding: 0.3rem 0.5rem; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 500; }
.pill { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
        font-size: 0.78rem; font-weight: 500; }
.pill.ok   { background: rgba(34,136,34,0.15); color: var(--ok); }
.pill.warn { background: rgba(176,112,0,0.15); color: var(--warn); }
.pill.bad  { background: rgba(192,0,0,0.15); color: var(--bad); }
.btn { display: inline-block; padding: 0.4rem 0.9rem; border-radius: 6px;
       border: 1px solid var(--border); background: var(--card); color: var(--fg);
       text-decoration: none; cursor: pointer; font-size: 0.9rem; }
.btn.primary { background: var(--accent); color: white; border-color: var(--accent); }
.btn.danger  { background: var(--bad);    color: white; border-color: var(--bad); }
input[type=text], textarea, select { width: 100%; padding: 0.45rem 0.55rem;
       background: var(--bg); color: var(--fg); border: 1px solid var(--border);
       border-radius: 6px; font: inherit; }
textarea { min-height: 120px; resize: vertical; }
form > * + * { margin-top: 0.6rem; }
small.muted { color: var(--muted); }
.flash { padding: 0.7rem 1rem; border-radius: 6px; margin-bottom: 1rem;
         background: rgba(78,161,255,0.12); border: 1px solid rgba(78,161,255,0.3); }
.flash.error { background: rgba(192,0,0,0.12); border-color: rgba(192,0,0,0.3); }
pre { background: var(--bg); padding: 0.6rem; border-radius: 6px;
      overflow-x: auto; font-size: 0.82rem; border: 1px solid var(--border); }
nav a { margin-right: 1rem; color: var(--accent); text-decoration: none; }
nav a:hover { text-decoration: underline; }
"""


def layout(title: str, body: str, flash: str = "", auto_refresh_seconds: int = 0) -> str:
    flash_html = f'<div class="flash">{flash}</div>' if flash else ""
    refresh_meta = (
        f'<meta http-equiv="refresh" content="{auto_refresh_seconds}">'
        if auto_refresh_seconds > 0 else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_meta}
<title>{title} — trading-bot v4</title>
<style>{BASE_CSS}</style>
</head>
<body>
<header>
  <h1>trading-bot v4 — operator dashboard</h1>
  <nav>
    <a href="/">Status</a>
    <a href="/risk">Risk profile</a>
    <a href="/strategy">Strategies</a>
    <a href="/digest">Digest</a>
    <a href="/halt">Halt / resume</a>
  </nav>
</header>
{flash_html}
{body}
</body>
</html>
"""


def status_page(snap: dict, auto_refresh_seconds: int = 30) -> str:
    halted = snap.get("halted")
    pill = '<span class="pill bad">HALTED</span>' if halted else '<span class="pill ok">RUNNING</span>'
    rth_pill = (
        '<span class="pill ok">RTH open</span>' if snap.get("rth_open")
        else '<span class="pill warn">market closed</span>'
    )
    kills = snap.get("active_kills") or []
    kills_html = (
        "<em>none active</em>" if not kills
        else "".join(f'<span class="pill bad">{k}</span> ' for k in kills)
    )

    # Account panel (latest equity + intraday P&L)
    acct = snap.get("account") or {}
    if acct:
        eq = acct.get("equity", 0.0)
        pnl = acct.get("intraday_pnl")
        pnl_pct = acct.get("intraday_pnl_pct")
        pnl_html = "<em>n/a (no opening snapshot)</em>"
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            color = "ok" if pnl >= 0 else "bad"
            pnl_html = f'<span class="pill {color}">{sign}${pnl:,.2f} ({sign}{pnl_pct:.2f}%)</span>'
        account_card = f"""
  <div class="card">
    <h2>Account</h2>
    <dl class="kv">
      <dt>equity</dt>           <dd>${eq:,.2f}</dd>
      <dt>cash</dt>             <dd>${acct.get('cash', 0):,.2f}</dd>
      <dt>buying power</dt>     <dd>${acct.get('buying_power', 0):,.2f}</dd>
      <dt>intraday P&amp;L</dt> <dd>{pnl_html}</dd>
      <dt>last snapshot</dt>    <dd><small class="muted">{acct.get('snapshot_ts','')}</small></dd>
    </dl>
  </div>
"""
    else:
        account_card = """
  <div class="card">
    <h2>Account</h2>
    <p><em>No account snapshots yet — daemon needs to tick at least once
    with broker credentials wired.</em></p>
  </div>
"""

    # Positions panel
    positions = snap.get("positions") or []
    if positions:
        pos_rows = "".join(
            f"<tr><td>{p['symbol']}</td>"
            f"<td>{p['qty']:.4f}</td>"
            f"<td>${p['avg_cost']:,.2f}</td>"
            f"<td>${p['market_price']:,.2f}</td>"
            f"<td>${p['market_value']:,.2f}</td>"
            f"<td>{p['asset_class']}</td>"
            f'<td><span class="pill {("ok" if p["classification"]=="bot" else "warn" if p["classification"]=="external" else "bad" if p["classification"]=="unknown" else "ok")}">{p["classification"]}</span></td>'
            f"</tr>"
            for p in positions
        )
        positions_panel = f"""
<div class="card" style="margin-top:1rem;">
  <h2>Current positions ({len(positions)})</h2>
  <table><thead>
    <tr><th>symbol</th><th>qty</th><th>avg cost</th><th>mark</th><th>value</th><th>class</th><th>tag</th></tr>
  </thead><tbody>{pos_rows}</tbody></table>
</div>
"""
    else:
        positions_panel = """
<div class="card" style="margin-top:1rem;">
  <h2>Current positions (0)</h2>
  <p><em>No positions snapshot yet, or account is flat.</em></p>
</div>
"""

    heartbeats = snap.get("heartbeats") or []
    hb_rows = "".join(
        f"<tr><td>{h['job_name']}</td>"
        f"<td>{h.get('last_run_et') or h['last_run_ts']}</td>"
        f'<td><span class="pill {("ok" if h["last_status"]=="ok" else "warn" if h["last_status"]=="skipped" else "bad")}">{h["last_status"]}</span></td>'
        f"<td><small class='muted'>{(h['last_detail'] or '')[:140]}</small></td>"
        f"<td>{h['last_duration_s']:.2f}s</td></tr>"
        for h in heartbeats
    ) or "<tr><td colspan=5><em>no heartbeats yet — daemon not running?</em></td></tr>"

    strat = snap.get("strategies") or []
    strat_rows = "".join(
        f"<tr><td>{s['strategy_id']}</td><td>v{s['version']}</td>"
        f'<td><span class="pill {("warn" if s["status"]=="research_only" else "ok")}">{s["status"]}</span></td></tr>'
        for s in strat
    ) or "<tr><td colspan=3><em>no strategies registered</em></td></tr>"

    body = f"""
<div class="grid">
  <div class="card">
    <h2>System</h2>
    <dl class="kv">
      <dt>state</dt>            <dd>{pill}</dd>
      <dt>market</dt>           <dd>{rth_pill}</dd>
      <dt>profile</dt>          <dd>{snap.get('current_profile','—')}</dd>
      <dt>active kills</dt>     <dd>{kills_html}</dd>
      <dt>now (ET)</dt>         <dd><small class="muted">{snap.get('ts_et', snap.get('ts',''))}</small></dd>
      <dt>ledger</dt>           <dd><small class="muted">{snap.get('ledger_db','?')}</small></dd>
    </dl>
  </div>
  {account_card}
  <div class="card">
    <h2>Strategies ({len(strat)})</h2>
    <table><thead><tr><th>id</th><th>ver</th><th>status</th></tr></thead>
    <tbody>{strat_rows}</tbody></table>
  </div>
</div>

{positions_panel}

<div class="card" style="margin-top:1rem;">
  <h2>Daemon heartbeats</h2>
  <table><thead>
    <tr><th>job</th><th>last run</th><th>status</th><th>detail</th><th>dur</th></tr>
  </thead><tbody>{hb_rows}</tbody></table>
</div>

<p><small class="muted">Auto-refresh every {auto_refresh_seconds}s. <a href="/">Refresh now</a>.</small></p>
"""
    return layout("Status", body, auto_refresh_seconds=auto_refresh_seconds)


def digest_page(d: dict) -> str:
    """Render the digest dict as a multi-card page."""
    acct = d.get("account") or {}
    if acct.get("n_snapshots"):
        pnl = acct.get("intraday_pnl", 0.0)
        sign = "+" if pnl >= 0 else ""
        color = "ok" if pnl >= 0 else "bad"
        acct_html = f"""
<dl class="kv">
  <dt>snapshots</dt>     <dd>{acct['n_snapshots']}</dd>
  <dt>opening</dt>       <dd>${acct['opening_equity']:,.2f} <small class="muted">({acct['opening_ts']})</small></dd>
  <dt>latest</dt>        <dd>${acct['latest_equity']:,.2f} <small class="muted">({acct['latest_ts']})</small></dd>
  <dt>P&amp;L</dt>       <dd><span class="pill {color}">{sign}${pnl:,.2f} ({sign}{acct.get('intraday_pnl_pct', 0):.2f}%)</span></dd>
  <dt>cash</dt>          <dd>${acct.get('latest_cash', 0):,.2f}</dd>
  <dt>buying power</dt>  <dd>${acct.get('latest_buying_power', 0):,.2f}</dd>
</dl>"""
    else:
        acct_html = "<p><em>No account snapshots in window.</em></p>"

    hbs = d.get("heartbeats") or []
    hb_rows = "".join(
        f"<tr><td>{h['job_name']}</td><td>{h['last_run_ts']}</td>"
        f'<td><span class="pill {("ok" if h["last_status"]=="ok" else "warn" if h["last_status"]=="skipped" else "bad")}">{h["last_status"]}</span></td>'
        f"<td><small class='muted'>{(h.get('last_detail') or '')[:120]}</small></td></tr>"
        for h in hbs
    ) or "<tr><td colspan=4><em>no heartbeats</em></td></tr>"

    ks = d.get("kill_switches") or []
    ks_rows = "".join(
        f"<tr><td>{k['event_ts']}</td>"
        f'<td><span class="pill {("bad" if k["event_kind"]=="fire" else "ok")}">{k["event_kind"]}</span></td>'
        f"<td>{k['detector']}</td><td>{k['actor']}</td>"
        f"<td><small class='muted'>{(k.get('reason') or '')[:120]}</small></td></tr>"
        for k in ks
    ) or "<tr><td colspan=5><em>no kill-switch events in window</em></td></tr>"

    orders = d.get("orders") or []
    fills = d.get("fills") or []
    orders_rows = "".join(
        f"<tr><td>{o['created_ts']}</td><td>{o['symbol']}</td>"
        f"<td>{o['side']}</td><td>{o['qty']}</td><td>{o.get('strategy_id','')}</td></tr>"
        for o in orders
    ) or "<tr><td colspan=5><em>no orders in window</em></td></tr>"
    fills_rows = "".join(
        f"<tr><td>{f['event_ts']}</td><td>{f['symbol']}</td>"
        f"<td>{f['qty']}</td><td>${f['price']}</td></tr>"
        for f in fills
    ) or "<tr><td colspan=4><em>no fills in window</em></td></tr>"

    body = f"""
<div class="card"><h2>Digest — last {d.get('window_hours', 24)}h</h2>
<p><small class="muted">since {d.get('since', '')}</small></p>
{acct_html}
</div>

<div class="card" style="margin-top:1rem;">
  <h2>Heartbeats</h2>
  <table><thead><tr><th>job</th><th>last run</th><th>status</th><th>detail</th></tr></thead>
  <tbody>{hb_rows}</tbody></table>
</div>

<div class="card" style="margin-top:1rem;">
  <h2>Kill-switch events ({len(ks)})</h2>
  <table><thead><tr><th>ts</th><th>kind</th><th>detector</th><th>actor</th><th>reason</th></tr></thead>
  <tbody>{ks_rows}</tbody></table>
</div>

<div class="grid" style="margin-top:1rem;">
  <div class="card"><h2>Orders ({len(orders)})</h2>
    <table><thead><tr><th>ts</th><th>symbol</th><th>side</th><th>qty</th><th>strategy</th></tr></thead>
    <tbody>{orders_rows}</tbody></table>
  </div>
  <div class="card"><h2>Fills ({len(fills)})</h2>
    <table><thead><tr><th>ts</th><th>symbol</th><th>qty</th><th>price</th></tr></thead>
    <tbody>{fills_rows}</tbody></table>
  </div>
</div>
"""
    return layout("Digest", body)


def risk_page(state: dict, flash: str = "") -> str:
    cur = state.get("current", "unknown")

    def diff_table(name, diffs):
        if not diffs:
            return f"<p><em>{name}: no changes from current.</em></p>"
        rows = "".join(
            f"<tr><td><code>{d['path']}</code></td>"
            f"<td>{d['old']}</td><td>{d['new']}</td>"
            f'<td><span class="pill {("warn" if d["direction"]=="loosen" else "ok")}">{d["direction"]}</span></td></tr>'
            for d in diffs
        )
        return (
            f"<h3 style='margin-top:1rem;'>{name}</h3>"
            f"<table><thead><tr><th>field</th><th>current</th><th>new</th><th>direction</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    body = f"""
<div class="card">
  <h2>Risk profile</h2>
  <p>Current: <strong>{cur}</strong></p>
  <p><small class="muted">
    Switching to a tighter profile takes effect immediately.
    Switching to a looser profile rewrites the lock but the kernel
    enforces a 7-day cooldown before honouring loosened thresholds
    (Plan v4 §4).
  </small></p>
  <form method="post" action="/risk/apply">
    <label>Choose profile:
      <select name="profile">
        <option value="safe" {"selected" if cur=="safe" else ""}>safe — tighter caps, smaller per-order risk</option>
        <option value="neutral" {"selected" if cur=="neutral" else ""}>neutral — v4 phase-2 defaults</option>
        <option value="aggressive" {"selected" if cur=="aggressive" else ""}>aggressive — looser caps (7-day cooldown applies)</option>
      </select>
    </label>
    <label>Operator note (audit trail): <input type="text" name="note" placeholder="why are you changing this?" required></label>
    <button class="btn primary" type="submit">Apply profile</button>
  </form>
</div>

<div class="card" style="margin-top:1rem;">
  <h2>Preview diffs vs each profile</h2>
  {diff_table("Safe", state.get('diffs_vs_safe', []))}
  {diff_table("Neutral", state.get('diffs_vs_neutral', []))}
  {diff_table("Aggressive", state.get('diffs_vs_aggressive', []))}
</div>
"""
    return layout("Risk profile", body, flash=flash)


def halt_page(snap: dict, flash: str = "") -> str:
    halted = snap.get("halted")
    pill = '<span class="pill bad">HALTED</span>' if halted else '<span class="pill ok">RUNNING</span>'

    if halted:
        form = """
<form method="post" action="/halt/resume">
  <label>Reason for resume:
    <input type="text" name="reason" required placeholder="e.g. data feed restored">
  </label>
  <button class="btn primary" type="submit">Resume trading</button>
</form>
"""
    else:
        form = """
<form method="post" action="/halt/halt" onsubmit="return confirm('Halt all new entries? Existing positions can still exit.');">
  <label>Reason for halt:
    <input type="text" name="reason" required placeholder="e.g. unexplained drawdown, vacation, news event">
  </label>
  <button class="btn danger" type="submit">HALT</button>
</form>
"""

    body = f"""
<div class="card">
  <h2>Manual halt</h2>
  <p>Current state: {pill}</p>
  <p><small class="muted">
    Halt fires the <code>manual_operator_halt</code> kill switch. New
    entries from every strategy are blocked. Exits and reduce-only
    operations pass through. The action is audited to the hash-chained
    <code>kill_switch_event</code> table.
  </small></p>
  {form}
</div>
"""
    return layout("Halt / resume", body, flash=flash)


def strategy_page(strategies: list[dict], flash: str = "") -> str:
    rows = "".join(
        f"<tr><td>{s['strategy_id']}</td><td>v{s['strategy_ver']}</td>"
        f"<td>{s['lane']}</td><td>{s['status']}</td><td>{s['owner']}</td>"
        f"<td><small class='muted'>{s.get('thesis_id','')}</small></td></tr>"
        for s in strategies
    ) or "<tr><td colspan=6><em>no strategies registered</em></td></tr>"

    body = f"""
<div class="card">
  <h2>Submit a new strategy</h2>
  <p><small class="muted">
    All submissions land at <code>research_only</code>. To promote, run the
    research factory and produce a Tier-1 validation artifact.
  </small></p>
  <form method="post" action="/strategy/submit">
    <label>Name (short, alphanumeric):
      <input type="text" name="name" required placeholder="e.g. MEAN_REV_v1">
    </label>
    <label>Hypothesis (plain English — what edge, why, when it works, when it breaks):
      <textarea name="description" required placeholder="Describe the alpha you think exists. Include: the mechanism, the regimes you expect it to work in, and the kill criteria (when do you give up)."></textarea>
    </label>
    <label>Processing mode:
      <select name="mode">
        <option value="draft">draft — register only, no AI (fastest)</option>
        <option value="intake">intake — adversarial pair (Bull persona + Bear persona)</option>
        <option value="mutate">mutate — full mutation cycle (heavier, needs search space + budget)</option>
      </select>
    </label>
    <button class="btn primary" type="submit">Submit hypothesis</button>
  </form>
</div>

<div class="card" style="margin-top:1rem;">
  <h2>Registered strategies</h2>
  <table><thead>
    <tr><th>id</th><th>ver</th><th>lane</th><th>status</th><th>owner</th><th>thesis</th></tr>
  </thead><tbody>{rows}</tbody></table>
</div>
"""
    return layout("Strategies", body, flash=flash)


def strategy_result_page(result: dict) -> str:
    pretty = "<pre>" + _escape(_pretty_json(result)) + "</pre>"
    body = f"""
<div class="card">
  <h2>Submission result</h2>
  {pretty}
  <p><a href="/strategy" class="btn">Back to strategies</a></p>
</div>
"""
    return layout("Submission result", body)


def _pretty_json(d) -> str:
    import json
    return json.dumps(d, indent=2, default=str)


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


__all__ = [
    "BASE_CSS", "digest_page", "halt_page", "layout", "risk_page",
    "status_page", "strategy_page", "strategy_result_page",
]
