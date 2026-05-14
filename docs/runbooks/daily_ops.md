# Daily operator routine

Time: ~10 minutes/day. Skip on weekends unless an alert pages you.

## Morning (before 09:30 ET)

1. `bot status` — confirm:
   - `halted: false` (or you halted intentionally last night)
   - `active_kills: []`
   - All daemon heartbeats < 1h old
   - Last reconciliation match=1
2. Open the dashboard (http://127.0.0.1:8765/) — eyeball.
3. Glance at Alpaca account in browser — confirm equity matches your
   mental model.
4. If anything looks off → `bot halt --reason "morning check anomaly"`
   and investigate before market open.

## Mid-day

Nothing scheduled. The daemon ticks autonomously. Resist the urge to
override.

## Close (16:00 ET)

Nothing scheduled. Reconciliation runs at 23:00 ET; check tomorrow.

## Evening (optional)

1. If you want to think about new strategies — open the dashboard,
   submit drafts. They register at `research_only`; nothing trades.
2. If you want to tweak risk — `bot risk-profile show` to see current,
   then `bot risk-profile safe` for vacation mode.

## Weekly (Sundays)

1. `python tools/verify_ledger.py` — full chain verify (slow; weekly is
   fine).
2. `python tools/recompute_hashes.py --check` — confirm `policy/HASHES`
   matches contents (paranoia check).
3. Browse `data/ledger/` size growth; mirror size should match within
   10%.
4. Read `data/daemon.log` for the week; look for warnings you missed.

---
**Operator sign-off (mark each weekday completed):**

| Date | Morning check | Reconciliation OK? | Notes |
|---|---|---|---|
| | | | |
| | | | |
