// Browser-side event bus bridge.
//
// Opens an EventSource against /api/stream and re-dispatches every named
// event onto document.body as a CustomEvent. HTMX fragments listen via
//   hx-trigger="<event_name> from:body, every 120s"
// so SSE-arrival triggers an immediate refresh and polling stays as fallback.
//
// Reconnect: EventSource auto-reconnects with the server's `retry` value
// (we send retry: 5000 + jitter). Last-Event-ID is sent automatically on
// reconnect so we don't miss events queued during the gap.
//
// Connection status: a small pill in the header reflects state via
//   data-stream-status="live|reconn|down|off"
// on document.body. CSS picks this up.
(function () {
  if (!window.EventSource) {
    document.body.dataset.streamStatus = "off";
    return;
  }

  const STREAM_URL = "/api/stream";
  const status = (s) => { document.body.dataset.streamStatus = s; };

  let es = null;
  let reconnectAttempts = 0;
  let lastEventId = 0;

  // List of event types the dashboard cares about. We bind a listener
  // for each so the EventSource named-event matching works. (Generic
  // "message" listener doesn't fire for events with `event: <name>`.)
  // Producers may emit events outside this list — they'll still be
  // visible in /api/stream but won't trigger DOM dispatch.
  const KNOWN_EVENTS = [
    // Trading
    "order.placed", "order.filled", "order.partial_fill",
    "order.canceled", "order.rejected", "order.submitted",
    "position.changed", "trade.closed",
    // Decision
    "decision.created", "lesson.created",
    "debate.risk.completed", "debate.unblock.completed", "debate.promotion.completed",
    // Discovery
    "scan.completed", "intel.updated", "scout.completed",
    "opportunities.updated", "wheel.universe.refreshed", "wheel.cycle.changed",
    "wheel.scan.heartbeat",
    // Learning
    "threshold.updated", "lab.evolution.completed", "calibrator.completed",
    // LLM routing
    "mailbox.brief.submitted", "mailbox.brief.completed", "mailbox.brief.failed",
    // Process
    "role.completed", "role.failed", "role.stalled",
    "heartbeat.tick", "process.started", "process.stopped",
    // Activity
    "activity.appended",
    // Email
    "email.sent",
    // Bus health (dashboard self-emitted)
    "stream.hello",
    // Live market-data ticks (Phase 8) — ephemeral, not persisted.
    // Single named event "price.update" carries {symbol, price, ts} so
    // we don't have to listen per-symbol; DOM cells with data-tick-symbol
    // are updated in place. See updatePriceCell below.
    "price.update",
  ];

  // Per-symbol last price (for tick direction coloring).
  const lastPriceBySymbol = new Map();

  function fmtUsd(v) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function updatePriceCell(payload) {
    if (!payload || !payload.symbol || payload.price == null) return;
    const sym = String(payload.symbol).toUpperCase();
    const price = Number(payload.price);
    const prev = lastPriceBySymbol.get(sym);
    lastPriceBySymbol.set(sym, price);
    const cells = document.querySelectorAll('[data-tick-symbol="' + sym + '"]');
    if (!cells.length) return;
    const flashClass = prev == null ? null : (price > prev ? "tick-up" : (price < prev ? "tick-down" : null));
    cells.forEach((c) => {
      c.textContent = fmtUsd(price);
      c.dataset.tickSource = "live";
      if (flashClass) {
        c.classList.remove("tick-up", "tick-down");
        // Force reflow so the same class can re-trigger the animation.
        // eslint-disable-next-line no-unused-expressions
        c.offsetWidth;
        c.classList.add(flashClass);
      }
    });
  }

  function dispatch(name, body) {
    try { if (body.id) lastEventId = Math.max(lastEventId, body.id); } catch (e) { /* ignore */ }
    if (name === "price.update") {
      updatePriceCell(body && body.payload);
    }
    document.body.dispatchEvent(new CustomEvent(name, { detail: body, bubbles: true }));
    // Also fire a generic "stream:event" so observers can see everything.
    document.body.dispatchEvent(new CustomEvent("stream:event", { detail: body, bubbles: true }));
  }

  function bindListeners(source) {
    KNOWN_EVENTS.forEach((name) => {
      source.addEventListener(name, (ev) => {
        let body = {};
        try { body = JSON.parse(ev.data); } catch (e) { /* ignore */ }
        dispatch(name, body);
      });
    });
  }

  function connect() {
    try { if (es) es.close(); } catch (e) { /* ignore */ }
    status(reconnectAttempts === 0 ? "live" : "reconn");
    es = new EventSource(STREAM_URL);
    bindListeners(es);

    es.onopen = () => {
      reconnectAttempts = 0;
      status("live");
    };
    es.onerror = () => {
      // Browser will auto-reconnect using server's `retry` value. We
      // just reflect the state.
      reconnectAttempts += 1;
      status(reconnectAttempts > 2 ? "down" : "reconn");
    };
  }

  // Pause the stream while the tab is hidden — saves bandwidth and a
  // long-tail of dropped events on background tabs. Resume on visible.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      try { if (es) es.close(); } catch (e) { /* ignore */ }
      status("off");
    } else {
      connect();
    }
  });

  connect();

  // Expose for debugging from devtools.
  window.__tradingBus = {
    status: () => document.body.dataset.streamStatus,
    lastEventId: () => lastEventId,
    reconnect: connect,
  };
})();
