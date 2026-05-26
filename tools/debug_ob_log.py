#!/usr/bin/env python3
"""
ابزار دیاگنوستیک اردربوک — OB Diagnostic & Log Tool
======================================================
این اسکریپت وضعیت کامل پایگاه‌داده اردربوک را بررسی می‌کند
و یک فایل لاگ جامع تولید می‌کند.

اجرا:
    python tools/debug_ob_log.py

خروجی:
    ob_debug.txt   (در ریشه پروژه) ← این فایل را commit و push کنید

    git add ob_debug.txt
    git commit -m "debug: OB diagnostic log"
    git push
"""

import sys
import os
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from io import StringIO

# ── آدرس‌ها ──────────────────────────────────────────────────────────────────
ROOT     = Path(__file__).resolve().parent.parent
DB_PATH  = ROOT / "data" / "arbitrage.db"
LOG_FILE = ROOT / "ob_debug.txt"   # در ریشه پروژه — آسان برای push کردن

# ── Output capture ────────────────────────────────────────────────────────────
buf = StringIO()

def log(msg=""):
    print(msg)
    buf.write(msg + "\n")

# ═════════════════════════════════════════════════════════════════════════════
def section(title):
    bar = "═" * 70
    log(f"\n{bar}")
    log(f"  {title}")
    log(bar)

def check(label, ok, detail=""):
    mark = "✓" if ok else "✗"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  →  {detail}"
    log(line)
    return ok

# ═════════════════════════════════════════════════════════════════════════════
log(f"OB Diagnostic Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
log(f"DB: {DB_PATH}")

# 1. DB exists?
section("1. DATABASE FILE")
if not check("DB file exists", DB_PATH.exists(), str(DB_PATH)):
    log("\n  ✗ ERROR: No database found. Run: python main.py --bootstrap")
    LOG_FILE.write_text(buf.getvalue(), encoding="utf-8")
    sys.exit(1)

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

# 2. Tables
section("2. TABLES")
tables = {r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()}
all_expected = [
    "daily_history", "intraday_trades", "intraday_orderbook",
    "ob_ticks", "snapshots", "nav_cache",
]
for t in all_expected:
    n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in tables else None
    if t in tables:
        check(t, True, f"{n:,} rows")
    else:
        check(t, False, "MISSING — run main.py to auto-create")

# 3. intraday_orderbook structure
section("3. intraday_orderbook COLUMNS")
if "intraday_orderbook" in tables:
    cols = [r[1] for r in db.execute("PRAGMA table_info(intraday_orderbook)")]
    log(f"  Columns ({len(cols)}): {', '.join(cols)}")
    for must in ["nav", "spread_pct", "bid_depth", "ask_depth"]:
        check(f"column '{must}'", must in cols)
else:
    log("  Table missing — skip")

# 4. OB snapshots per date
section("4. OB SNAPSHOTS BY DATE")
if "intraday_orderbook" in tables:
    rows = db.execute("""
        SELECT date, COUNT(*) as n_rows, COUNT(DISTINCT symbol) as n_syms,
               MIN(time) as first_t, MAX(time) as last_t,
               AVG(spread_pct) as avg_spread
        FROM intraday_orderbook
        GROUP BY date ORDER BY date DESC LIMIT 20
    """).fetchall()
    if rows:
        log(f"  {'Date':10s}  {'Rows':>6}  {'Syms':>5}  {'First':>8}  {'Last':>8}  {'AvgSpread':>10}")
        log(f"  {'-'*10}  {'-'*6}  {'-'*5}  {'-'*8}  {'-'*8}  {'-'*10}")
        for r in rows:
            log(f"  {r['date']:10d}  {r['n_rows']:6d}  {r['n_syms']:5d}  "
                f"{r['first_t']:08d}  {r['last_t']:08d}  {r['avg_spread']:10.4f}%")
    else:
        log("  ⚠  No OB snapshots found in database!")
        log("  This means run_scan() has never successfully saved an OB snapshot.")

# 5. OB snapshots per symbol (most recent date)
section("5. OB SNAPSHOTS PER SYMBOL (most recent date)")
if "intraday_orderbook" in tables:
    latest_date_row = db.execute(
        "SELECT MAX(date) as d FROM intraday_orderbook"
    ).fetchone()
    latest_date = latest_date_row["d"] if latest_date_row else None
    if latest_date:
        sym_rows = db.execute("""
            SELECT symbol, COUNT(*) as n, MIN(time) as first, MAX(time) as last,
                   AVG(spread_pct) as avg_sp, AVG(bid_depth+ask_depth) as avg_depth
            FROM intraday_orderbook WHERE date=?
            GROUP BY symbol ORDER BY symbol
        """, (latest_date,)).fetchall()
        log(f"  Date: {latest_date}  ({len(sym_rows)} symbols)")
        log(f"  {'Symbol':14s}  {'Snaps':>6}  {'First':>8}  {'Last':>8}  {'AvgSprd':>8}  {'AvgDepth':>10}")
        log(f"  {'-'*14}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*10}")
        for r in sym_rows:
            log(f"  {r['symbol']:14s}  {r['n']:6d}  {r['first']:08d}  {r['last']:08d}  "
                f"{r['avg_sp']:8.4f}%  {r['avg_depth']:10.0f}")
    else:
        log("  No data")

# 6. Sample OB row quality
section("6. SAMPLE OB ROW (most recent)")
if "intraday_orderbook" in tables:
    sample = db.execute("""
        SELECT * FROM intraday_orderbook
        ORDER BY date DESC, time DESC LIMIT 3
    """).fetchall()
    if sample:
        for row in sample:
            d = dict(row)
            log(f"  {d['symbol']:12s}  date={d['date']}  time={d['time']:06d}")
            log(f"    bid1={d['bid1_price']:>12,.0f}  vol={d['bid1_vol']:>8,}")
            log(f"    ask1={d['ask1_price']:>12,.0f}  vol={d['ask1_vol']:>8,}")
            log(f"    spread={d['spread_pct']:.4f}%  bid_depth={d['bid_depth']:,}  "
                f"ask_depth={d['ask_depth']:,}  nav={d['nav']:,.0f}")
            log()
    else:
        log("  No rows")

# 7. Scan snapshots history
section("7. SCAN HISTORY (snapshots table)")
if "snapshots" in tables:
    scans = db.execute("""
        SELECT date, COUNT(DISTINCT symbol) as n_sym, COUNT(*) as n_rows,
               MIN(scanned_at) as first_scan, MAX(scanned_at) as last_scan
        FROM snapshots
        GROUP BY date ORDER BY date DESC LIMIT 10
    """).fetchall()
    if scans:
        for r in scans:
            log(f"  {r['date']}  symbols={r['n_sym']}  scans={r['n_rows']}  "
                f"{r['first_scan']} → {r['last_scan']}")
    else:
        log("  No scans found")

# 8. daily_history coverage
section("8. DAILY HISTORY COVERAGE")
if "daily_history" in tables:
    dh = db.execute("""
        SELECT COUNT(DISTINCT date) as dates, COUNT(DISTINCT symbol) as syms,
               MIN(date) as mn, MAX(date) as mx
        FROM daily_history
    """).fetchone()
    log(f"  Dates: {dh['dates']}  ({dh['mn']} → {dh['mx']})")
    log(f"  Symbols: {dh['syms']}")

    # Which dates in daily_history have NO OB coverage?
    if "intraday_orderbook" in tables and dh['dates'] > 0:
        dh_dates = {r[0] for r in db.execute(
            "SELECT DISTINCT date FROM daily_history"
        ).fetchall()}
        ob_dates = {r[0] for r in db.execute(
            "SELECT DISTINCT date FROM intraday_orderbook"
        ).fetchall()}
        missing = sorted(dh_dates - ob_dates, reverse=True)
        log(f"\n  Daily-history dates WITHOUT OB coverage: {len(missing)} / {len(dh_dates)}")
        for d in missing[:20]:
            log(f"    {d}")
        if len(missing) > 20:
            log(f"    ... and {len(missing)-20} more")

# 9. Code path check — save_orderbook_snapshot reachability
section("9. CODE PATH CHECK")
try:
    sys.path.insert(0, str(ROOT))
    from database import Database
    db2 = Database(DB_PATH)
    check("Database() instantiation OK", True)
    check("intraday_orderbook table exists post-init",
          "intraday_orderbook" in {
              r[0] for r in sqlite3.connect(DB_PATH).execute(
                  "SELECT name FROM sqlite_master WHERE type='table'"
              ).fetchall()
          })
except Exception as e:
    check("Database() instantiation", False, str(e))

try:
    from data_fetcher import TSETMCFetcher
    check("TSETMCFetcher import OK", True)
except Exception as e:
    check("TSETMCFetcher import", False, str(e))

try:
    from main import run_scan
    import inspect
    src = inspect.getsource(run_scan)
    has_save = "save_orderbook_snapshot" in src
    check("run_scan calls save_orderbook_snapshot", has_save)
except Exception as e:
    check("run_scan source check", False, str(e))

# 10. Summary
section("10. DIAGNOSIS")
ob_count = 0
if "intraday_orderbook" in tables:
    ob_count = db.execute("SELECT COUNT(*) FROM intraday_orderbook").fetchone()[0]

if ob_count == 0:
    log("  ✗ CRITICAL: intraday_orderbook has 0 rows.")
    log()
    if "intraday_orderbook" not in tables:
        log("  Cause: The table did not exist (schema migration was incomplete).")
        log("  Fix:   Update database.py and restart main.py. The table will be")
        log("         created automatically by the new _init() migration code.")
    else:
        log("  Cause: Table exists but no snapshots saved. Possible reasons:")
        log("   a) run_scan() has never been called (no --watch or --serve run)")
        log("   b) All get_best_limits() calls returned empty (network issue)")
        log("   c) Exception in save_orderbook_snapshot silently skipped")
    log()
    log("  Action: Run the backfill tool to populate historical data:")
    log("          python tools/backfill_ob.py")
    log()
    log("  Then run live scanner:")
    log("          python main.py --serve --watch 15")
else:
    log(f"  OB snapshots found: {ob_count:,} rows")
    log("  If some chart points lack OB data, that date has no snapshots.")
    log("  Run backfill to fill gaps: python tools/backfill_ob.py")

db.close()

# Write log file
LOG_FILE.write_text(buf.getvalue(), encoding="utf-8")

print(f"\n{'═'*60}")
print(f"✓  Log saved: {LOG_FILE}")
print()
print("  لطفاً این دستورات را اجرا کنید تا لاگ push شود:")
print()
print("  git add ob_debug.txt")
print("  git commit -m \"debug: OB diagnostic log\"")
print("  git push")
print(f"{'═'*60}")
