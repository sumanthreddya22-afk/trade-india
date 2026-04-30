"""Build /Users/bharathkandala/Trading/docs/project_overview.pdf

Comprehensive project documentation rendered via ReportLab — covers
architecture, the 3 trading workflows (stocks / crypto / options-wheel),
all news + intel sources per workflow, the Lab (evolution, calibration,
walk-forward, promotion), strategies + algorithms, state + persistence,
and reporting + email.

Written from a thorough, file-cited investigation of the codebase on
2026-04-29. Run:

    python3 tools/build_project_overview_pdf.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


OUTPUT = Path("/Users/bharathkandala/Trading/docs/project_overview.pdf")

# ---- Styles --------------------------------------------------------------

base = getSampleStyleSheet()

style_title = ParagraphStyle(
    "TitleBig",
    parent=base["Title"],
    fontSize=26,
    leading=30,
    alignment=TA_LEFT,
    spaceAfter=8,
    textColor=colors.HexColor("#0F172A"),
)
style_subtitle = ParagraphStyle(
    "SubTitle",
    parent=base["Normal"],
    fontSize=12,
    leading=16,
    textColor=colors.HexColor("#475569"),
    spaceAfter=20,
)
style_h1 = ParagraphStyle(
    "H1",
    parent=base["Heading1"],
    fontSize=18,
    leading=22,
    spaceBefore=18,
    spaceAfter=8,
    textColor=colors.HexColor("#0EA5E9"),
)
style_h2 = ParagraphStyle(
    "H2",
    parent=base["Heading2"],
    fontSize=14,
    leading=18,
    spaceBefore=12,
    spaceAfter=6,
    textColor=colors.HexColor("#0F172A"),
)
style_h3 = ParagraphStyle(
    "H3",
    parent=base["Heading3"],
    fontSize=11.5,
    leading=15,
    spaceBefore=8,
    spaceAfter=4,
    textColor=colors.HexColor("#1E293B"),
)
style_body = ParagraphStyle(
    "Body",
    parent=base["BodyText"],
    fontSize=10,
    leading=14,
    spaceAfter=6,
    textColor=colors.HexColor("#1F2937"),
)
style_bullet = ParagraphStyle(
    "Bullet",
    parent=style_body,
    leftIndent=14,
    bulletIndent=4,
    spaceAfter=3,
)
style_code = ParagraphStyle(
    "Code",
    parent=base["Code"],
    fontSize=8.5,
    leading=11,
    backColor=colors.HexColor("#F1F5F9"),
    borderColor=colors.HexColor("#CBD5E1"),
    borderWidth=0.5,
    borderPadding=6,
    leftIndent=4,
    rightIndent=4,
    spaceBefore=4,
    spaceAfter=8,
    textColor=colors.HexColor("#0F172A"),
)
style_callout = ParagraphStyle(
    "Callout",
    parent=style_body,
    backColor=colors.HexColor("#FEF3C7"),
    borderColor=colors.HexColor("#F59E0B"),
    borderWidth=0.5,
    borderPadding=8,
    leftIndent=4,
    rightIndent=4,
    spaceBefore=6,
    spaceAfter=10,
)


def H1(text):
    return Paragraph(text, style_h1)


def H2(text):
    return Paragraph(text, style_h2)


def H3(text):
    return Paragraph(text, style_h3)


def P(text):
    return Paragraph(text, style_body)


def B(text):
    return Paragraph("&bull;&nbsp;&nbsp;" + text, style_bullet)


def CODE(text):
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe = safe.replace("\n", "<br/>")
    return Paragraph(safe, style_code)


def CALL(text):
    return Paragraph(text, style_callout)


def make_table(data, col_widths=None, header_bg="#0EA5E9", zebra=True):
    t = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, 0), "LEFT"),
        ("ALIGN", (0, 1), (-1, -1), "LEFT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#0F172A")),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#94A3B8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if zebra:
        for r in range(1, len(data)):
            if r % 2 == 1:
                cmds.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F8FAFC")))
    t.setStyle(TableStyle(cmds))
    return t


# ---- Page header / footer ------------------------------------------------


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.drawString(0.75 * inch, 0.5 * inch, "Trading Bot — Project Overview")
    canvas.drawRightString(
        LETTER[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}"
    )
    # top accent line
    canvas.setStrokeColor(colors.HexColor("#0EA5E9"))
    canvas.setLineWidth(2)
    canvas.line(
        0.75 * inch, LETTER[1] - 0.55 * inch,
        LETTER[0] - 0.75 * inch, LETTER[1] - 0.55 * inch,
    )
    canvas.restoreState()


# ---- Content -------------------------------------------------------------


def build_story():
    s = []

    # ============================================================
    # COVER
    # ============================================================
    s.append(Paragraph("Trading Bot", style_title))
    s.append(
        Paragraph(
            "A semi-autonomous, multi-asset algorithmic trading system for "
            "Alpaca (paper) — equities, crypto, and options-wheel — with an "
            "automated strategy lab, multi-source intel gates, and a "
            "self-supervising daemon.",
            style_subtitle,
        )
    )
    s.append(
        Paragraph(
            "<b>Generated:</b> 2026-04-29 &nbsp;&middot;&nbsp; "
            "<b>Repo:</b> /Users/bharathkandala/Trading &nbsp;&middot;&nbsp; "
            "<b>Branch:</b> main &nbsp;&middot;&nbsp; "
            "<b>Python:</b> 3.11+",
            style_body,
        )
    )

    s.append(Spacer(1, 12))

    s.append(H2("At a glance"))
    s.append(make_table(
        [
            ["Component", "Count / Detail"],
            ["Source modules (src/trading_bot/)", "78 modules + 4 subpackages (roles, options, backtest, dashboard)"],
            ["Roles (cooperating agents)", "23 (daemon, supervisor, lab tiers)"],
            ["Trading workflows", "3 (stocks momentum, crypto momentum 24/7, options wheel)"],
            ["Per-trade intel gates", "6 named (Tier 1: earnings, fear/greed, reddit-spike; Tier 2: insider, macro shock, coingecko)"],
            ["External data providers", "10 (Polygon, Finnhub, Alpaca, FRED, GDELT, ApeWisdom, Alternative.me, CoinGecko, SEC EDGAR, Truth Social)"],
            ["Alembic migrations", "12"],
            ["Test files", "100+ pytest modules under tests/"],
            ["Strategy templates (live)", "MomentumStrategy (active), MeanReversionStrategy (disabled), Wheel CSP/CC"],
            ["LLM integration", "Anthropic Claude — Opus 4.7 for Strategy Architect, Haiku 4.5 for Tone Analyst"],
        ],
        col_widths=[2.0 * inch, 4.5 * inch],
    ))

    s.append(Spacer(1, 14))
    s.append(H2("Document map"))
    s.append(B("<b>1. Project overview &amp; architecture</b> — what the bot does, how the daemon, supervisor and lab cooperate."))
    s.append(B("<b>2. The three workflows</b> — stocks momentum, crypto momentum, options-wheel — full entry/exit decision trees."))
    s.append(B("<b>3. News &amp; intel sources</b> — every external feed, each per-trade gate, thresholds and caches."))
    s.append(B("<b>4. The Lab</b> — Optuna search, walk-forward, fitness, calibration drift, LLM-driven strategy generation, AST + sandbox validation, promotion."))
    s.append(B("<b>5. Strategies &amp; algorithms</b> — exact rules and math for momentum, mean reversion, wheel CSP/CC, regime detection, backtest harness."))
    s.append(B("<b>6. Risk &amp; portfolio</b> — risk manager gates, regime allocations, sector caps, position protection."))
    s.append(B("<b>7. State, persistence, reporting</b> — state.db schema, 12 migrations, multi-email reporting cadence."))
    s.append(B("<b>8. Schedule timetable &amp; roles</b> — every cron, every role, what feeds what."))
    s.append(B("<b>9. Configuration reference</b> — the full strategy/config.yaml annotated."))

    s.append(PageBreak())

    # ============================================================
    # 1. PROJECT OVERVIEW & ARCHITECTURE
    # ============================================================
    s.append(H1("1. Project overview &amp; architecture"))

    s.append(H2("1.1 What this bot is"))
    s.append(P(
        "This repository is a <b>semi-autonomous algorithmic trading bot</b> "
        "targeting an <b>Alpaca paper account</b>, with a hard, hardcoded "
        "paper-only check on the wheel strategy. It trades three asset classes "
        "side-by-side in a single coordinated process: US equities, USD-quoted "
        "crypto pairs, and listed equity options run as a covered-call wheel."
    ))
    s.append(P(
        "The system is structured as cooperating <b>roles</b> — discrete "
        "single-purpose agents that each implement a uniform "
        "<i>Role Protocol</i> (see <font face='Courier'>src/trading_bot/roles/base.py</font>). "
        "Roles run inside one of three host processes: the <b>daemon</b> "
        "(execution loop, scheduling, market-data and trading), the "
        "<b>supervisor</b> (drawdown sentinel, stall watchdog, schedule auditor) "
        "and the <b>lab</b> (overnight strategy evolution and parameter search)."
    ))

    s.append(H2("1.2 The three host processes"))
    s.append(make_table(
        [
            ["Process", "Entry point", "What it does", "Cadence"],
            ["Daemon", "src/trading_bot/daemon.py:432", "APScheduler driving every trading workflow, intel warm-cache, reporting and reconciliation. Boots Alembic migrations on start, writes data/heartbeat.json every 60 s.", "Continuous (long-running)"],
            ["Supervisor", "src/trading_bot/supervisor.py", "Independent watchdog process. Reads the daemon heartbeat. On stall (> 120 s) emits a critical alert and runs launchctl kickstart. Hosts AccountSentinel, ScheduleAuditor, ResourceGuardian.", "60 s loop"],
            ["Lab", "src/trading_bot/lab.py:95", "Overnight strategy evolution. Optuna parameter search at 02:00 ET, auto-promote at 02:45 ET, calibration at 05:00 ET, Saturday strategy generation at 06:00 ET.", "Cron-driven (BackgroundScheduler)"],
        ],
        col_widths=[0.85 * inch, 2.05 * inch, 3.0 * inch, 1.05 * inch],
    ))

    s.append(H2("1.3 Boot sequence"))
    s.append(B("Daemon reads <font face='Courier'>data/paper_active.json</font> (active config), overridable via <font face='Courier'>TRADING_BOT_CONFIG</font>."))
    s.append(B("Alembic migrations run automatically against <font face='Courier'>data/state.db</font> at startup (daemon.py:438-456)."))
    s.append(B("WAL mode is enabled at engine creation (<font face='Courier'>journal_mode=WAL</font>, <font face='Courier'>synchronous=NORMAL</font>) so daemon, lab and supervisor read concurrently without locking."))
    s.append(B("Heartbeat written via tmp+rename, with PID + uuid suffix in the tmp filename to prevent collisions on concurrent scheduler jobs (state_heartbeat.py:17)."))
    s.append(B("All scheduled jobs are wrapped in <font face='Courier'>_wrap()</font> which checks the <font face='Courier'>data/pause.flag</font> sentinel before running &mdash; the AccountSentinel writes that flag on drawdown breach."))

    s.append(H2("1.4 Stall and drawdown safety"))
    s.append(P(
        "The system has two independent watchdogs. The <b>StallDetector</b> "
        "(<font face='Courier'>watchdog_stall.py:22-52</font>) compares "
        "<font face='Courier'>data/heartbeat.json</font> mtime to wall clock; "
        "if older than 120 s it issues a <b>single</b> "
        "<font face='Courier'>launchctl kickstart -k gui/{uid}/{daemon_label}</font> "
        "and emits a critical email. The <b>AccountSentinel</b> "
        "(<font face='Courier'>roles/account_sentinel.py</font>) reconciles "
        "the Alpaca account against an equity high-water mark "
        "(<font face='Courier'>state_hwm.py</font>); on breach it writes "
        "<font face='Courier'>data/pause.flag</font>, which freezes every "
        "order-placing job until cleared."
    ))

    s.append(CALL(
        "<b>Pause semantics:</b> <font face='Courier'>data/pause.flag</font> blocks "
        "intel_scan and crypto_scan (the two order-placing workflows). It does "
        "<i>not</i> block the daily digest, schedule auditor, alert drainer or "
        "reconciler, which remain safe to run."
    ))

    s.append(PageBreak())

    # ============================================================
    # 2. THE THREE WORKFLOWS
    # ============================================================
    s.append(H1("2. The three trading workflows"))

    s.append(P(
        "All three workflows share a uniform pipeline: <b>preflight</b> "
        "(idempotency, position checks) &rarr; <b>signal</b> (technical evaluation) "
        "&rarr; <b>intel gates</b> (news / sentiment vetoes) &rarr; "
        "<b>risk gate</b> (sizing, caps, regime allocation) &rarr; <b>order</b>. "
        "Gates can only veto entries; nothing in the pipeline opens a position "
        "that wasn&rsquo;t first signalled. This makes adding a new gate "
        "<i>strictly safer</i>, never more aggressive."
    ))

    # -------- Workflow 1: Stocks --------
    s.append(H2("2.1 Workflow 1 &mdash; Equity Momentum (stocks)"))
    s.append(B("<b>Purpose:</b> hourly intraday scan of US equities for short-term momentum entries."))
    s.append(B("<b>Runner:</b> <font face='Courier'>StockScannerRole</font> &mdash; <font face='Courier'>roles/stock_scanner.py</font>."))
    s.append(B("<b>Cadence:</b> hourly cron, weekdays 09:00&ndash;15:00 ET (registered as <font face='Courier'>intel_scan</font> in scheduler_jobs.py)."))
    s.append(B("<b>Universe:</b> two-stage screener &mdash; stage 1 ranks the entire Polygon US universe, stage 2 evaluates the top-100 across three lanes."))

    s.append(H3("Stage-1 ranking (screener.py:75-102)"))
    s.append(CODE(
        "score = one_day_return * 1.4\n"
        "      + (stock_5d_return - SPY_5d_return)\n"
        "      + min(volume_ratio_20d, 3.0) * 2.0\n"
        "# top 100 carried into stage 2"
    ))

    s.append(H3("Stage-2 lanes (strategy_lanes.py)"))
    s.append(B("<b>MomentumLane</b> (lines 41&ndash;86): RSI(14) &isin; [55, 70], MACD &gt; signal, close &gt; EMA(20), 5-day return &gt; 0. Conviction 0.4&ndash;0.9."))
    s.append(B("<b>MeanReversionLane</b> (lines 89&ndash;124): RSI &lt; 30, close below 20-day Bollinger lower band (2&sigma;)."))
    s.append(B("<b>BreakoutLane</b> (lines 127&ndash;160): close above prior 20-day high, volume &gt; 1.5&times; 20-day average."))

    s.append(H3("Entry decision (orchestrator.py:154-248)"))
    s.append(B("Has open position / pending order / already traded today &rarr; <b>skip</b>."))
    s.append(B("Insufficient bars (&lt; 26) or strategy returns HOLD &rarr; <b>skip</b>."))
    s.append(B("News sentiment floor: if Polygon score &lt; <font face='Courier'>strategy.sentiment_floor</font> (default <b>&minus;0.5</b>) &rarr; <b>skip</b>."))
    s.append(B("Macro shock gate (GDELT) &le; &minus;3.0 &rarr; <b>skip</b>."))
    s.append(B("Earnings gate (Finnhub): earnings within next 5 trading days &rarr; <b>skip</b>."))
    s.append(B("Insider cluster gate (Finnhub, OFF by default): &ge; 5 sells in 90 d &rarr; <b>skip</b>."))
    s.append(B("Risk manager: per-trade risk &le; 1%, position &le; 10%, concentration &le; 5%, daily loss &gt; &minus;2%, weekly &gt; &minus;5%."))
    s.append(B("If all gates pass: place <b>buy</b> order; stop = <font face='Courier'>max(EMA20, entry &times; (1 &minus; stop_pct))</font>."))

    s.append(H3("Position sizing (strategy.py:86-93)"))
    s.append(CODE(
        "risk_qty           = floor((equity * risk_pct/100) / per_share_risk)\n"
        "concentration_qty  = floor((equity * max_concentration_pct/100) / entry)\n"
        "qty                = min(risk_qty, concentration_qty)\n"
        "if qty < 1: HOLD with reason 'qty < 1 share'"
    ))

    # -------- Workflow 2: Crypto --------
    s.append(H2("2.2 Workflow 2 &mdash; Crypto Momentum (24/7)"))
    s.append(B("<b>Purpose:</b> continuous scan of ~30 USD-quoted crypto pairs."))
    s.append(B("<b>Runner:</b> <font face='Courier'>CryptoScannerRole</font> (<font face='Courier'>roles/crypto_scanner.py</font>) &mdash; runs every 30 minutes, 24&times;7."))
    s.append(B("<b>Universe discovery</b> (<font face='Courier'>crypto_universe.py</font>): pulls Alpaca active crypto assets, filters to USD-quoted, excludes stablecoins (USDC, USDT, DAI&hellip;), applies operator blocklist <font face='Courier'>strategy/crypto_blocklist.yaml</font>."))
    s.append(B("<b>Signal:</b> shares the <i>MomentumLane</i> evaluator with equities &mdash; same RSI/MACD/EMA rules."))

    s.append(H3("Crypto-specific intel gates"))
    s.append(B("<b>Polygon news sentiment floor is bypassed</b> &mdash; Polygon news is equity-focused."))
    s.append(B("<b>Macro shock gate</b> (GDELT &le; &minus;3.0): blocks &mdash; the only macro signal that affects crypto entries."))
    s.append(B("<b>Crypto Fear &amp; Greed gate</b> (Alternative.me): index outside <b>[20, 80]</b> &rarr; skip."))
    s.append(B("<b>Reddit spike gate</b> (ApeWisdom r/CryptoCurrency): mentions &ge; 2.0&times; 24h-ago &rarr; skip."))
    s.append(B("<b>CoinGecko sentiment gate</b> (OFF by default): community sentiment &lt; 50% &rarr; skip when enabled."))
    s.append(B("<b>Asset class cap</b>: crypto allocation capped at 25% in trending_up, 5% in risk_off (regime-dependent)."))

    s.append(CALL(
        "Crypto runs 24/7 and ignores RTH. The same heartbeat / pause flag still "
        "apply, so a daemon stall halts crypto trading the moment the supervisor "
        "writes <font face='Courier'>pause.flag</font> &mdash; even at 3 AM."
    ))

    # -------- Workflow 3: Wheel --------
    s.append(H2("2.3 Workflow 3 &mdash; Covered-Call Wheel (options)"))
    s.append(B("<b>Purpose:</b> generate yield via covered call premium on eligible equities."))
    s.append(B("<b>Single state machine</b> per symbol: <font face='Courier'>CSP_OPEN</font> &rarr; <font face='Courier'>ASSIGNED</font> &rarr; <font face='Courier'>CC_OPEN</font> &rarr; <font face='Courier'>CLOSED</font> (<font face='Courier'>options/wheel_state.py:17-22</font>)."))
    s.append(B("<b>Paper-only:</b> hard-coded URL prefix check. Enabled 2026-04-29."))

    s.append(H3("Wheel runners (options/wheel_runner.py)"))
    s.append(make_table(
        [
            ["Job", "Time (ET)", "What it does"],
            ["wheel_universe_build", "21:30 nightly", "Walk Alpaca optionable_us_equities (~6,000), filter via Finnhub (market cap, listing age). Cache wheel_universe_cache 14 d."],
            ["iv_capture", "09:45 weekdays", "Capture ATM 30 d IV per eligible name; persist to option_iv_history."],
            ["wheel_scan", "10:15 weekdays", "Single CSP/CC entry pass. Preflight, chain pick, sector cap, journal."],
            ["wheel_manage", "10:00&ndash;15:00 every 30 min", "Take-profit, DTE force-close, delta-breach roll, cycle close."],
        ],
        col_widths=[1.4 * inch, 1.4 * inch, 3.5 * inch],
    ))

    s.append(H3("Preflight gates (wheel_lane.py:62-89)"))
    s.append(B("Cycle phase &isin; {none, ASSIGNED} &mdash; CSP_OPEN/CC_OPEN are managed elsewhere."))
    s.append(B("Regime &isin; {trending_up, sideways}; trending_down and risk_off blocked."))
    s.append(B("VIX &isin; [<b>15.0</b>, <b>30.0</b>]; below = premiums too thin, above = vol-of-vol risk."))
    s.append(B("Sentiment &ge; <b>&minus;0.3</b> (looser than equity floor &mdash; we already screen by other means)."))
    s.append(B("IV rank &ge; <b>30%</b> over trailing 252 d."))
    s.append(B("WSB spike check (ApeWisdom r/WallStreetBets): mentions &ge; 2.0&times; baseline &rarr; skip."))
    s.append(B("Earnings window: skip if Finnhub flags earnings between today and (expiration + 2 d)."))

    s.append(H3("Contract picking (chain.py:42-77)"))
    s.append(P(
        "Both <font face='Courier'>pick_csp_contract</font> and "
        "<font face='Courier'>pick_cc_contract</font> filter contracts by "
        "DTE&nbsp;&isin;&nbsp;[30,&nbsp;45], abs(delta)&nbsp;&isin;&nbsp;[0.20,&nbsp;0.30], "
        "open interest&nbsp;&ge;&nbsp;100, spread&nbsp;&le;&nbsp;$0.10 or 5%% of mid, "
        "bid&nbsp;&ge;&nbsp;$0.20. The CSP picker sells the put closest to delta&nbsp;0.25; "
        "the CC picker additionally requires <font face='Courier'>strike &ge; cost_basis</font> "
        "so being called away locks in profit."
    ))

    s.append(H3("Management cycle (wheel_runner.py:282-435)"))
    s.append(B("<b>Take-profit:</b> if <font face='Courier'>mid &le; credit &times; (1 &minus; 0.50)</font> &rarr; buy-to-close."))
    s.append(B("<b>DTE force-close:</b> if <font face='Courier'>dte &le; 21</font> &rarr; close."))
    s.append(B("<b>Delta breach roll:</b> CSP &gt; 0.45 / CC &gt; 0.55, roll up to <b>2 times</b> per cycle."))
    s.append(B("<b>Roll mechanic:</b> buy-to-close current + sell-to-open one expiry out at same delta band; falls back to defensive close if no replacement contract."))
    s.append(B("<b>Sector cap:</b> 25% per GICS sector (yfinance-backed cache, 14 d TTL); unknown sectors do not gate."))

    s.append(PageBreak())

    # ============================================================
    # 3. NEWS & INTEL SOURCES
    # ============================================================
    s.append(H1("3. News &amp; intel sources &mdash; per workflow"))

    s.append(P(
        "The bot ingests <b>10 external providers</b> across price data, news, "
        "sentiment and macro context. Six of them feed the <b>per-trade gates</b> "
        "added in commit <font face='Courier'>f1df0f6</font> ('six per-trade "
        "news/intel gates'). Every gate has soft-fail semantics: a network "
        "error or 4xx returns <i>None</i> and lets the entry through, never "
        "blocks it. This protects against an upstream outage taking the bot "
        "offline."
    ))

    s.append(H2("3.1 Provider catalog"))
    s.append(make_table(
        [
            ["Provider", "Endpoint / file", "What it produces", "Cache &amp; TTL"],
            ["Polygon (via Massive)", "massive_client.py", "Per-ticker news + Polygon-built sentiment label, daily grouped OHLC, short interest", "SQLite news_sentiment.db (3 d), massive_grouped.db (30 d)"],
            ["Finnhub", "intelligence_finnhub.py", "Earnings calendar, insider transactions (Form 4), company profile", "In-process dict, 24 h"],
            ["Alpaca News", "intelligence.py:70-118", "Per-symbol financial headlines (2 d lookback)", "Bundled in IntelligenceAggregator"],
            ["Alpaca Market Data", "alpaca_client.py", "Real-time quotes, bars, options chain, account state", "BarStore SQLite (24 h)"],
            ["FRED", "intelligence.py:122-154", "Macro series (VIX VIXCLS, DGS10, DFF, fed funds rate)", "Direct CSV; regime detector consumes VIX"],
            ["GDELT 2.0", "intelligence.py:157-196", "Macro tone score (-10..+10) from \"stock market OR S&amp;P 500 OR Federal Reserve\" 24h window", "30 min in-process"],
            ["ApeWisdom", "intelligence_apewisdom.py", "Reddit r/CryptoCurrency and r/WallStreetBets mention spikes", "5 min snapshot per scan"],
            ["Alternative.me", "intel_gates.py:182-219", "Crypto Fear &amp; Greed Index (0&ndash;100)", "1 h in-process"],
            ["CoinGecko", "intel_gates.py:322-377", "Per-coin sentiment_votes_up_percentage", "30 min in-process (OFF by default)"],
            ["SEC EDGAR", "intelligence.py:199-249", "Recent Form 4 insider filings (RSS)", "Bundled context only, no gate"],
            ["Truth Social", "vip_tweets.py", "VIP handle posts; keyword severity scoring (HIGH / MED)", "data/vip_seen.json (persistent)"],
        ],
        col_widths=[1.1 * inch, 1.3 * inch, 2.5 * inch, 1.4 * inch],
    ))

    s.append(H2("3.2 The six per-trade intel gates"))

    s.append(H3("Tier 1 &mdash; fast, always-on"))
    s.append(make_table(
        [
            ["Gate", "Source", "Rule", "Default", "Workflow"],
            ["Stock Earnings", "Finnhub /calendar/earnings", "Skip if earnings in next N trading days", "ON, lookahead 5 d", "Stocks, Wheel"],
            ["Crypto Fear &amp; Greed", "Alternative.me", "Skip if index outside [floor, ceiling]", "ON, [20, 80]", "Crypto"],
            ["Crypto Reddit Spike", "ApeWisdom r/CryptoCurrency", "Skip if mentions &ge; multiplier &times; 24h-ago", "ON, &times;2.0", "Crypto"],
        ],
        col_widths=[1.2 * inch, 1.5 * inch, 1.7 * inch, 1.0 * inch, 0.9 * inch],
    ))

    s.append(H3("Tier 2 &mdash; defensive, conditionally enabled"))
    s.append(make_table(
        [
            ["Gate", "Source", "Rule", "Default", "Workflow"],
            ["Stock Insider Cluster", "Finnhub /stock/insider-transactions", "Skip if &ge; 5 insider sells in 90 d", "OFF (noisy on 10b5-1)", "Stocks"],
            ["Macro Shock", "GDELT 2.0 doc API", "Skip ALL entries if tone &le; threshold", "ON, &le; &minus;3.0", "All"],
            ["CoinGecko Sentiment", "CoinGecko v3", "Skip if sentiment_votes_up_pct &lt; floor", "OFF, floor 50%", "Crypto"],
        ],
        col_widths=[1.2 * inch, 1.5 * inch, 1.7 * inch, 1.0 * inch, 0.9 * inch],
    ))

    s.append(H2("3.3 Per-workflow gate matrix"))
    s.append(make_table(
        [
            ["Gate / Filter", "Stocks", "Crypto", "Wheel"],
            ["Existing position / pending order / traded-today", "yes", "yes", "yes (cycle phase)"],
            ["Insufficient bars (< 26)", "yes", "yes", "n/a (chain-driven)"],
            ["Strategy signal HOLD", "yes", "yes", "yes (preflight)"],
            ["Polygon news sentiment floor", "<b>yes</b> (-0.5)", "no (bypass)", "yes (-0.3, looser)"],
            ["Macro shock (GDELT)", "yes", "yes", "yes"],
            ["Earnings gate (Finnhub)", "yes (5 d)", "no", "yes (today &mdash; expiration+2)"],
            ["Insider cluster (Finnhub)", "off by default", "no", "off"],
            ["Crypto Fear &amp; Greed", "no", "yes [20, 80]", "no"],
            ["Reddit r/CryptoCurrency spike", "no", "yes (&times;2.0)", "no"],
            ["WSB r/WallStreetBets spike", "no", "no", "<b>yes</b> (&times;2.0)"],
            ["CoinGecko community sentiment", "no", "off by default", "no"],
            ["Regime allowed", "any", "any", "trending_up, sideways"],
            ["VIX band", "n/a", "n/a", "[15, 30]"],
            ["IV rank floor", "n/a", "n/a", "&ge; 30 over 252 d"],
            ["Sector cap", "n/a", "n/a", "25% per sector"],
            ["Risk manager (sizing, caps)", "yes", "yes", "yes (collateral)"],
        ],
        col_widths=[2.3 * inch, 1.4 * inch, 1.4 * inch, 1.4 * inch],
    ))

    s.append(H2("3.4 VIP listener &mdash; alert-only, never auto-trades"))
    s.append(P(
        "<font face='Courier'>VipListenerRole</font> polls Truth Social RSS for "
        "configured handles (currently only Donald Trump in "
        "<font face='Courier'>strategy/vip_handles.yaml</font>) every 30 minutes "
        "during US market hours. The keyword severity scorer in "
        "<font face='Courier'>vip_tweets.py</font> tags posts as HIGH on tokens "
        "like <i>tariff, sanction, fed chair, executive order, $spy, $btc</i>, "
        "and MED on macro words like <i>inflation, recession, jobs report, "
        "deficit</i>. <b>This system never auto-trades, never auto-halts and "
        "never auto-vetoes</b> &mdash; it emits a critical email and writes "
        "the post id to <font face='Courier'>data/vip_seen.json</font> so it "
        "doesn&rsquo;t re-fire on the next poll."
    ))

    s.append(PageBreak())

    # ============================================================
    # 4. THE LAB
    # ============================================================
    s.append(H1("4. The Lab &mdash; automated strategy evolution"))

    s.append(P(
        "The Lab is a separate launchd-managed process whose job is to find "
        "better strategy parameters than what the daemon is currently running, "
        "validate them across multiple time slices, and either flag promising "
        "candidates or auto-promote them into the live config. It runs four "
        "scheduled jobs per day."
    ))

    s.append(H2("4.1 Lab schedule"))
    s.append(make_table(
        [
            ["Time (ET)", "Job", "Role", "Outcome"],
            ["02:00 daily", "param_search", "ParamOptimizerRole", "100 Optuna TPE trials &rarr; leaderboard rows + 1 EvolutionRun summary"],
            ["02:45 daily", "auto_promote", "PromoterRole", "Read top leaderboard, evaluate gates, possibly write paper_active.json"],
            ["05:00 daily", "calibrate", "CalibratorRole", "Spearman drift score between predictions and 30-trade realized P&amp;L &rarr; halt promotions if drift > 0.3"],
            ["06:00 Saturday", "saturday_evolve", "StrategyArchitect &rarr; CodeReviewer", "Claude generates 1&ndash;3 templates &rarr; AST + sandbox validation"],
        ],
        col_widths=[1.0 * inch, 1.2 * inch, 1.4 * inch, 2.9 * inch],
    ))

    s.append(H2("4.2 Parameter search &mdash; Optuna TPE"))
    s.append(P(
        "<font face='Courier'>ParamOptimizerRole</font> "
        "(<font face='Courier'>roles/param_optimizer.py:41-135</font>) builds an "
        "<font face='Courier'>optuna.create_study</font> with "
        "<font face='Courier'>TPESampler</font> in <i>maximize</i> mode and runs "
        "<font face='Courier'>n_trials=100</font> by default. The search space "
        "lives in <font face='Courier'>param_space.py</font>; only the "
        "<i>momentum</i> template is currently enumerated:"
    ))
    s.append(CODE(
        "PARAM_SPACE['momentum'] = {\n"
        "    'rsi_lower':       (50, 60, float),\n"
        "    'rsi_upper':       (65, 75, float),\n"
        "    'ema_period':      (15, 30, int),\n"
        "    'stop_pct':        (3.0, 7.0, float),   # percent, not fraction\n"
        "    'sentiment_floor': (-1.0, 0.0, float),\n"
        "}"
    ))
    s.append(B("Each trial calls <font face='Courier'>BacktestEngineerRole.safe_run</font> for a 6-fold walk-forward backtest, computes a fitness score, writes a leaderboard row."))
    s.append(B("Trials whose backtest fails are converted to <font face='Courier'>optuna.TrialPruned()</font> &mdash; pruned, not penalised."))
    s.append(B("The lab universe is read from <font face='Courier'>strategy/opportunities.md</font> (top 25 stocks, curated by the Universe Curator at 07:30 ET); fallback baseline is <font face='Courier'>SPY, QQQ, AAPL, MSFT, AMD, NVDA, GOOGL, META, JPM, JNJ</font>."))

    s.append(H2("4.3 Walk-forward mechanics"))
    s.append(P(
        "<font face='Courier'>walkforward.default_folds</font> "
        "(<font face='Courier'>walkforward.py:27-54</font>) builds "
        "<b>6 folds</b> with <b>train=12 months, test=3 months</b>, walking the "
        "cursor forward by the test window each fold &mdash; tiled, "
        "non-overlapping test slices. Before each fold, "
        "<font face='Courier'>_ensure_bars_warmed</font> backfills the BarStore "
        "from Alpaca (or the Polygon/Massive loader) for any missing symbols "
        "and drops symbols whose fetch fails."
    ))

    s.append(H2("4.4 Fitness function"))
    s.append(CODE(
        "fitness = alpha_vs_spy_x  +  0.5 * sortino  -  0.5 * dd_penalty\n"
        "# dd_penalty = max(0, max_dd_pct - 20.0)  # only above 20% drawdown\n"
        "\n"
        "# Hard promotion gate (all three must hold):\n"
        "alpha_vs_spy_x >= 1.5\n"
        "sortino        >= 1.0\n"
        "max_dd_pct     <= 20.0\n"
    ))
    s.append(B("<font face='Courier'>alpha_vs_spy_x</font> = strategy_return / SPY period return, clamped to 100.0 when SPY is flat."))
    s.append(B("<font face='Courier'>sortino</font> annualised via &radic;252 using only negative daily returns for the downside std."))
    s.append(B("<font face='Courier'>max_dd_pct</font> from peak-tracking on equity curve; reported as worst-case <b>max</b> across folds."))
    s.append(B("Aggregate alpha and sortino reported as <b>mean</b> across folds; drawdown reported as <b>max</b> (worst-case treatment)."))

    s.append(H2("4.5 Promotion &mdash; auto and manual"))
    s.append(H3("Auto (PromoterRole at 02:45 ET)"))
    s.append(B("Halt gate first: any active <font face='Courier'>PromoterHalt</font> row &rarr; skip with reason <font face='Courier'>halted_by_calibrator</font>."))
    s.append(B("Hard threshold gate (alpha &ge; 1.5x, sortino &ge; 1.0, dd &le; 20%)."))
    s.append(B("<b>10% delta gate:</b> candidate fitness must beat incumbent by &ge; 10% (<font face='Courier'>MIN_FITNESS_DELTA = 0.10</font>) &mdash; this stops nightly leaderboard noise from churning the active config."))
    s.append(B("First-time promotion (no incumbent or incumbent fitness &le; 0) auto-passes the delta gate."))
    s.append(B("On approval: <font face='Courier'>promote_atomically</font> writes via tmp+rename, stamps <font face='Courier'>version='auto-YYYYMMDD-HHMMSS'</font>, <font face='Courier'>promoted_by='lab-promoter'</font>, sends a <b>Strategy Promotion email</b> with params + risk-caps diff."))
    s.append(B("<font face='Courier'>LabPromotionStore</font> records the promotion for first-24 h validation: scans, entries, near-misses, validated_at."))

    s.append(H3("Manual CLI (promote_cli.py)"))
    s.append(B("<font face='Courier'>bot promote paper</font> &mdash; replicates the auto logic on demand."))
    s.append(B("<font face='Courier'>bot promote live</font> &mdash; <b>three independent gates</b>: (1) <font face='Courier'>ALPACA_LIVE_API_KEY/SECRET</font> set, (2) <font face='Courier'>--i-know-this-is-real-money</font> flag, (3) operator types literal <font face='Courier'>'YES, FLIP TO LIVE'</font> exactly."))
    s.append(B("Live promotion locks <b>stricter caps</b>: max_position 5% (vs paper 10%), daily_loss 1.5% (vs 2%), max_drawdown 10% (vs 20%) &mdash; halved across the board."))
    s.append(B("Writes a <font face='Courier'>ConfigHistory</font> audit row."))

    s.append(H2("4.6 Calibration &mdash; drift detection"))
    s.append(P(
        "<font face='Courier'>CalibratorRole</font> at 05:00 ET pairs the most-recent leaderboard&rsquo;s "
        "<font face='Courier'>per_trade_predictions_json</font> with realised "
        "P&amp;L from <font face='Courier'>data/closed_trades.db</font> over a "
        "rolling 30-trade / 30-day window. The drift score is the "
        "<b>Spearman rank correlation</b> between predicted and realised "
        "per-trade returns, implemented in pure numpy in "
        "<font face='Courier'>calibration.py:13-31</font>."
    ))
    s.append(make_table(
        [
            ["Spearman corr", "Severity", "Action"],
            ["&gt; 0.5", "ok", "no action"],
            ["0.3 &le; corr &le; 0.5", "warning", "log + email"],
            ["&lt; 0.3", "high", "<b>insert PromoterHalt with halted_until = now + 7 days</b>"],
            ["n &lt; 10 trades", "insufficient_data", "no action"],
        ],
        col_widths=[1.4 * inch, 1.4 * inch, 3.2 * inch],
    ))

    s.append(H2("4.7 LLM-driven strategy generation"))
    s.append(P(
        "On Saturdays at 06:00 ET, <font face='Courier'>StrategyArchitectRole</font> "
        "asks Claude to generate 1&ndash;3 net-new strategy templates conforming "
        "to a strict signature: <font face='Courier'>evaluate(symbol, ind, equity) "
        "-&gt; Signal</font> and <font face='Courier'>from_params</font> classmethod. "
        "The system prompt enforces hard constraints in plain English."
    ))
    s.append(B("Models: <font face='Courier'>claude-opus-4-7</font> for the architect, <font face='Courier'>claude-haiku-4-5-20251001</font> for the tone analyst &mdash; overridable via env."))
    s.append(B("Imports allowed: pandas, numpy, ta, math, datetime, dataclasses, typing, decimal, enum."))
    s.append(B("Imports prohibited: os, sys, subprocess, requests, urllib, eval, exec, <font face='Courier'>__import__</font>, open."))
    s.append(B("No I/O. No future bars. 5-year backtest must run in &lt; 30 s."))
    s.append(B("Output is strict JSON; parser strips <font face='Courier'>```json</font> fences and falls back to a regex search."))
    s.append(B("Every API call goes through <font face='Courier'>anthropic_client.py</font> with retry, cost logging (<font face='Courier'>AnthropicCostLog</font> table), and a <font face='Courier'>CostHalt</font> budget cap."))

    s.append(H2("4.8 Generated-code validation pipeline"))
    s.append(P(
        "<font face='Courier'>CodeReviewerRole</font> runs three deterministic "
        "checks on each pending <font face='Courier'>TemplateProposal</font>:"
    ))
    s.append(B("<b>1. AST allowlist</b> (<font face='Courier'>ast_validator.py:67-94</font>): walks the AST, rejects non-allowlisted import roots, rejects calls to <font face='Courier'>FORBIDDEN_CALLS = {eval, exec, compile, __import__, open}</font>, catches <font face='Courier'>__builtins__.__import__</font> attribute trickery."))
    s.append(B("<b>2. Sandbox runtime</b> (<font face='Courier'>sandbox_runner.py</font>): writes source + tests to a tempdir, runs <font face='Courier'>python -m pytest</font> in a subprocess with <font face='Courier'>walltime_s=30</font>, <font face='Courier'>mem_mb=512</font> via <font face='Courier'>RLIMIT_CPU</font> and <font face='Courier'>RLIMIT_AS</font>."))
    s.append(B("<b>3. Disposition</b>: accepted modules land in <font face='Courier'>src/trading_bot/strategies/_evolved/&lt;name&gt;/</font>; rejects go to <font face='Courier'>_archive/</font>. <font face='Courier'>review_status</font> is updated; on accept, <font face='Courier'>accepted_at</font> is stamped."))

    s.append(PageBreak())

    # ============================================================
    # 5. STRATEGIES & ALGORITHMS
    # ============================================================
    s.append(H1("5. Strategies &amp; algorithms &mdash; concrete rules"))

    s.append(H2("5.1 MomentumStrategy (active in trending_up)"))
    s.append(P("<font face='Courier'>strategy.py:24-105</font>. Long-only, integer-share quantities."))
    s.append(make_table(
        [
            ["Param", "Default", "Lab range"],
            ["rsi_lower", "55", "50&ndash;60"],
            ["rsi_upper", "70", "65&ndash;75"],
            ["ema_period", "20", "15&ndash;30"],
            ["per_trade_risk_pct", "0.5", "fixed"],
            ["stop_pct", "0.05 (5%)", "3%&ndash;7%"],
            ["max_concentration_pct", "4.5", "fixed"],
            ["sentiment_floor", "&minus;0.5", "&minus;1.0&hellip;0.0"],
        ],
        col_widths=[2.0 * inch, 1.5 * inch, 2.5 * inch],
    ))
    s.append(P("<b>Entry rules</b> (all four must pass):"))
    s.append(B("RSI(14) &isin; [rsi_lower, rsi_upper]"))
    s.append(B("MACD line &gt; MACD signal"))
    s.append(B("Last close &gt; EMA(20)"))
    s.append(B("5-day return &gt; 0"))
    s.append(P("<b>Stop:</b> <font face='Courier'>max(EMA20, entry &times; (1 &minus; stop_pct))</font> &mdash; whichever is higher (tighter)."))

    s.append(H2("5.2 MeanReversionStrategy (currently disabled)"))
    s.append(P(
        "<font face='Courier'>strategy.py:108-165</font>. RSI &lt; [25, 35], "
        "close &le; 1.01 &times; EMA20, 5-day return &ge; &minus;5%. Same sizing math as "
        "Momentum but with a flat stop_pct=4%. <b>Currently returns None for "
        "all regimes</b> &mdash; backtest profit factor of 0.66 over 49 trades "
        "led to disabling pending rework. Returning None means &ldquo;no entries this "
        "regime&rdquo; but existing positions are still managed by exits/stops."
    ))

    s.append(H2("5.3 Strategy router"))
    s.append(make_table(
        [
            ["Regime", "Strategy returned", "Notes"],
            ["trending_up", "MomentumStrategy", "Active path"],
            ["sideways", "None", "Mean-reversion disabled (PF 0.66)"],
            ["trending_down", "None", "Insufficient backtest data"],
            ["risk_off", "None", "Defensive &mdash; no new entries"],
        ],
        col_widths=[1.4 * inch, 2.0 * inch, 2.6 * inch],
    ))

    s.append(H2("5.4 Regime detection (regime.py)"))
    s.append(P(
        "Inputs: SPY 250-day bars + optional VIX (FRED VIXCLS). Logic in "
        "<font face='Courier'>detect_regime_from_bars</font> "
        "(<font face='Courier'>regime.py:46-132</font>):"
    ))
    s.append(B("VIX &gt; <b>28</b> &rarr; forced <b>risk_off</b> (highest priority)."))
    s.append(B("20-day annualised realised vol &gt; <b>22%</b> (Phase 0b change from 30%) OR 20-day drawdown &lt; &minus;10% &rarr; <b>risk_off</b>."))
    s.append(B("close &gt; EMA50 &gt; EMA200, vol &lt; 25%, VIX &le; 22 &rarr; <b>trending_up</b>."))
    s.append(B("close &lt; EMA50 &lt; EMA200 &rarr; <b>trending_down</b>."))
    s.append(B("Otherwise &rarr; <b>sideways</b>."))

    s.append(H2("5.5 Backtest harness (backtest/simulator.py)"))
    s.append(P(
        "Day-by-day replay that reuses the live entry strategy, regime detector, "
        "indicators, and risk manager <i>verbatim</i> &mdash; only the day clock, "
        "simulated portfolio, and exit-leg logic are new. This means a backtest "
        "result reflects what <i>would actually</i> happen in production."
    ))
    s.append(B("<b>Trading-day clock</b> from <font face='Courier'>BarStore.trading_dates(SPY)</font> &mdash; honors real holidays."))
    s.append(B("<b>Daily loop</b>: resolve open positions &rarr; mark to market &rarr; record P&amp;L &rarr; update streaks &rarr; detect regime (with VIX backfill if missing) &rarr; check halt (loss caps) &rarr; if not halted, attempt entries."))
    s.append(B("<b>Exit resolution</b>: missing bar &rarr; time exit at max_hold_days; otherwise check stop first (low &le; stop &rarr; \"stop\"), then take-profit (high &ge; tp &rarr; \"tp\"), then time. <b>Stop wins on intra-bar conflict</b> (conservative)."))
    s.append(B("<b>Optional trailing-stop logic</b> (off by default per empirical sweep): ratchet stop to break-even at +3% peak unrealised, then to <font face='Courier'>peak &times; giveback_fraction</font> at +5%."))
    s.append(B("<b>Entry execution at next-day open</b> avoids look-ahead bias. 2:1 R:R take-profit (<font face='Courier'>tp = entry + 2 &times; risk_per_share</font>)."))
    s.append(B("Per-strategy, per-regime and per-asset-class metrics in <font face='Courier'>backtest/metrics.py</font>: win rate, profit factor, expectancy, Sharpe (annualised), max drawdown."))

    s.append(PageBreak())

    # ============================================================
    # 6. RISK & PORTFOLIO
    # ============================================================
    s.append(H1("6. Risk &amp; portfolio management"))

    s.append(H2("6.1 RiskManager.check &mdash; every trade gate"))
    s.append(P(
        "<font face='Courier'>risk_manager.py:26-51</font>. The single "
        "<font face='Courier'>check(order, account, positions, state, regime)</font> "
        "method is called by every workflow before submitting an order."
    ))
    s.append(B("<b>1. Halted flag:</b> any halt (manual or auto) &rarr; <font face='Courier'>RiskRuleViolation</font>."))
    s.append(B("<b>2. Per-trade risk:</b> <font face='Courier'>(entry &minus; stop) &times; qty &le; 1% account equity</font>."))
    s.append(B("<b>3. Max position:</b> <font face='Courier'>notional &le; 10% account equity</font>."))
    s.append(B("<b>4. Concentration:</b> <font face='Courier'>symbol notional &le; 5% account equity</font>."))
    s.append(B("<b>5. Asset-class caps:</b> regime-dependent (see table below)."))
    s.append(B("<b>6. Loss limits:</b> daily &ge; &minus;2%, weekly &ge; &minus;5%."))

    s.append(H2("6.2 Regime-dependent allocation caps"))
    s.append(make_table(
        [
            ["Regime", "Stocks", "Crypto", "Options", "Cash floor"],
            ["trending_up", "60%", "25%", "15%", "0%"],
            ["sideways", "40%", "20%", "20%", "20%"],
            ["trending_down", "30%", "15%", "10%", "45%"],
            ["risk_off", "10%", "5%", "0%", "85%"],
        ],
        col_widths=[1.6 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch, 1.4 * inch],
    ))

    s.append(H2("6.3 Position protection (auto-stops)"))
    s.append(P(
        "<font face='Courier'>OrderStewardRole</font> runs every "
        "<b>:20 and :50</b> of every hour, 24/7 (<font face='Courier'>scheduler_jobs.py:78</font>) "
        "and calls <font face='Courier'>position_protection.py</font>. For every "
        "unprotected position it computes a strategy-aligned stop "
        "(<font face='Courier'>max(EMA20, close &times; (1 &minus; stop_pct))</font>) "
        "and either places it (<i>protect</i>) or market-closes the position "
        "(<i>flatten</i>). Off-hours equity flattens are deferred &mdash; Alpaca "
        "rejects equity market orders outside RTH; crypto is protected 24/7. "
        "Failures are per-symbol so one bad name doesn&rsquo;t abort the sweep."
    ))

    s.append(H2("6.4 Strategy Coach &amp; the Hold-SPY fallback"))
    s.append(P(
        "<font face='Courier'>StrategyCoachRole</font> runs at 06:00 ET daily "
        "and computes a 30-day realised alpha multiplier "
        "(strategy_return / SPY_return) from "
        "<font face='Courier'>closed_trades.db</font>. It flips a "
        "<font face='Courier'>FallbackFlag</font> with hysteresis: <b>enter</b> "
        "fallback when alpha &lt; 1.5&times; SPY today; <b>exit</b> when alpha "
        "&gt; 1.65&times; today AND &gt; 1.5&times; for 5 sustained trading days. "
        "<font face='Courier'>HoldSpyCoordinatorRole</font> at 15:55 ET runs the "
        "five-day exit/reverse transition idempotently &mdash; sells 1/5 of "
        "active positions per day and rebuys SPY on the way in, flips back on "
        "the way out."
    ))

    s.append(PageBreak())

    # ============================================================
    # 7. STATE, PERSISTENCE, REPORTING
    # ============================================================
    s.append(H1("7. State, persistence &amp; reporting"))

    s.append(H2("7.1 SQLite schema (state.db)"))
    s.append(P(
        "Single SQLAlchemy ORM in <font face='Courier'>state_db.py</font>, 16+ "
        "tables, WAL mode for concurrent read/write between daemon, lab and "
        "supervisor. Notable tables:"
    ))
    s.append(B("<b>Heartbeat, RoleRun, RoleKpi</b> &mdash; daemon health and per-role telemetry."))
    s.append(B("<b>EquityHighWaterMark, RegimeHistory, ConfigHistory</b> &mdash; account and config audit."))
    s.append(B("<b>Leaderboard, EvolutionRun, CalibrationRun, PromoterHalt</b> &mdash; lab tables; Leaderboard stores <font face='Courier'>per_trade_predictions_json</font> for the calibrator."))
    s.append(B("<b>FallbackFlag (append-only), HoldSpyTransitionState</b> &mdash; Phase 4 hold-SPY coordinator."))
    s.append(B("<b>AnthropicCostLog, CostHalt, TemplateProposal</b> &mdash; LLM cost tracking + proposal pipeline."))
    s.append(B("<b>OptionFill, OptionIvHistory, WheelCycle, WheelUniverseCache</b> &mdash; Phase 5 wheel state."))
    s.append(B("<b>SectorCache</b> &mdash; yfinance-backed sector classifier with 14 d TTL."))
    s.append(B("<b>EmailsSent, AlertsPending, AlertsSent</b> &mdash; idempotent alert pipeline."))

    s.append(H2("7.2 Alembic migrations (12 total)"))
    s.append(make_table(
        [
            ["#", "Name", "Adds"],
            ["001", "initial_schema", "config_history, equity_high_water_mark, heartbeats, regime_history, role_kpis, role_runs"],
            ["002", "leaderboard_and_evolution_runs", "Lab primary tables"],
            ["003", "calibration_runs", "calibration_runs, promoter_halts"],
            ["004", "fallback_flags", "fallback_flags, hold_spy_transitions"],
            ["005", "anthropic_and_proposals", "anthropic_cost_log, cost_halts, template_proposals"],
            ["006", "emails_sent", "emails_sent audit table"],
            ["007", "trade_journal_unique_order_id", "UNIQUE(entry_order_id) + dedup cleanup"],
            ["008", "lab_promotions", "first-24h validation tracking"],
            ["009", "schedule_audits", "cron-fire audit table"],
            ["010", "alerts_pending", "alerts_pending, alerts_sent, bot_meta"],
            ["011", "wheel_strategy", "option_fills, option_iv_history, wheel_cycles, wheel_universe_cache"],
            ["012", "sector_cache", "sector_cache (yfinance + ETF static map)"],
        ],
        col_widths=[0.5 * inch, 2.0 * inch, 4.0 * inch],
    ))

    s.append(H2("7.3 Runtime files (data/)"))
    s.append(B("<font face='Courier'>heartbeat.json</font> &mdash; <font face='Courier'>{ts, pid, version, last_action}</font>; written every 60 s via tmp+rename."))
    s.append(B("<font face='Courier'>last_scan.json</font> &mdash; most-recent scan&rsquo;s regime, universe, per-symbol decisions."))
    s.append(B("<font face='Courier'>portfolio_snapshot.json</font> &mdash; equity + positions, written by PortfolioMonitorRole."))
    s.append(B("<font face='Courier'>paper_active.json</font> + <font face='Courier'>.pre-promote-backup</font> &mdash; active config, replaced atomically by promoter."))
    s.append(B("<font face='Courier'>vip_seen.json</font> &mdash; last-seen Truth Social post ids per handle (idempotency)."))
    s.append(B("Other DBs: <font face='Courier'>backtest_bars.db</font>, <font face='Courier'>backtest_trades.db</font>, <font face='Courier'>closed_trades.db</font>, <font face='Courier'>massive_grouped.db</font>, <font face='Courier'>news_sentiment.db</font>, <font face='Courier'>trade_journal.db</font>."))

    s.append(H2("7.4 Reporting &amp; email"))
    s.append(P(
        "Every email is built from a single light-first / dark-mode-override "
        "shell in <font face='Courier'>email_shell.py</font>. The recent commits "
        "<font face='Courier'>fe1e797</font> ('every email dark-themed + EOD "
        "session review section') and <font face='Courier'>da1048e</font> "
        "('dark-mode-native overhaul + daily digest 16:30 ET') confirm the "
        "current cadence."
    ))
    s.append(make_table(
        [
            ["Email", "Cadence", "Builder", "Highlights"],
            ["Midday Snapshot", "12:00 ET weekdays", "email_midday.py", "KPI grid, today&rsquo;s trades, open positions, watchlist signals, optional wheel watchlist"],
            ["Daily Digest", "16:30 ET weekdays", "email_digest.py:184-454", "13 sections: KPI grid, EOD <b>Session Review</b>, equity sparkline, risk gauges, regime, positions, trades, wheel cycles, lab activity, watchlist movers, sentiment table, system health, footer"],
            ["Strategy Promotion", "On every promotion", "email_promotion.py", "Params diff + risk-caps diff with green/red arrows, &lsquo;Watch first 24 h&rsquo; callout"],
            ["Per-trade Fill", "On every fill", "email_fill.py", "Equity vs option branches; subjects: <i>STOP HIT AAPL ...</i>, <i>BUY MSFT ...</i>, <i>CSP Opened SPY ...</i>"],
            ["Critical Alert", "On stall / drawdown breach", "email_critical.py", "LOW/MEDIUM/HIGH/CRITICAL color rails; throttled to once per hour while condition persists"],
        ],
        col_widths=[1.2 * inch, 1.4 * inch, 1.4 * inch, 2.5 * inch],
    ))

    s.append(H2("7.5 EOD Session Review"))
    s.append(P(
        "<font face='Courier'>session_summary.review_session(ctx)</font> is "
        "rule-based, no LLM round-trip, and emits a three-column "
        "&ldquo;What went well / what went wrong / what could be better&rdquo; "
        "block in the daily digest. Severity rail color depends on the count of "
        "&lsquo;wrong&rsquo; items (good / warn / bad). Checks include: P&amp;L "
        "magnitude, risk-cap proximity (75% of cap &rarr; warn), 7-day closed-trade "
        "win rate (&ge; 60% well, &le; 40% wrong), errors, daemon blips, schedule "
        "audit warnings, regime context, VIX context (above threshold &rarr; warn; "
        "below 13 &rarr; wheel premiums thin), wheel MTD P&amp;L, wheel collateral "
        "approaching cap (&gt; 18%), pending strategy promotions. Always inserts "
        "at least one positive observation as a safety net."
    ))

    s.append(H2("7.6 Dashboard"))
    s.append(P(
        "<font face='Courier'>src/trading_bot/dashboard/app.py</font> is a FastAPI "
        "app with a TTL-driven snapshot cache and a background refresher thread. "
        "Routes: <font face='Courier'>index</font>, <font face='Courier'>architecture</font>, "
        "<font face='Courier'>refresh</font>, HTMX fragment routes, and JSON "
        "APIs <font face='Courier'>/api/snapshot</font>, "
        "<font face='Courier'>/api/equity_curve</font>, "
        "<font face='Courier'>/api/market_session</font>. "
        "<font face='Courier'>_lab_views()</font> reuses "
        "<font face='Courier'>lab_data.py</font> so the dashboard and digest "
        "show identical numbers &mdash; single source of truth."
    ))

    s.append(PageBreak())

    # ============================================================
    # 8. SCHEDULE TIMETABLE & ROLES
    # ============================================================
    s.append(H1("8. Schedule timetable &amp; roles"))

    s.append(H2("8.1 Daemon job timetable (America/New_York)"))
    s.append(make_table(
        [
            ["Time", "Job", "Cadence", "Role"],
            ["Continuous", "heartbeat", "60 s interval", "HealthPulseRole"],
            ["06:00 weekdays", "strategy_coach", "Cron daily", "StrategyCoachRole"],
            ["06:30 weekdays", "massive_refresh", "Cron daily", "UniverseCuratorRole.run_refresh"],
            ["07:30 weekdays", "premarket_rank", "Cron daily", "UniverseCuratorRole.run_rank"],
            ["08:55 weekdays", "news_warm_morning", "Cron daily", "SentimentAnalystRole"],
            ["09:00&ndash;15:00", "intel_scan (stocks)", "Hourly weekdays", "StockScannerRole"],
            ["09:00&ndash;15:00", "portfolio_watch", "Hourly weekdays", "PortfolioMonitorRole"],
            ["09:30 onwards", "crypto_scanner", "Every 30 min, 24/7", "CryptoScannerRole"],
            ["09:45 weekdays", "iv_capture", "Cron daily", "run_iv_capture"],
            ["10:15 weekdays", "wheel_scan", "Cron daily", "run_wheel_scan"],
            ["10:00&ndash;15:00", "wheel_manage", "Every 30 min", "run_wheel_manage"],
            ["12:00 weekdays", "news_warm_midday", "Cron daily", "SentimentAnalystRole"],
            ["12:00 weekdays", "midday_rerank + snapshot", "Cron daily", "UniverseCuratorRole + cli.midday_snapshot_cli"],
            ["09:00&ndash;16:00", "vip_scan", "Every 30 min", "VipListenerRole"],
            ["Every :20, :50", "verify_stops", "Every 30 min, 24/7", "OrderStewardRole"],
            ["15:55 weekdays", "hold_spy_coordinator", "Cron daily", "HoldSpyCoordinatorRole"],
            ["16:05 weekdays", "reconciler", "Cron daily", "reconcile_cli + reconcile_options"],
            ["16:30 weekdays", "daily_digest", "Cron daily", "ReporterRole.run_eod"],
            ["21:30 nightly", "wheel_universe_build", "Cron daily", "run_universe_build"],
            ["21:55 nightly", "reconciler", "Cron daily", "reconcile_cli (final)"],
            ["03:00 Sunday", "log_rotation", "Weekly", "LogRotationRole"],
        ],
        col_widths=[1.3 * inch, 1.6 * inch, 1.4 * inch, 2.2 * inch],
    ))

    s.append(H2("8.2 The role roster (23 roles)"))
    s.append(make_table(
        [
            ["Role file", "Tier", "Process", "Purpose"],
            ["health_pulse.py", "&mdash;", "daemon", "Heartbeat writer (60 s); writes heartbeat.json for stall detection"],
            ["sentiment_analyst.py", "1", "daemon", "Polygon news + sentiment warm-cache (08:55, 12:00 ET) + on-demand"],
            ["universe_curator.py", "1", "daemon", "Maintain tradable list; massive_refresh + rank stage 1/2"],
            ["vip_listener.py", "1", "daemon", "Poll Truth Social RSS every 30 min RTH; flag HIGH-severity posts"],
            ["stock_scanner.py", "2", "daemon", "Hourly equity momentum scan (intel-scan); BUY/HOLD/SKIP"],
            ["crypto_scanner.py", "2", "daemon", "30-min crypto momentum scan, 24/7; no sentiment filter"],
            ["strategy_coach.py", "2", "daemon", "Daily 30 d alpha vs SPY; flip fallback flag with hysteresis"],
            ["order_steward.py", "3", "daemon", "Verify open positions have live stops; cancel stale limit orders"],
            ["hold_spy_coordinator.py", "4", "daemon", "5-day exit/reverse transition when fallback flag flips"],
            ["portfolio_monitor.py", "4", "daemon", "60-min position snapshots + alerts during RTH"],
            ["account_sentinel.py", "6", "supervisor", "Reconcile Alpaca account; write pause.flag on drawdown breach"],
            ["watchdog.py", "6", "supervisor", "Detect daemon stall via heartbeat staleness; kickstart if needed"],
            ["resource_guardian.py", "6", "supervisor", "Disk / DB / network health monitoring"],
            ["schedule_auditor.py", "6", "supervisor", "Verify expected roles ran within grace window"],
            ["reporter.py", "6", "daemon", "Compose + send digest emails (midday 12:00, EOD 16:30)"],
            ["strategy_architect.py", "5", "lab", "Claude API integration for strategy ideation (Saturday)"],
            ["code_reviewer.py", "5", "lab", "AST validation + sandbox runtime for proposed templates"],
            ["backtest_engineer.py", "5", "lab", "Walk-forward testing + optimisation"],
            ["param_optimizer.py", "5", "lab", "Optuna-based parameter tuning"],
            ["calibrator.py", "5", "lab", "Predicted-vs-realised drift detection (Spearman)"],
            ["promoter.py", "5", "lab", "Promote promising strategy variants (auto + manual)"],
            ["runner.py", "&mdash;", "&mdash;", "BaseRole with safe_run + structured error handling"],
            ["base.py", "&mdash;", "&mdash;", "Role dataclasses (RoleResult, ReportCard, Health, HealthStatus)"],
        ],
        col_widths=[1.5 * inch, 0.4 * inch, 0.7 * inch, 3.9 * inch],
    ))

    s.append(PageBreak())

    # ============================================================
    # 9. CONFIG REFERENCE
    # ============================================================
    s.append(H1("9. Configuration reference &mdash; strategy/config.yaml"))

    s.append(H2("9.1 Risk block"))
    s.append(CODE(
        "risk:\n"
        "  daily_loss_limit_pct: 2.0           # halt trading if daily P&L < -2%\n"
        "  weekly_loss_limit_pct: 5.0          # halt trading if weekly P&L < -5%\n"
        "  per_trade_risk_pct: 1.0             # max account at risk per trade\n"
        "  max_position_pct: 10.0              # no single position > 10%\n"
        "  max_symbol_concentration_pct: 5.0   # max 5% in any one symbol\n"
        "  max_consecutive_losing_days: 3      # 3 losers -> reduce sizing 50%\n"
        "  unprotected_stop_pct: 0.05          # auto-stop for unprotected positions"
    ))

    s.append(H2("9.2 Allocation &amp; regime allocations"))
    s.append(CODE(
        "allocation:\n"
        "  stocks_max_pct: 70.0\n"
        "  crypto_max_pct: 30.0\n"
        "  options_max_pct: 20.0\n"
        "  cash_floor_pct:  10.0\n"
        "\n"
        "regime_allocations:\n"
        "  trending_up:    {stocks: 60.0, crypto: 25.0, options: 15.0, cash:  0.0}\n"
        "  trending_down:  {stocks: 30.0, crypto: 15.0, options: 10.0, cash: 45.0}\n"
        "  sideways:       {stocks: 40.0, crypto: 20.0, options: 20.0, cash: 20.0}\n"
        "  risk_off:       {stocks: 10.0, crypto:  5.0, options:  0.0, cash: 85.0}"
    ))

    s.append(H2("9.3 Regime &amp; sentiment"))
    s.append(CODE(
        "regime:\n"
        "  vol_threshold_pct: 22.0  # Phase 0b: was 30; trigger risk_off above\n"
        "\n"
        "strategy:\n"
        "  sentiment_floor: -0.5    # Polygon news -1..+1; null disables filter\n"
        "  sentiment_max_age_days: 3"
    ))

    s.append(H2("9.4 Wheel block"))
    s.append(CODE(
        "wheel:\n"
        "  enabled: true              # paper-only; enabled 2026-04-29\n"
        "  delta_target_low:  0.20\n"
        "  delta_target_high: 0.30\n"
        "  dte_min: 30\n"
        "  dte_max: 45\n"
        "  take_profit_pct: 0.50\n"
        "  dte_force_close: 21\n"
        "  delta_breach_csp: 0.45\n"
        "  delta_breach_cc:  0.55\n"
        "  max_rolls_per_cycle: 2\n"
        "  iv_rank_floor: 30.0\n"
        "  vix_floor:    15.0\n"
        "  vix_ceiling:  30.0\n"
        "  sentiment_floor: -0.3\n"
        "  min_premium_abs: 0.20\n"
        "  min_annualized_yield: 0.12\n"
        "  min_open_interest: 100\n"
        "  universe_cache_hours: 24\n"
        "  wsb_spike_multiplier: 2.0"
    ))

    s.append(H2("9.5 Email &amp; storage"))
    s.append(CODE(
        "email:\n"
        "  to: bharath8887@gmail.com\n"
        "  daily_summary_time_et: \"16:30\"\n"
        "  weekly_summary_day: \"Sunday\"\n"
        "\n"
        "storage:\n"
        "  trade_journal_path: data/trade_journal.db"
    ))

    s.append(H1("Appendix &mdash; design principles"))
    s.append(B("<b>Soft-fail intel:</b> any external-source failure (timeout, 4xx, parse error) lets the entry through &mdash; gates can only veto, never trigger entries."))
    s.append(B("<b>Filter-only gates:</b> nothing in the gate pipeline opens a position that wasn&rsquo;t already signalled. Adding a gate is strictly safer."))
    s.append(B("<b>Per-symbol idempotency:</b> traded-today journal check prevents duplicates on restart (root cause of 2026-04-27 AMD/CLS/AMDL incident)."))
    s.append(B("<b>VIP alerts are human-mediated:</b> Truth Social monitoring never auto-trades, auto-halts or auto-vetoes &mdash; backtest validation required first."))
    s.append(B("<b>Stricter live caps:</b> live promotion locks max_position 5% (vs 10%), daily_loss 1.5% (vs 2%), max_drawdown 10% (vs 20%)."))
    s.append(B("<b>WAL mode</b> on state.db so daemon, lab and supervisor read concurrently without deadlocks."))
    s.append(B("<b>Tmp+rename</b> on heartbeat, paper_active.json, alerts &mdash; atomic file replacement prevents partial-write reads."))
    s.append(B("<b>Single source of truth</b> for lab metrics: <font face='Courier'>lab_data.py</font> feeds both the dashboard and the daily digest."))

    return s


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.75 * inch,
        title="Trading Bot — Project Overview",
        author="Generated 2026-04-29",
    )
    doc.build(build_story(), onFirstPage=on_page, onLaterPages=on_page)
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
