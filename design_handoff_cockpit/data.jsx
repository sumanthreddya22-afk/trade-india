// ============================================================
// data.jsx — mock state for the cockpit
// All numbers are illustrative; in the real app they'd be
// fetched from /api/status, /api/snapshot, /api/equity-curve.
// Marked [mock] in the UI where it matters.
// ============================================================

const LANES = [
  { key: "stocks",  name: "ETF Momentum", short: "Stocks", enabled: true,  exposure_pct: 0.4118, cap_pct: 0.85 },
  { key: "crypto",  name: "Crypto",        short: "Crypto", enabled: true,  exposure_pct: 0.1502, cap_pct: 0.15 },
  { key: "options", name: "Wheel",         short: "Options", enabled: false, exposure_pct: 0.0,    cap_pct: 0.20 },
];

const STATUS_BASE = {
  system_state: "running",   // running | degraded | halted | down
  halted: { active: false, reason: null, since: null, operator: null },
  risk_profile: "neutral",   // safe | neutral | aggressive
  account: {
    equity: 54317.42,
    cash: 12104.81,
    day_pl_abs: 184.0,
    day_pl_pct: 0.0034,
    buying_power: 24209.62
  },
  lanes: LANES,
  kill_switches: [
    { name: "manual_operator_halt", active: false },
    { name: "crypto_cap_breach",    active: false },
    { name: "data_staleness",       active: false },
    { name: "pdt_breach",           active: false },
    { name: "drawdown_2pct",        active: false },
    { name: "drift_threshold",      active: false },
    { name: "lock_mismatch",        active: false },
    { name: "ledger_chain_fail",    active: false },
  ],
  boot_check: { ok: true, hash_verified_at: "2026-05-15T13:02:11Z" }
};

const REGIME = {
  label: "chop",
  since: "2026-05-13",
  signals: [
    { name: "VIX",            val: "17.2",   trend: "flat" },
    { name: "SPY 20d trend",  val: "+0.6%",  trend: "flat" },
    { name: "Breadth (A/D)",  val: "0.92",   trend: "down" },
    { name: "Yield curve",    val: "-12bps", trend: "flat" },
    { name: "Crypto corr",    val: "0.31",   trend: "up"   },
  ]
};

const RISK_CAPS = [
  { name: "account_exposure", used: 0.62,  cap: 0.85,  unit: "%" },
  { name: "stocks_lane",      used: 0.41,  cap: 0.70,  unit: "%" },
  { name: "crypto",           used: 0.150, cap: 0.150, unit: "%" },   // AT CAP
  { name: "options_lane",     used: 0.0,   cap: 0.20,  unit: "%" },
  { name: "pdt_count",        used: 1,     cap: 3,     unit: "#" },
  { name: "single_name",      used: 0.085, cap: 0.10,  unit: "%" },
];

const STRATEGY_MODE = [
  { name: "ETF_MOMENTUM_v1#a3f2", state: "armed",        lane: "stocks",  hash: "a3f2…b1" },
  { name: "ETF_MEANREV_v0#7c1e",  state: "paused",       lane: "stocks",  hash: "7c1e…d4" },
  { name: "BTC_TREND_v2#9f04",    state: "armed",        lane: "crypto",  hash: "9f04…22" },
  { name: "ETH_BREAKOUT_v1#1aab", state: "research_only",lane: "crypto",  hash: "1aab…fe" },
  { name: "WHEEL_SPY_v0#0001",    state: "research_only",lane: "options", hash: "0001…00" },
];

const POSITIONS = [
  // STOCKS
  { symbol: "SPY",  lane: "stocks",  qty: 28, entry: 521.10, mark: 524.88, pl_abs: 105.84, pl_pct: 0.00725,  classification: "bot",       stop: 514.50, opened_at: "2026-05-12T13:42:00Z", order_uid: "ord_01HK6T4S2P9", strategy_version: "ETF_MOMENTUM_v1#a3f2", drift_bps: 1.2 },
  { symbol: "QQQ",  lane: "stocks",  qty: 12, entry: 478.32, mark: 481.04, pl_abs:  32.64, pl_pct: 0.00569,  classification: "bot",       stop: 470.10, opened_at: "2026-05-13T15:12:00Z", order_uid: "ord_01HK7P21XK1", strategy_version: "ETF_MOMENTUM_v1#a3f2", drift_bps: 0.4 },
  { symbol: "IWM",  lane: "stocks",  qty: 40, entry: 218.05, mark: 216.21, pl_abs: -73.60, pl_pct: -0.00844, classification: "bot",       stop: 212.80, opened_at: "2026-05-14T13:30:00Z", order_uid: "ord_01HK8DN4GG2", strategy_version: "ETF_MOMENTUM_v1#a3f2", drift_bps: 2.1 },
  { symbol: "AAPL", lane: "stocks",  qty: 50, entry: 198.40, mark: 200.16, pl_abs:  88.00, pl_pct: 0.00887,  classification: "unknown",   stop: null,   opened_at: "2026-05-15T09:14:00Z", order_uid: null,             strategy_version: null,                     drift_bps: null },
  // CRYPTO
  { symbol: "BTC/USD", lane: "crypto", qty: 0.082, entry: 71204.10, mark: 71618.40, pl_abs: 33.97,  pl_pct: 0.00581,  classification: "bot",      stop: 70100.0, opened_at: "2026-05-11T01:05:00Z", order_uid: "ord_01HK3VV12QR", strategy_version: "BTC_TREND_v2#9f04", drift_bps: 0.8 },
  { symbol: "ETH/USD", lane: "crypto", qty: 1.40,  entry: 3812.50,  mark: 3791.20,  pl_abs: -29.82, pl_pct: -0.00559, classification: "bot",      stop: 3720.0,  opened_at: "2026-05-12T11:35:00Z", order_uid: "ord_01HK4LF2J1B", strategy_version: "BTC_TREND_v2#9f04", drift_bps: 1.6 },
  // OPTIONS — empty (Wheel not active)
];

const OPEN_ORDERS = [
  { symbol: "SPY",     lane: "stocks", side: "BUY",  qty: 5,  type: "MKT",  status: "pending_new",   age_s: 4,   idempotency: "idm_1f0a8e", client_order_id: "co_92a17b" },
  { symbol: "IWM",     lane: "stocks", side: "SELL", qty: 40, type: "STP",  status: "working",       age_s: 1820, idempotency: "idm_4cc921", client_order_id: "co_55ed11", canceled: false },
  { symbol: "BTC/USD", lane: "crypto", side: "SELL", qty: 0.012, type: "LMT", status: "stuck",       age_s: 78,  idempotency: "idm_b1c2d3", client_order_id: "co_e91029", stuck: true },
  { symbol: "QQQ",     lane: "stocks", side: "BUY",  qty: 8,  type: "LMT",  status: "canceled",      age_s: 240, idempotency: "idm_dde001", client_order_id: "co_31eea1", canceled: true },
];

const ACTION_REQUIRED = [
  { id: "ar_unknown", severity: "high", title: "Unknown position: AAPL × 50",
    cause: "Detected at 09:14 by reconciliation pass. Not in ledger. Kernel halts on unknown in Phase 2+.",
    cta: [{ label: "Classify", primary: true }, { label: "Close position" }] },
  { id: "ar_recon",   severity: "high", title: "Reconciliation mismatch",
    cause: "Order ord_01HK6T4S2P9 — 1 fill local, 2 broker. ∆ qty = +28.",
    cta: [{ label: "Open reconciliation", primary: true }, { label: "Dismiss" }] },
  { id: "ar_lock",    severity: "med",  title: "Validation lock loosened",
    cause: "lock_validation_v3 was downgraded — 7-day cooldown until 2026-05-22.",
    cta: [{ label: "Review", primary: true }] },
];

// activity items — most recent at index 0
const SEED_ACTIVITY = [
  { ts: "13:02:11", seq: 28411, type: "fill",   lane: "stocks", msg: "SPY filled 28 @ 521.10 → ord_01HK6T4S2P9" },
  { ts: "12:58:02", seq: 28410, type: "submit", lane: "stocks", msg: "submit SPY 28 MKT (ETF_MOMENTUM_v1#a3f2)" },
  { ts: "12:55:41", seq: 28409, type: "scan",   lane: "stocks", msg: "scan/stocks: 412 considered → 7 passed → 1 submitted" },
  { ts: "12:50:30", seq: 28408, type: "heart",  lane: null,     msg: "daemon heartbeat ok · pid 41882 · uptime 47h12m" },
  { ts: "12:42:18", seq: 28407, type: "policy", lane: null,     msg: "policy: lock_validation_v3 loosened (signer: operator)" },
  { ts: "12:30:00", seq: 28406, type: "scan",   lane: "crypto", msg: "scan/crypto: 18 considered → 2 passed → 0 submitted (cap reached)" },
  { ts: "12:21:55", seq: 28405, type: "skip",   lane: "crypto", msg: "skip BTC/USD — lane at exposure cap (15.0%)" },
  { ts: "12:11:04", seq: 28404, type: "fill",   lane: "crypto", msg: "ETH/USD partial 0.40 @ 3812.50" },
  { ts: "12:08:47", seq: 28403, type: "mutate", lane: "stocks", msg: "mutation proposed: ETF_MOMENTUM lookback 20→22 (research_only)" },
  { ts: "12:01:00", seq: 28402, type: "heart",  lane: null,     msg: "ledger chain verified · seq 28402 · hash 7c1a…ee" },
  { ts: "11:58:11", seq: 28401, type: "submit", lane: "stocks", msg: "submit QQQ 12 MKT (ETF_MOMENTUM_v1#a3f2)" },
  { ts: "11:55:00", seq: 28400, type: "cancel", lane: "stocks", msg: "cancel QQQ 8 LMT — replaced (co_31eea1)" },
];

const EQUITY_CURVE = (() => {
  // 90 daily-ish points climbing from 49500 to ~54300 with some chop
  const pts = [];
  let v = 49500;
  const start = new Date("2026-02-15T20:00:00Z").getTime();
  const dayMs = 24 * 3600 * 1000;
  for (let i = 0; i < 90; i++) {
    const t = start + i * dayMs;
    // slow upward drift with noise & a couple of bigger moves
    const drift = 35 + Math.sin(i / 7) * 60;
    const noise = (Math.sin(i * 1.91) + Math.cos(i * 2.7)) * 90;
    v += drift + noise + (i === 32 ? -380 : 0) + (i === 67 ? 260 : 0);
    pts.push({ ts: new Date(t).toISOString(), equity: Math.round(v * 100) / 100 });
  }
  return {
    range: "3m",
    points: pts,
    markers: [
      { i: 22, kind: "halt",    label: "manual halt 03-09" },
      { i: 47, kind: "profile", label: "safe → neutral" },
      { i: 78, kind: "lock",    label: "validation lock v3" },
    ]
  };
})();

const EXPOSURE_BREAKDOWN = [
  { name: "ETF Momentum", value: 0.4118, color: "var(--info)" },
  { name: "Crypto",       value: 0.1502, color: "var(--warn)" },
  { name: "Wheel",        value: 0.0,    color: "var(--text-faint)" },
  { name: "Cash",         value: 0.4380, color: "var(--text-faint)" },
];

// =======================  Recent Activity  =======================
const DAILY_DIGEST = {
  date: "2026-05-15",
  stats: [
    { label: "Equity Δ 24h",      value: "+$184",  sub: "+0.34%", up: true },
    { label: "Fills",             value: "7",      sub: "5 entries · 2 exits" },
    { label: "Orders submitted",  value: "12",     sub: "1 stuck · 1 canceled" },
    { label: "Scans",             value: "48",     sub: "all 3 lanes" },
    { label: "Mutations proposed",value: "3",      sub: "0 promoted" },
    { label: "Halts",             value: "0",      sub: "0 unresolved", up: true },
  ]
};

const DECISIONS = [
  { id: "d1", time: "13:02", strategy: "ETF_MOMENTUM_v1#a3f2", symbol: "SPY",     action: "entry",   reason: "passed mom_20 > 1.4σ; vol filter ok; lane cap headroom 28%", seq: 28411,
    gates: [
      { name: "mom_20",        ok: true,  val: "+1.62σ", thresh: "≥ 1.4σ" },
      { name: "vol_filter",    ok: true,  val: "12.4",  thresh: "≤ 18.0" },
      { name: "lane_cap",      ok: true,  val: "62%",   thresh: "< 85%" },
      { name: "single_name",   ok: true,  val: "5.3%",  thresh: "< 10%" },
      { name: "regime",        ok: true,  val: "chop",  thresh: "any" },
    ]},
  { id: "d2", time: "12:21", strategy: "BTC_TREND_v2#9f04",    symbol: "BTC/USD", action: "skip",    reason: "crypto lane at exposure cap (15.0% / 15.0%)", seq: 28405,
    gates: [
      { name: "trend_score",   ok: true,  val: "0.71",  thresh: "≥ 0.55" },
      { name: "lane_cap",      ok: false, val: "15.0%", thresh: "< 15.0%" },
    ]},
  { id: "d3", time: "11:55", strategy: "ETF_MOMENTUM_v1#a3f2", symbol: "QQQ",     action: "entry",   reason: "passed mom_20; replaced stale LMT with MKT", seq: 28400,
    gates: [
      { name: "mom_20",        ok: true,  val: "+1.81σ", thresh: "≥ 1.4σ" },
      { name: "stale_order",   ok: true,  val: "240s",  thresh: "auto-replace" },
    ]},
  { id: "d4", time: "10:42", strategy: "ETF_MEANREV_v0#7c1e",  symbol: "XLE",     action: "exit",    reason: "stop touched (-0.81%) before reversal threshold", seq: 28391,
    gates: [
      { name: "stop",          ok: true,  val: "-0.81%", thresh: "-0.8%" },
    ]},
  { id: "d5", time: "10:08", strategy: "ETF_MOMENTUM_v1#a3f2", symbol: "XLF",     action: "skip",    reason: "drift gauge 3.2bps over 20-trade window — cooldown", seq: 28384,
    gates: [
      { name: "drift_gauge",   ok: false, val: "3.2bps", thresh: "< 2.5bps" },
    ]},
  { id: "d6", time: "09:15", strategy: "n/a",                  symbol: "—",       action: "mut-rej", reason: "ETH_BREAKOUT λ-mutation failed BH-FDR (p=0.21)", seq: 28371 },
  { id: "d7", time: "09:02", strategy: "ETF_MOMENTUM_v1#a3f2", symbol: "—",       action: "mut-ok",  reason: "lookback 20→22 survives walk-forward, awaiting Tier 3", seq: 28368 },
];

const LESSONS = [
  { ts: "2026-05-15 12:45", tag: "stocks",
    body: "QQQ entry today underran by 3bps vs. expected fill. Liquidity rebated faster than the cost model anticipates around the noon lull. Will widen the broker_paper → pessimistic gap by 1bp for QQQ-class symbols and re-run the calibrator." },
  { ts: "2026-05-14 18:10", tag: "crypto",
    body: "Crypto lane sat at cap all day. Either raise cap (no — risk policy) or rotate the trend-score: today's regime is chop, BTC_TREND is overweight bull. Queued a mutation proposal: gate trend signal on regime∈{bullish}." },
  { ts: "2026-05-13 09:30", tag: "system",
    body: "Daemon restart at 01:30 lost 2m of partial fills. Reconciliation caught all of it within 8m; ledger chain held. Filed work item: persist partial-fill watermark to wal every fill, not every 30s." },
];

const LAST_SCAN = {
  ts: "13:02:11Z",
  cycle_ms: 412,
  funnel: [
    { name: "considered",  val: 412 },
    { name: "liquidity",   val: 287 },
    { name: "regime",      val: 142 },
    { name: "mom_20",      val:  31 },
    { name: "vol_filter",  val:  18 },
    { name: "single_name", val:  11 },
    { name: "lane_cap",    val:   7 },
    { name: "submitted",   val:   1 },
  ]
};

// =======================  Strategy Lab  =======================
const STRATEGIES = [
  { name: "ETF_MOMENTUM_v1",   hash: "a3f2…b1", lane: "stocks",  state: "paper",         tier: 3, p_sharpe: 1.28, d_sharpe: 0.94, pbo: 0.18, last_run: "13:02", live_eligible: true },
  { name: "ETF_MEANREV_v0",    hash: "7c1e…d4", lane: "stocks",  state: "research_only", tier: 2, p_sharpe: 0.78, d_sharpe: 0.42, pbo: 0.34, last_run: "11:48" },
  { name: "BTC_TREND_v2",      hash: "9f04…22", lane: "crypto",  state: "paper",         tier: 3, p_sharpe: 1.51, d_sharpe: 1.04, pbo: 0.22, last_run: "12:30", live_eligible: true },
  { name: "ETH_BREAKOUT_v1",   hash: "1aab…fe", lane: "crypto",  state: "research_only", tier: 1, p_sharpe: 0.42, d_sharpe: 0.10, pbo: 0.58, last_run: "09:15" },
  { name: "WHEEL_SPY_v0",      lane: "options", hash: "0001…00", state: "research_only", tier: 1, p_sharpe: 0.61, d_sharpe: 0.20, pbo: 0.46, last_run: "08:00" },
  { name: "ETF_MOMENTUM_v0",   hash: "1119…aa", lane: "stocks",  state: "retired",       tier: 2, p_sharpe: 0.95, d_sharpe: 0.55, pbo: 0.30, last_run: "2026-04-21" },
  { name: "BTC_VOL_BAND_v0",   hash: "5fac…02", lane: "crypto",  state: "retired",       tier: 1, p_sharpe: 0.18, d_sharpe: -0.04, pbo: 0.62, last_run: "2026-04-08" },
];

const WF_FOLDS = [
  { name: "Fold 1", sharpe: 1.31, up: true },
  { name: "Fold 2", sharpe: 1.18, up: true },
  { name: "Fold 3", sharpe: 0.94, up: true },
  { name: "Fold 4", sharpe: 1.42, up: true },
  { name: "Fold 5", sharpe: 1.07, up: true },
  { name: "Holdout", sharpe: 1.21, up: true, locked: true },
];

const HEATMAP = (() => {
  // 11 × 11 parameter plateau, with center plateau near max
  const m = [];
  for (let y = 0; y < 11; y++) {
    const row = [];
    for (let x = 0; x < 11; x++) {
      const dx = (x - 5) / 5; const dy = (y - 5) / 5;
      const d2 = dx * dx + dy * dy;
      const v = Math.max(0, 1.4 * Math.exp(-d2 * 1.4) + (Math.sin(x * 1.3) + Math.cos(y * 1.7)) * 0.06);
      row.push(Math.round(v * 100) / 100);
    }
    m.push(row);
  }
  return m;
})();

const MUTATIONS = [
  { time: "13:02", strat: "ETF_MOMENTUM_v1", param: "lookback 20→22", tag: "survived", p: "0.012" },
  { time: "12:08", strat: "ETF_MOMENTUM_v1", param: "lookback 20→25", tag: "proposed", p: "—"     },
  { time: "11:51", strat: "ETF_MOMENTUM_v1", param: "vol_thresh 18→16", tag: "rejected", p: "0.31"  },
  { time: "10:14", strat: "ETF_MOMENTUM_v1", param: "regime gate {bull}", tag: "rejected", p: "0.27" },
  { time: "09:02", strat: "ETF_MOMENTUM_v1", param: "exit 1.0σ→0.8σ",   tag: "survived", p: "0.041" },
  { time: "Yest.",  strat: "ETF_MOMENTUM_v1", param: "single_name 10%→8%", tag: "rejected", p: "0.18"  },
];

const PROMOTION_QUEUE = [
  { name: "ETF_MOMENTUM_v1#a3f2", lane: "stocks", p_sharpe: 1.28, d_sharpe: 0.94, pbo: 0.18 },
  { name: "BTC_TREND_v2#9f04",    lane: "crypto", p_sharpe: 1.51, d_sharpe: 1.04, pbo: 0.22 },
];

const LLM_SPEND = {
  today_total: 8.42,
  month_total: 142.18,
  budget_month: 200.00,
  roles: [
    { role: "Judge",     model: "Opus",   today: 4.12, share: 0.49, color: "opus"    },
    { role: "Reviewer",  model: "Sonnet", today: 2.66, share: 0.32, color: "sonnet"  },
    { role: "Mutator",   model: "Sonnet", today: 1.21, share: 0.14, color: "sonnet"  },
    { role: "Postmortem",model: "Haiku",  today: 0.43, share: 0.05, color: "haiku"   },
  ]
};

// =======================  System Health  =======================
const JOBS = [
  { name: "scan/stocks",      schedule: "*/5 * * * *",   last: "13:02:11", dur_ms: 412, next_s: 217, status: "ok"  },
  { name: "scan/crypto",      schedule: "*/2 * * * *",   last: "13:02:00", dur_ms: 286, next_s:  44, status: "ok"  },
  { name: "scan/options",     schedule: "0 13 * * *",    last: "13:00:00", dur_ms: 138, next_s: 86220, status: "ok"  },
  { name: "reconcile/broker", schedule: "*/1 * * * *",   last: "13:02:30", dur_ms:  92, next_s:  15, status: "ok"  },
  { name: "drift/recompute",  schedule: "*/10 * * * *",  last: "12:50:01", dur_ms: 1810, next_s: 480, status: "ok"  },
  { name: "ledger/verify",    schedule: "*/30 * * * *",  last: "12:30:01", dur_ms: 4202, next_s: 1620, status: "ok"  },
  { name: "research/mutator", schedule: "0 */2 * * *",   last: "12:00:00", dur_ms: 38420, next_s: 3380, status: "ok"  },
  { name: "research/judge",   schedule: "0 4 * * *",     last: "04:00:00", dur_ms: 612000, next_s: 54300, status: "ok"  },
  { name: "snapshot/equity",  schedule: "*/15 * * * *",  last: "13:00:00", dur_ms:  18, next_s: 720, status: "ok"  },
  { name: "alpaca/heartbeat", schedule: "*/30 * * * *",  last: "12:45:00", dur_ms: 850, next_s: 990, status: "fail", err: "TLS handshake timeout (8s)" },
];

const FRESHNESS = [
  { src: "stocks_eod",      last: "16:00:00 (-1d)", cadence: "1d",  lag_s: 0,    ok: true  },
  { src: "stocks_1m",       last: "13:02:00",       cadence: "1m",  lag_s: 12,   ok: true  },
  { src: "crypto_1m",       last: "13:02:09",       cadence: "1m",  lag_s: 3,    ok: true  },
  { src: "crypto_orderbook",last: "13:02:11",       cadence: "1s",  lag_s: 1,    ok: true  },
  { src: "options_chain",   last: "13:01:00",       cadence: "5m",  lag_s: 132,  ok: true  },
  { src: "macro_vix",       last: "12:58:00",       cadence: "1m",  lag_s: 252,  ok: false, why: "stale" },
];

const POLICY_LOCKS = [
  { name: "lock_risk_caps_v2",  ver: "v2.4.1", changed: "2026-05-08", signer: "operator", status: "verified" },
  { name: "lock_kill_switches", ver: "v1.2.0", changed: "2026-04-29", signer: "operator", status: "verified" },
  { name: "lock_validation_v3", ver: "v3.0.2", changed: "2026-05-13", signer: "operator", status: "verified" },
  { name: "lock_lane_caps",     ver: "v1.0.4", changed: "2026-05-01", signer: "operator", status: "verified" },
  { name: "lock_cost_model",    ver: "v0.9.0", changed: "2026-05-11", signer: "operator", status: "verified" },
  { name: "lock_regime_def",    ver: "v2.1.0", changed: "2026-04-19", signer: "operator", status: "verified" },
  { name: "lock_pdt_policy",    ver: "v1.0.0", changed: "2026-03-02", signer: "operator", status: "verified" },
  { name: "lock_data_sources",  ver: "v1.3.0", changed: "2026-04-30", signer: "operator", status: "verified" },
  { name: "lock_personae",      ver: "v0.6.0", changed: "2026-05-10", signer: "operator", status: "verified" },
];

const PERSONAS = [
  { name: "judge_v1",     hash: "9a02…f1", status: "verified" },
  { name: "reviewer_v1",  hash: "4dee…20", status: "verified" },
  { name: "mutator_v1",   hash: "11bc…03", status: "verified" },
  { name: "postmortem_v1",hash: "ce41…77", status: "verified" },
  { name: "adversary_v1", hash: "8021…d8", status: "verified" },
  { name: "regime_v1",    hash: "f019…a4", status: "verified" },
  { name: "explainer_v1", hash: "2bb1…0e", status: "verified" },
  { name: "validator_v1", hash: "55a0…91", status: "verified" },
];

const HALTS = [
  { time: "2026-05-08 09:14", reason: "manual_operator_halt", operator: "operator", seq: 27412, duration: "4m 18s" },
  { time: "2026-04-29 14:01", reason: "drift_threshold",       operator: "kernel",   seq: 26104, duration: "12m 02s" },
  { time: "2026-04-22 11:31", reason: "manual_operator_halt", operator: "operator", seq: 25881, duration: "1h 02m" },
  { time: "2026-04-12 05:45", reason: "data_staleness",       operator: "kernel",   seq: 24502, duration: "3m 51s" },
];

const LEDGER_HEALTH = {
  tables: [
    { name: "orders",       rows: 18204 },
    { name: "fills",        rows: 17981 },
    { name: "positions",    rows: 12   },
    { name: "decisions",    rows: 9215 },
    { name: "halts",        rows: 42   },
    { name: "mutations",    rows: 411  },
    { name: "policy_locks", rows: 9    },
  ],
  last_seq: 28411,
  last_hash: "7c1a…ee",
  chain_verified_at: "13:02:11Z",
  // last 60 blocks
  blocks: Array.from({ length: 60 }, (_, i) => ({ ok: true, seq: 28411 - (59 - i) })),
};

const DAEMON = {
  last_beat: "13:02:11Z",
  uptime: "47h 12m 04s",
  host: "127.0.0.1",
  pid: 41882,
  beats_per_min: 6,
};

const COST_MODEL = {
  per_trade_bps: { raw: 1.2, broker_paper: 2.6, pessimistic: 4.5 },
};

const DRIFT = {
  window: 20,
  current_bps: 1.9,
  threshold_bps: 2.5,
  sparkline: [1.1, 1.3, 1.0, 0.8, 1.4, 1.6, 1.2, 1.0, 1.9, 2.3, 2.0, 1.7, 1.5, 1.1, 1.4, 1.6, 1.9, 2.1, 2.0, 1.9],
};

const RECON = {
  last_run: "13:02:30",
  total: 18204,
  mismatches: 1,
  unresolved: 1,
};

Object.assign(window, {
  LANES,
  STATUS_BASE,
  REGIME,
  RISK_CAPS,
  STRATEGY_MODE,
  POSITIONS,
  OPEN_ORDERS,
  ACTION_REQUIRED,
  SEED_ACTIVITY,
  EQUITY_CURVE,
  EXPOSURE_BREAKDOWN,
  DAILY_DIGEST,
  DECISIONS,
  LESSONS,
  LAST_SCAN,
  STRATEGIES,
  WF_FOLDS,
  HEATMAP,
  MUTATIONS,
  PROMOTION_QUEUE,
  LLM_SPEND,
  JOBS,
  FRESHNESS,
  POLICY_LOCKS,
  PERSONAS,
  HALTS,
  LEDGER_HEALTH,
  DAEMON,
  COST_MODEL,
  DRIFT,
  RECON,
});
