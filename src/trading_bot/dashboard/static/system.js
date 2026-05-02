// System view — node flash on bus event, supervision-zone toggle,
// periodic node-health refresh.
//
// Each .system-node element has data-events="evt1,evt2,..." listing the
// bus event types it cares about. We listen at document.body (the
// event_bus.js bridge dispatches there) and toggle data-flash on the
// matching nodes for ~1.5s.
(function () {
  function flash(node) {
    if (!node) return;
    node.setAttribute("data-flash", "1");
    clearTimeout(node._flashTimeout);
    node._flashTimeout = setTimeout(() => node.removeAttribute("data-flash"), 1500);
  }

  function buildEventIndex() {
    const index = new Map();  // event-type -> [node element, ...]
    document.querySelectorAll(".system-node[data-events]").forEach((n) => {
      const events = (n.dataset.events || "").split(",").map(s => s.trim()).filter(Boolean);
      events.forEach((e) => {
        if (!index.has(e)) index.set(e, []);
        index.get(e).push(n);
      });
    });
    return index;
  }

  function bindFlashListeners() {
    const index = buildEventIndex();
    index.forEach((nodes, eventType) => {
      document.body.addEventListener(eventType, () => nodes.forEach(flash));
    });
  }

  function bindZoneToggle() {
    document.querySelectorAll('[data-action="toggle-zone"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        const zoneId = btn.dataset.zone;
        const sec = document.querySelector(`section[data-zone="${zoneId}"]`);
        if (!sec) return;
        const body = sec.querySelector(".zone-body");
        sec.classList.toggle("collapsed");
        if (body) body.toggleAttribute("hidden");
      });
    });
  }

  function watchIncidents() {
    const strip = document.getElementById("incidents");
    if (!strip) return;
    const items = new Map();  // node-id -> {kind, since, label}
    const render = () => {
      if (items.size === 0) {
        strip.classList.add("hidden");
        strip.innerHTML = "";
        return;
      }
      strip.classList.remove("hidden");
      const rows = [];
      for (const [nid, it] of items) {
        const ageS = Math.max(0, Math.floor((Date.now() - it.since) / 1000));
        const ageStr = ageS < 60 ? `${ageS}s` : `${Math.floor(ageS / 60)}m`;
        rows.push(`<div>● <strong>${it.kind}</strong> ${nid} <span class="text-rose-300/70">${ageStr} ago</span> ${it.label || ""}</div>`);
      }
      strip.innerHTML = rows.join("");
    };
    // role.failed / role.stalled — surface; node turns red via /fragment/system_nodes refresh.
    document.body.addEventListener("role.failed", (e) => {
      const role = (e.detail?.payload?.role) || "(unknown)";
      items.set(`role.${role}`, { kind: "FAIL", since: Date.now(), label: e.detail?.payload?.error || "" });
      render();
    });
    document.body.addEventListener("role.stalled", (e) => {
      const role = (e.detail?.payload?.role) || "(unknown)";
      items.set(`stall.${role}`, { kind: "STALL", since: Date.now(), label: "" });
      render();
    });
    // Periodic age refresh.
    setInterval(render, 5000);
  }

  function refreshNodeHealth() {
    fetch("/fragment/system_nodes", { headers: { Accept: "application/json" } })
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data || !data.nodes) return;
        for (const [nid, info] of Object.entries(data.nodes)) {
          const el = document.getElementById("node-" + nid);
          if (!el) continue;
          if (info.health) el.dataset.health = info.health;
          const last = el.querySelector(".last-activity");
          if (last && info.last_activity_label) {
            last.textContent = "last " + info.last_activity_label;
          }
        }
      })
      .catch(() => {});
  }

  function openDrilldown(nodeId) {
    const panel = document.getElementById("drilldown");
    const body = document.getElementById("drilldown-body");
    if (!panel || !body) return;
    body.innerHTML = '<div class="text-slate-400">Loading…</div>';
    panel.style.transform = "translateY(0)";
    panel.setAttribute("aria-hidden", "false");
    fetch("/fragment/node/" + encodeURIComponent(nodeId))
      .then((r) => r.ok ? r.text() : Promise.reject(r.status))
      .then((html) => { body.innerHTML = html; })
      .catch((e) => { body.innerHTML = '<div class="text-rose-400">Error loading node ' + nodeId + ': ' + e + '</div>'; });
  }

  function closeDrilldown() {
    const panel = document.getElementById("drilldown");
    if (!panel) return;
    panel.style.transform = "translateY(100%)";
    panel.setAttribute("aria-hidden", "true");
  }

  function bindNodeClicks() {
    document.querySelectorAll(".system-node:not(.passive)").forEach((n) => {
      n.addEventListener("click", () => openDrilldown(n.dataset.nodeId));
      n.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openDrilldown(n.dataset.nodeId);
        }
      });
    });
    document.body.addEventListener("click", (e) => {
      const t = e.target;
      if (t && t.dataset && t.dataset.action === "close-drilldown") closeDrilldown();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrilldown();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindFlashListeners();
    bindZoneToggle();
    watchIncidents();
    refreshNodeHealth();
    bindNodeClicks();
    setInterval(refreshNodeHealth, 30000);
  });
})();
