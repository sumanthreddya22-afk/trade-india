#!/usr/bin/env python
"""Generate docs/BKCLTrade-How-It-Works.pdf — two-version project explainer.

Side-by-side: a traders' walkthrough and a plain-English walkthrough.
Self-contained; run with `uv run python tools/build_project_explainer_pdf.py`.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

OUT = Path("docs/BKCLTrade-How-It-Works.pdf")
OUT.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

styles = getSampleStyleSheet()

INDIGO = colors.HexColor("#1a237e")
SLATE = colors.HexColor("#37474f")
ACCENT = colors.HexColor("#0d47a1")
MUTED = colors.HexColor("#546e7a")
LIGHT_BG = colors.HexColor("#eceff1")
CODE_BG = colors.HexColor("#f5f5f5")

styles.add(ParagraphStyle(
    name="CoverTitle", parent=styles["Title"],
    fontSize=28, leading=34, textColor=INDIGO, alignment=TA_CENTER,
    spaceAfter=8,
))
styles.add(ParagraphStyle(
    name="CoverSub", parent=styles["Normal"],
    fontSize=14, leading=18, textColor=MUTED, alignment=TA_CENTER,
    spaceAfter=24,
))
styles.add(ParagraphStyle(
    name="CoverMeta", parent=styles["Normal"],
    fontSize=10, leading=14, textColor=MUTED, alignment=TA_CENTER,
))
styles.add(ParagraphStyle(
    name="H1", parent=styles["Heading1"],
    fontSize=20, leading=24, textColor=INDIGO, spaceBefore=18, spaceAfter=10,
    keepWithNext=True,
))
styles.add(ParagraphStyle(
    name="H2", parent=styles["Heading2"],
    fontSize=14, leading=18, textColor=ACCENT, spaceBefore=14, spaceAfter=6,
    keepWithNext=True,
))
styles.add(ParagraphStyle(
    name="H3", parent=styles["Heading3"],
    fontSize=11, leading=14, textColor=SLATE, spaceBefore=10, spaceAfter=4,
    keepWithNext=True,
))
styles.add(ParagraphStyle(
    name="Body", parent=styles["BodyText"],
    fontSize=10, leading=14, textColor=SLATE,
    spaceAfter=6, alignment=TA_LEFT,
))
styles.add(ParagraphStyle(
    name="MyBullet", parent=styles["BodyText"],
    fontSize=10, leading=14, textColor=SLATE,
    leftIndent=18, bulletIndent=6, spaceAfter=3,
))
styles.add(ParagraphStyle(
    name="Quote", parent=styles["BodyText"],
    fontSize=10.5, leading=15, textColor=SLATE,
    leftIndent=18, rightIndent=18, spaceBefore=6, spaceAfter=10,
    fontName="Helvetica-Oblique",
))
styles.add(ParagraphStyle(
    name="MyCode", parent=styles["Code"],
    fontSize=8.5, leading=11, textColor=colors.HexColor("#1b5e20"),
    backColor=CODE_BG, borderPadding=4, leftIndent=4, rightIndent=4,
    spaceBefore=4, spaceAfter=8,
))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def H1(t): return Paragraph(t, styles["H1"])
def H2(t): return Paragraph(t, styles["H2"])
def H3(t): return Paragraph(t, styles["H3"])
def P(t):  return Paragraph(t, styles["Body"])
def Q(t):  return Paragraph(t, styles["Quote"])
def C(t):
    safe = (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return Paragraph(f"<font face='Courier'>{safe}</font>", styles["MyCode"])

def bullets(items):
    flow = []
    for item in items:
        flow.append(Paragraph("• " + item, styles["MyBullet"]))
    return flow

def info_table(rows, col_widths=None, header=True):
    style = [
        ("BACKGROUND",  (0, 0), (-1, 0), INDIGO if header else LIGHT_BG),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.whitesmoke if header else SLATE),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("LEADING",     (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
        ("TOPPADDING",    (0, 0), (-1, 0), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, LIGHT_BG]),
        ("GRID",        (0, 0), (-1, -1), 0.25, colors.HexColor("#b0bec5")),
    ]
    rendered = []
    for r, row in enumerate(rows):
        out_row = []
        for cell in row:
            style_name = "Body" if r > 0 else "Body"
            text_style = ParagraphStyle(
                f"_t_{r}", parent=styles["Body"],
                fontSize=9, leading=12,
                textColor=(colors.whitesmoke if (header and r == 0) else SLATE),
                fontName=("Helvetica-Bold" if (header and r == 0) else "Helvetica"),
            )
            out_row.append(Paragraph(str(cell), text_style))
        rendered.append(out_row)
    t = Table(rendered, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle(style))
    return t


# ---------------------------------------------------------------------------
# Page-level chrome
# ---------------------------------------------------------------------------

def header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(2 * cm, 1.2 * cm,
                      "BKCLTrade — How It Works")
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm,
                           f"Page {doc.page}")
    canvas.setStrokeColor(colors.HexColor("#cfd8dc"))
    canvas.line(2 * cm, 1.5 * cm, A4[0] - 2 * cm, 1.5 * cm)
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def cover():
    today = dt.date.today().isoformat()
    return [
        Spacer(1, 4 * cm),
        Paragraph("BKCLTrade", styles["CoverTitle"]),
        Paragraph("How the codebase actually works",
                  styles["CoverSub"]),
        Spacer(1, 8 * cm),
        Paragraph("A semi-autonomous algorithmic trading laboratory for "
                  "Indian markets (NSE / BSE) via Zerodha Kite Connect.",
                  styles["CoverMeta"]),
        Spacer(1, 1.2 * cm),
        Paragraph(f"Generated {today}  •  ~25,000 LOC  •  720 passing tests",
                  styles["CoverMeta"]),
        Paragraph("Two-version walkthrough: trader's view + plain-English view",
                  styles["CoverMeta"]),
        PageBreak(),
    ]


def toc():
    rows = [
        ["§", "Section", "Page"],
        ["", "Cover", "1"],
        ["", "Table of contents", "2"],
        ["A", "Version A — for someone with trading knowledge", "3"],
        ["A.1", "Core philosophy", "3"],
        ["A.2", "The three concentric rings", "3"],
        ["A.3", "The daemon — what runs when", "5"],
        ["A.4", "The Indian-specific pieces", "6"],
        ["A.5", "Hard safety rules", "7"],
        ["A.6", "How an order actually flows", "7"],
        ["B", "Version B — for someone without trading knowledge", "8"],
        ["B.1", "What this project IS", "8"],
        ["B.2", "The hospital metaphor", "8"],
        ["B.3", "The three big chunks", "8"],
        ["B.4", "The robot's daily schedule", "10"],
        ["B.5", "The Indian-specific parts", "11"],
        ["B.6", "The five rules that cannot be broken", "11"],
        ["B.7", "What's happening right now", "12"],
        ["B.8", "What would it take to make a real trade", "12"],
        ["C", "Appendix", "13"],
        ["C.1", "Repository map", "13"],
        ["C.2", "Recent commits (India migration)", "13"],
    ]
    return [
        H1("Table of contents"),
        info_table(rows, col_widths=[1.2 * cm, 12 * cm, 1.6 * cm]),
        PageBreak(),
    ]


# ----------------------------- VERSION A ---------------------------------

def version_a():
    flow = []
    flow += [H1("Version A — for someone with trading knowledge")]
    flow += [P(
        "Concrete jargon allowed. Assumes familiarity with STT, SPAN, T+1 "
        "settlement, walk-forward backtests, deflated Sharpe ratio, "
        "probability of backtest overfitting (PBO), BH-FDR, MWPL, and the "
        "broad idea of a hash-chained event ledger."
    )]

    # A.1
    flow += [H2("A.1  Core philosophy")]
    flow += [Q(
        "Build an autonomous <i>systematic trading laboratory</i>, not a "
        "chatbot that trades. AI generates hypotheses, mutates strategies, "
        "runs research, writes postmortems, and recommends promotions. "
        "A <b>deterministic</b> risk, ledger, validation, and execution "
        "kernel decides whether anything may trade."
    )]
    flow += [P(
        "Autonomy level: L2 — the bot may run sandbox experiments and "
        "produce validation packets; it cannot promote to live capital "
        "without human sign-off."
    )]

    # A.2
    flow += [H2("A.2  The three concentric rings")]

    flow += [H3("Ring 1 — Deterministic kernel (no LLM ever)")]
    flow += [info_table([
        ["Module", "LOC", "What it does"],
        ["<b>ledger/</b>", "3,216",
         "27 SQLite tables, append-only + hash-chained. Each row carries "
         "<font face='Courier'>prev_hash</font> and "
         "<font face='Courier'>this_hash = sha256(prev_hash ‖ canonical_json(row))</font>. "
         "Same primitive as a blockchain. Verified at boot and nightly."],
        ["<b>risk/</b>", "2,912",
         "Single-entry gate <font face='Courier'>precheck.evaluate()</font>. "
         "8 kill switches × 7 cap categories × 4 SEBI gates × regime overlay "
         "× live-capital cap. Strictest rule wins."],
        ["<b>execution/order_router</b>", "—",
         "Risk-gated, idempotent (client_order_id-keyed), data-freshness-"
         "checked submission with exponential-backoff retry on transient "
         "broker errors. Permanent errors fail fast."],
        ["<b>kernel/boot.py</b>", "177",
         "At startup: SQLite integrity_check on ledger + mirror; schema "
         "version match; recompute SHA-256 of every .lock against "
         "policy/HASHES; verify the full hash chain; surface active kills. "
         "Refuses to start on any mismatch."],
    ], col_widths=[4 * cm, 1.4 * cm, 10.5 * cm])]
    flow += [Spacer(1, 4)]
    flow += [P("Off-host mirror DB for cross-checking + single-writer file "
               "lock to prevent racing writers.")]

    flow += [H3("Ring 2 — Strategy registry + validated alpha")]
    flow += [P(
        "<font face='Courier'>registry/</font> ships three append-only "
        "tables: <b>strategy_version</b> (immutable code+config hash per "
        "version), <b>validation_artifact</b> (three tiers per Plan §13), "
        "<b>promotion_packet</b> (Tier-3 with human sign-off)."
    )]
    flow += [P("Nine strategies are live in code:")]
    flow += bullets([
        "<b>Cross-sectional momentum</b> — etf_momentum_v1/v3 (12-1 month "
        "rank on NSE ETFs, top-N equal weight, monthly rebalance)",
        "<b>Dual momentum</b> — dual_momentum_v1/v3 (binary rotation "
        "NIFTYBEES ↔ LIQUIDBEES based on absolute momentum)",
        "<b>Crypto trend</b> — crypto_momentum_v1/v3 (INR pairs via "
        "CoinDCX, capped at 15% equity)",
        "<b>Options wheel</b> — spy_wheel_v1/v3 (CSP → assigned → CC, "
        "multi-underlying NIFTY/BANKNIFTY/FINNIFTY in v3)",
        "<b>NIFTY gap fade</b> — nifty_gap_v1 (intraday hypothesis at "
        "research_only; just added)",
    ])
    flow += [P(
        "A lane only emits orders when status ∈ {tiny_paper, scaled_paper, "
        "live}. <font face='Courier'>research_only</font>, "
        "<font face='Courier'>shadow</font>, "
        "<font face='Courier'>halted</font>, "
        "<font face='Courier'>observe_only</font>, "
        "<font face='Courier'>reduce_only</font> are blocked from new entries."
    )]

    flow += [H3("Ring 3 — Research factory (LLM-allowed, sandboxed)")]
    flow += [P(
        "<font face='Courier'>research/</font> (~4,900 LOC, 28 modules) "
        "is a full quant research stack:"
    )]
    flow += bullets([
        "<b>walkforward.py</b> — purged + embargoed walk-forward folds + "
        "30% locked holdout",
        "<b>dsr.py</b> — Deflated Sharpe Ratio (Bailey / López de Prado), "
        "n_trials-aware",
        "<b>pbo.py</b> — Probability of Backtest Overfitting (same authors' "
        "combinatorial test)",
        "<b>bh_fdr.py</b> — Benjamini-Hochberg multiple-testing correction; "
        "writes adjusted p-values + survived flag",
        "<b>parameter_plateau.py</b> — best Sharpe shouldn't be a needle "
        "in noise",
        "<b>failure_memory.py</b> — 90-day reject cache, hash-chained, "
        "append-only; mutation engine cannot re-propose recent duds",
        "<b>mutation_engine.py</b> — drives nightly cycles from policy/"
        "search_space_v1.json (hash-locked); 64 variants/family/month cap",
        "<b>sandbox.py</b> — blocks <font face='Courier'>execution/</font>, "
        "<font face='Courier'>kernel/</font>, and "
        "<font face='Courier'>risk.precheck</font> imports during mutation "
        "runs (LLM physically cannot touch the gate)",
        "<b>paper_validation.py</b> — precheck → submit → fill → slippage "
        "compare for fast-tracked candidates",
        "<b>tier1.py</b> — backtest → DSR + PBO → validation_artifact",
    ])
    flow += [P(
        "Promotion happens <b>only</b> when Tier-3 packets pass; the kernel "
        "rejects any version with "
        "<font face='Courier'>validation_artifact_id = NULL</font> or a "
        "failing artifact."
    )]

    # A.3
    flow += [PageBreak(), H2("A.3  The daemon — what runs when")]
    flow += [P(
        "<font face='Courier'>daemon/scheduler.py</font> wires 17 jobs on "
        "APScheduler. Cadences from Plan §6 + §10:"
    )]
    flow += [info_table([
        ["Job", "Cadence", "Purpose"],
        ["boot_check", "startup + every 6h",
         "Re-verify policy hashes + hash chain"],
        ["market_data_ingest", "every 1 min during RTH, 5 min otherwise",
         "Pull bars, update lane watermarks"],
        ["position_snapshot", "every 5 min",
         "Capture current broker positions"],
        ["account_snapshot", "every 5 min",
         "Equity / cash / buying-power; session-start anchor"],
        ["orphan_loop", "every 30 s",
         "Reconcile broker orders we didn't initiate"],
        ["reconciliation", "23:00 IST nightly",
         "Ledger vs broker cross-check; fire recon_mismatch kill on diff"],
        ["drift_monitor", "23:30 IST nightly",
         "Modelled vs realised cost over last 20 fills; demote on tolerance"],
        ["strategy_runner", "15:30 IST daily, 7d/wk",
         "Fire each strategy's evaluate_strategy(); equity self-skips on "
         "holidays"],
        ["mutation_cycle", "nightly across v3 families",
         "Propose → backtest → BH-FDR"],
        ["universe_audit", "daily",
         "Detect drift between live universe and policy-locked filter"],
        ["regime_monitor", "daily",
         "Classify regime; write regime_event"],
        ["intel_refresh", "daily",
         "Pull RBI / VIX / G-Sec / F&amp;O / BSE feeds"],
        ["mutation_review / source_scout / strategy_intake / "
         "search_space_proposal",
         "various", "Phase D research-bot scaffold"],
    ], col_widths=[4.5 * cm, 4 * cm, 7.4 * cm])]

    # A.4
    flow += [PageBreak(), H2("A.4  The Indian-specific pieces")]
    flow += [P(
        "We translated the entire system from a US-market design "
        "(NYSE / Alpaca / SEC / FINRA) to an India-market design "
        "(NSE / BSE / Zerodha / SEBI / STT / GST). What landed:"
    )]
    flow += [info_table([
        ["Concern", "Implementation"],
        ["Broker",
         "Zerodha Kite Connect adapter at "
         "<font face='Courier'>ingest/zerodha_adapter.py</font> (15.5 KB "
         "scaffold). Access token refreshes daily at 06:00 IST."],
        ["Cost model (3 lenses)",
         "Raw / broker_paper / pessimistic. Gate uses pessimistic only. "
         "Stocks (CNC): STT 0.1% both sides + stamp 0.015% buy + exchange "
         "0.00325% + SEBI 0.0001% + GST 18% on (brokerage+exchange+SEBI). "
         "F&amp;O: ₹20 flat + STT 0.0125% sell + stamp 0.003% buy + GST. "
         "Crypto: 20 bps taker + 1% TDS on sells ≥ ₹10k (Section 194S)."],
        ["Calendar",
         "NSE holidays hand-curated through 2027 (Republic Day, "
         "Mahashivratri, Holi, Eid, Good Friday, Ambedkar, Maharashtra, "
         "Independence, Ganesh Chaturthi, Gandhi Jayanti, Dussehra, "
         "Diwali, Gurunanak, Christmas). Muhurat session treated as closed "
         "for cadence. No half-days."],
        ["Session clock",
         "RTH 09:15-15:30 IST. Pre-open 09:00-09:15 (call auction; orders "
         "accepted, no execution). IST is fixed UTC+5:30, no DST."],
        ["SEBI circuit breakers",
         "<font face='Courier'>risk/india_sebi.py</font>: tiered NIFTY halt "
         "(10/15/20%, time-of-day dependent), F&amp;O ban-list opening-"
         "block (closing allowed), per-stock upper/lower price band check, "
         "conservative F&amp;O buy-side margin guard (NOT SPAN — Zerodha "
         "computes real SPAN at gateway)."],
        ["Crypto tax",
         "30% flat on gains (Section 115BBH) tracked annually; 1% TDS on "
         "sells per-trade in cost model."],
        ["Intel feeds",
         "All free. RBI rates (DBIE), India VIX (yfinance), G-Sec curve "
         "(FBIL), NSE F&amp;O OI/PCR, BSE filings."],
    ], col_widths=[3.5 * cm, 12.4 * cm])]

    # A.5
    flow += [H2("A.5  Hard safety rules")]
    flow += [P(
        "Cannot be bypassed without a deliberate env flag <i>and</i>, for "
        "some, a signed policy lock + 7-day cooldown:"
    )]
    flow += bullets([
        "<b>No LLM in trading hot path</b> — "
        "<font face='Courier'>TRADING_BOT_ENABLE_LLM_HOTPATH</font> default "
        "off. Every entry / scout / unblock early-exits.",
        "<b>No live param writes</b> — "
        "<font face='Courier'>TRADING_BOT_ALLOW_LIVE_PARAM_WRITES=1</font> "
        "required.",
        "<b>Crypto ≤ 15% equity</b> — enforced by "
        "<font face='Courier'>risk.asset_class_caps</font>.",
        "<b>Append-only</b> — schema forbids UPDATE/DELETE on "
        "<font face='Courier'>order_master</font>, "
        "<font face='Courier'>fill_event</font>, "
        "<font face='Courier'>position_snapshot</font>, "
        "<font face='Courier'>strategy_decision</font>, "
        "<font face='Courier'>reconciliation_proof</font>.",
        "<b>Policy hash verification</b> at every boot. Loosening any "
        "threshold = new dated .lock file + signature + 7-day cooldown.",
    ])

    # A.6
    flow += [H2("A.6  How an order actually flows")]
    flow += [C(
        "Strategy (e.g. etf_momentum_v1.evaluate_strategy)\n"
        "   ↓ produces OrderIntent {symbol, qty, side, asset_class, ...}\n"
        "execution.order_router.submit_order\n"
        "   ├─ 1. risk.precheck.evaluate\n"
        "   │     (kill switches → SEBI gates → account caps → PDT →\n"
        "   │      asset-class caps → lane caps → strategy loss →\n"
        "   │      regime overlay → symbol cap → order cap → live-cap)\n"
        "   │           → halt | reduce | accept\n"
        "   ├─ 2. ingest.watermarks.check_lane_freshness   → halt if stale\n"
        "   ├─ 3. ledger.order_master.check_idempotent      → reject dup\n"
        "   ├─ 4. ledger.insert_order_master + state_event  → hash-chained\n"
        "   ├─ 5. broker_submit(client_order_id, ...)\n"
        "   │     with exp-backoff retry on transient errors\n"
        "   └─ 6. ledger.append_state_event(submitted | rejected) + mirror"
    )]
    flow += [P(
        "Every step writes to the hash-chained ledger. Three days from "
        "now the entire decision can be replayed deterministically from "
        "the SQLite files alone."
    )]
    return flow


# ----------------------------- VERSION B ---------------------------------

def version_b():
    flow = []
    flow += [PageBreak(), H1("Version B — for someone without trading knowledge")]
    flow += [P(
        "Plain language. Analogies. No jargon without immediate definition. "
        "Some sentences will sound oversimplified to a trader — that's "
        "deliberate."
    )]

    # B.1
    flow += [H2("B.1  What this project IS, in one paragraph")]
    flow += [P(
        "This is a robot that's <i>learning</i> how to trade Indian stocks "
        "and crypto. But it's a paranoid robot. It has a research lab "
        "where it can try out trading ideas on fake money, and a separate "
        "<q>real money</q> door that's bolted shut with five locks. The "
        "robot is allowed to run experiments freely. It is NOT allowed to "
        "spend real money without a human unlocking the door. <b>It is "
        "currently not spending any real money.</b> Everything you see is "
        "either a simulation or a paper-trading dry run."
    )]

    # B.2
    flow += [H2("B.2  The big metaphor — a hospital, not a casino")]
    flow += [info_table([
        ["Hospital", "This codebase"],
        ["The pharmacy where drugs are stored",
         "The <b>ledger</b> — every event recorded, nothing ever deleted"],
        ["The triage nurse at the ER door",
         "The <b>risk kernel</b> — every order is checked before it goes "
         "anywhere"],
        ["The doctors writing prescriptions",
         "The <b>strategies</b> — code that decides <q>buy this, sell that</q>"],
        ["The research wing running clinical trials",
         "The <b>research factory</b> — backtests, statistical validation"],
        ["The hospital's medical board approving new procedures",
         "The <b>strategy registry</b> — nothing goes live without a "
         "passed validation packet"],
        ["The night janitor checking every door is locked",
         "The <b>boot check</b> — at every startup, verify nothing has "
         "been tampered with"],
    ], col_widths=[6.5 * cm, 9.4 * cm])]
    flow += [Spacer(1, 6)]
    flow += [P(
        "The whole thing is designed so that <b>the AI parts can fail or "
        "even go rogue and the system still cannot lose your money.</b>"
    )]

    # B.3
    flow += [H2("B.3  The three big chunks")]

    flow += [H3("Chunk 1 — The vault (the deterministic kernel)")]
    flow += [P(
        "Three things live here, and they are written by humans only. "
        "The AI is forbidden from touching them."
    )]
    flow += [P(
        "<b>The ledger</b> is like a bank's transaction book, except it's "
        "a special book where you cannot erase pages. Every page has a "
        "fingerprint that depends on the previous page's fingerprint — "
        "if anyone tries to change page 47, the fingerprint on page 48 "
        "won't match anymore. The system catches this every time it "
        "starts up. This is the same idea Bitcoin uses, just simpler."
    )]
    flow += [P(
        "<b>The risk kernel</b> is a checklist. Before any order can be "
        "sent to your broker, it has to pass through this checklist:"
    )]
    flow += bullets([
        "Are there any <q>kill switches</q> tripped right now? "
        "(data missing, broker down, etc.)",
        "Is the Indian stock market crashing? "
        "(the SEBI circuit-breaker rules — 10% / 15% / 20% halt tiers)",
        "Is this stock on today's <q>do not trade</q> list? "
        "(NSE publishes this daily for over-positioned scrips)",
        "Is the price allowed to move that high or low today? "
        "(every Indian stock has daily price bands)",
        "Are we putting too much into one stock?",
        "Are we putting too much into crypto? (cap: 15% of total money)",
        "And several more, including a per-order risk-of-loss cap.",
    ])
    flow += [P(
        "If even <b>one</b> check fails, the order is blocked. No exceptions."
    )]
    flow += [P(
        "<b>The execution layer</b> is the actual messenger that takes an "
        "order to the broker (Zerodha). It tags every order with a unique "
        "ID so if the network hiccups and the order accidentally gets "
        "sent twice, the broker recognises the duplicate and ignores it. "
        "If the broker says <q>I'm busy, try again,</q> it retries 3 times "
        "with increasing delays. If the broker says <q>this order is "
        "invalid,</q> it gives up immediately."
    )]

    flow += [H3("Chunk 2 — The doctors (the strategies)")]
    flow += [P(
        "These are the actual trading ideas. There are 9 of them right now. "
        "Examples:"
    )]
    flow += bullets([
        "<b>ETF momentum</b> — Buy whichever Indian stock fund has been "
        "going up the most for the past year. Once a month, check again "
        "and swap if a different one is winning. (Like always sitting at "
        "whichever cafeteria table is most popular.)",
        "<b>Dual momentum</b> — Either invest in NIFTYBEES (an Indian "
        "stock fund) OR in LIQUIDBEES (basically cash), based on which "
        "one is rising faster. Swap as needed. (Park-or-drive strategy.)",
        "<b>Crypto trend</b> — Buy Bitcoin or Ethereum (in rupees) when "
        "they're trending up. Never put more than 15% of your money here, "
        "because crypto is wild.",
        "<b>Options wheel</b> — A more advanced strategy that sells "
        "<q>insurance contracts</q> on NIFTY and collects premiums.",
        "<b>NIFTY gap fade</b> (just added) — When the Indian market "
        "opens at 9:15 AM unusually far from where it closed yesterday, "
        "bet that it will drift back toward yesterday's price by 3:30 PM.",
    ])
    flow += [P(
        "<b>All 9 strategies are currently in <q>research only</q> mode.</b> "
        "None can buy or sell anything until a human approves them."
    )]

    flow += [H3("Chunk 3 — The lab (the research factory)")]
    flow += [P("This is where the AI is allowed to play. Its job is to:")]
    flow += bullets([
        "Take a trading idea",
        "Run it against years of historical price data (<q>backtesting</q>)",
        "Statistically prove the idea isn't just lucky noise (using "
        "techniques borrowed from real quant finance research papers — "
        "the deflated Sharpe ratio, the probability-of-backtest-overfitting "
        "test, etc.)",
        "Try thousands of small variations of the idea (<q>mutations</q>) "
        "to find which works best",
        "Remember every variation that failed for 90 days so it doesn't "
        "propose the same dud twice",
        "Write up a <q>validation packet</q> — basically a report card",
    ])
    flow += [P(
        "A human reads the report card and decides whether to promote the "
        "idea from <q>lab experiment</q> to <q>shadow trading</q> (still "
        "no real money but watched closely) to <q>tiny paper</q> (very "
        "small fake-money trades for 60+ days) to <q>live</q> (real money "
        "— currently blocked entirely)."
    )]
    flow += [P(
        "The lab is in a sandbox. It literally cannot import the code "
        "that talks to the broker. If the AI somehow decides <q>let me "
        "just send a buy order from here,</q> Python refuses to even "
        "load the module."
    )]

    # B.4
    flow += [PageBreak(), H2("B.4  The robot's daily schedule")]
    flow += [P(
        "The system runs as a long-living background process (a "
        "<q>daemon</q>). Different jobs fire at different times:"
    )]
    flow += [info_table([
        ["When", "What"],
        ["Every 30 seconds",
         "Check for any <q>orphan</q> orders sitting at the broker that "
         "we don't know about (e.g. from a previous crash); reconcile them."],
        ["Every 1 minute (Indian market hours)",
         "Pull fresh price data."],
        ["Every 5 minutes",
         "Snapshot what we currently own."],
        ["Every 6 hours",
         "Re-run the startup integrity checks. If anything has been "
         "tampered with, halt."],
        ["3:30 PM IST daily",
         "Let each strategy decide what it wants to do tomorrow."],
        ["11:00 PM IST daily",
         "Compare what we thought would happen today with what actually "
         "happened. If the gap is too big, demote the strategy."],
        ["11:30 PM IST daily",
         "Compare costs (broker fees, slippage) we modelled vs what we "
         "actually paid. If we under-estimated costs, demote."],
        ["First of every month",
         "Run a fresh round of <q>mutation</q> experiments in the lab."],
    ], col_widths=[4.5 * cm, 11.4 * cm])]

    # B.5
    flow += [H2("B.5  The Indian-specific parts")]
    flow += [P(
        "Indian markets work differently from American markets in many "
        "small ways. We translated the whole system from a US-market "
        "design to an India-market design:"
    )]
    flow += [info_table([
        ["What's different in India", "What we changed"],
        ["The broker (Zerodha, not Robinhood / Alpaca)",
         "New adapter to talk to Zerodha's API"],
        ["Market hours (9:15 AM – 3:30 PM IST, no half-days)",
         "New calendar + clock module"],
        ["Costs (STT, GST, stamp duty, exchange fees, SEBI fees — not "
         "US-style SEC / FINRA fees)",
         "Rewrote the entire cost model. Previously it was silently "
         "computing <b>zero fees</b> because the code still used American "
         "fee names — strategies looked artificially profitable."],
        ["Tax (30% flat on crypto gains, 1% TDS deducted at source on "
         "every crypto sale ≥ ₹10,000)",
         "Built into the crypto cost model"],
        ["SEBI's circuit-breaker rules (market halts at 10%, 15%, 20% "
         "drops depending on time of day)",
         "New safety check"],
        ["Daily F&amp;O ban list (NSE bans new positions in over-"
         "positioned scrips)",
         "New safety check"],
        ["Per-stock daily price bands (every Indian stock has a max % "
         "move allowed per day)",
         "New safety check"],
        ["Free data sources only (RBI website, India VIX, NSE data, "
         "BSE filings)",
         "5 new <q>intel feed</q> modules"],
    ], col_widths=[7.5 * cm, 8.4 * cm])]

    # B.6
    flow += [H2("B.6  The five rules that cannot be broken")]
    flow += [P(
        "These are the system's vows. The code is structured so that "
        "violating them requires deliberately editing source code AND "
        "flipping environment variables AND, for some, waiting 7 days:"
    )]
    flow += bullets([
        "<b>The AI cannot directly place trades.</b> Ever. The trading "
        "path checks an env variable before allowing AI input; default "
        "is <q>off.</q>",
        "<b>The AI cannot change the trading rules at runtime.</b> Same "
        "env-var gate.",
        "<b>Crypto can never be more than 15% of the total money.</b> "
        "Enforced before every order.",
        "<b>Nothing in the ledger can be deleted or modified.</b> The "
        "database schema literally forbids it.",
        "<b>The rules can only be loosened with a 7-day cooldown and a "
        "signed lock file.</b> The startup check verifies the rules "
        "haven't been tampered with.",
    ])

    # B.7
    flow += [H2("B.7  What's actually happening right now")]
    flow += bullets([
        "Zero real money is being traded.",
        "The system is set up for <q>paper trading</q> (fake-money "
        "simulation), but not even that is running because there's no "
        "<font face='Courier'>.env</font> file with broker credentials.",
        "9 strategies are coded and registered, all in "
        "<q>research only</q> lane.",
        "The research lab is functional but has no historical price data "
        "to chew on yet — someone needs to write a job that downloads "
        "NSE price history.",
        "720 automated tests pass, meaning the parts that ARE built "
        "work correctly.",
        "The whole architecture is solid. What's missing is operational "
        "wiring (data, credentials, monitoring).",
    ])

    # B.8
    flow += [H2("B.8  What would have to happen for it to make a single real trade")]
    flow += [P("In order:")]
    rows = [
        ["#", "Step"],
        ["1", "Download NSE historical data into "
              "<font face='Courier'>data/historical_bars.db</font>."],
        ["2", "Run one of the strategies through the full Tier-1 → "
              "Tier-2 → Tier-3 validation pipeline (months of paper-"
              "trading observation required)."],
        ["3", "Get a human to sign the Tier-3 promotion packet."],
        ["4", "Generate Zerodha API credentials and place them in "
              "<font face='Courier'>.env</font>."],
        ["5", "Flip the lane status from "
              "<font face='Courier'>research_only</font> to "
              "<font face='Courier'>live</font> in the registry."],
        ["6", "Set <font face='Courier'>BOT_MODE=live</font> and "
              "<font face='Courier'>ENABLE_SUBMIT=true</font>."],
        ["7", "Restart the daemon. Boot checks run; if anything is "
              "amiss, refuses to start."],
        ["8", "The first time a strategy fires after that, an order "
              "goes to Zerodha."],
    ]
    flow += [info_table(rows, col_widths=[1.2 * cm, 14.7 * cm])]
    flow += [Spacer(1, 6)]
    flow += [P(
        "That's 7+ deliberate steps. The whole point of the architecture "
        "is that you cannot get to step 8 by accident."
    )]
    return flow


# --------------------------- APPENDIX ------------------------------------

def appendix():
    flow = []
    flow += [PageBreak(), H1("Appendix")]

    flow += [H2("C.1  Repository map (LOC per package)")]
    flow += [info_table([
        ["Package", "Files", "LOC", "Purpose"],
        ["daemon/", "8", "1,897", "APScheduler + jobs"],
        ["execution/", "5", "868", "Order router + cost model"],
        ["ingest/", "31", "4,910", "Broker adapters + intel feeds"],
        ["kernel/", "2", "177", "Boot check"],
        ["ledger/", "27", "3,216", "Append-only event store"],
        ["registry/", "7", "1,267", "Strategy versioning + promotion"],
        ["research/", "28", "4,906", "Backtest + DSR/PBO/BH-FDR + mutation"],
        ["risk/", "21", "2,912", "Single-entry precheck + SEBI gates"],
        ["operator_ui/", "4", "2,440", "Dashboard"],
        ["operator/", "5", "1,233", "CLI helpers"],
        ["shared/", "5", "682", "Timezone + LLM transport"],
        ["obs/", "3", "292", "Notifier (Gmail SMTP)"],
        ["strategies/", "—", "3,223", "9 strategy packages"],
    ], col_widths=[3 * cm, 1.4 * cm, 1.8 * cm, 9.7 * cm])]

    flow += [H2("C.2  Recent commits (India migration)")]
    flow += [info_table([
        ["Commit", "Title"],
        ["86a6778", "chore(notifier): point hardcoded Gmail address at "
                    "operator inbox"],
        ["a59c67c", "feat(strategy): NIFTY_GAP_v1 — overnight gap fade "
                    "hypothesis at research_only"],
        ["1ec1759", "feat(risk): SEBI gates — index circuit breakers, "
                    "F&amp;O ban list, price bands, margin"],
        ["29cf962", "feat(india): NSE/BSE migration baseline — Zerodha "
                    "broker, INR cost model, IST calendar"],
    ], col_widths=[2.5 * cm, 13.4 * cm])]

    flow += [H2("C.3  TL;DR for either audience")]
    flow += [Q(
        "This is a careful, defensive, well-architected trading research "
        "lab — not a money-making machine. Its value is the <i>framework</i> "
        "(the rules, the safety locks, the hash-chained audit trail, the "
        "validation pipeline). Whether it makes money depends entirely on "
        "whether the <i>strategies</i> you put through the validation "
        "pipeline actually have edge in Indian markets — which is an "
        "empirical question this codebase doesn't try to answer; it just "
        "gives you an honest way to ask."
    )]
    return flow


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def main() -> None:
    doc = SimpleDocTemplate(
        str(OUT), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title="BKCLTrade — How It Works",
        author="BKCLTrade documentation",
    )
    story = []
    story += cover()
    story += toc()
    story += version_a()
    story += version_b()
    story += appendix()
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    print(f"Wrote {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
