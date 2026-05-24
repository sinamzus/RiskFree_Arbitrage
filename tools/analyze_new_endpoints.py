#!/usr/bin/env python3
"""
Targeted analysis of the two newly-discovered working endpoints.

Run on Iran-connected machine:
    python tools/analyze_new_endpoints.py

Output: tools/new_endpoints_report.txt
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import requests

INS_CODE = "34718633636164421"   # كمند
CDN = "https://cdn.tsetmc.com/api"
OUT = Path(__file__).parent / "new_endpoints_report.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, */*",
    "Referer": "https://www.tsetmc.com/",
}
session = requests.Session()
session.headers.update(HEADERS)

lines = []
def log(msg=""): print(msg); lines.append(str(msg))
def sep(t=""): log(); log("─"*70); log(f"  {t}") if t else None; log("─"*70)

# ── today & last trading day ──────────────────────────────────────────────────
today = datetime.now()
dates = []
for d in range(0, 10):
    dd = today - timedelta(days=d)
    if dd.weekday() not in (4, 5):
        dates.append(dd.strftime("%Y%m%d"))
    if len(dates) >= 3:
        break

# ════════════════════════════════════════════════════════════════════════════
# A. GetClosingPriceDailyList — 30-day daily history
# ════════════════════════════════════════════════════════════════════════════
sep("A. GetClosingPriceDailyList — daily history")

for n in (30, 90, 365):
    url = f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{INS_CODE}/{n}"
    r = session.get(url, timeout=20)
    data = r.json()
    items = data.get("closingPriceDaily") or []
    log(f"\n  n={n}: {len(items)} entries")
    if items:
        # Show ALL keys + ALL values for the most recent entry
        latest = max(items, key=lambda x: x.get("dEven", 0))
        log(f"  Latest entry (dEven={latest.get('dEven')}) — ALL fields:")
        for k, v in sorted(latest.items()):
            log(f"    {k:35} = {v}")
        log()
        # Look specifically for NAV fields
        nav_keys = [k for k in latest if any(
            x in k.lower() for x in ("nav", "stat", "cancel", "issue", "redeem", "gelsta")
        )]
        log(f"  NAV-like keys: {nav_keys}")
        # Show last 5 entries (dates + closing price)
        log(f"  Last 5 entries:")
        for e in sorted(items, key=lambda x: x.get("dEven", 0), reverse=True)[:5]:
            log(f"    dEven={e.get('dEven')}  pClosing={e.get('pClosing')}  "
                f"priceYesterday={e.get('priceYesterday')}  "
                f"qTotTran5J={e.get('qTotTran5J')}  "
                f"priceChange={e.get('priceChange')}")

# ════════════════════════════════════════════════════════════════════════════
# B. GetClosingPriceDaily — single date
# ════════════════════════════════════════════════════════════════════════════
sep("B. GetClosingPriceDaily — single date (full field dump)")

for date_str in dates[:2]:
    url = f"{CDN}/ClosingPrice/GetClosingPriceDaily/{INS_CODE}/{date_str}"
    r = session.get(url, timeout=20)
    if r.status_code != 200:
        log(f"  {date_str}: HTTP {r.status_code}")
        continue
    data = r.json()
    entry = data.get("closingPriceDaily") or {}
    log(f"\n  Date {date_str} — ALL {len(entry)} fields:")
    for k, v in sorted(entry.items()):
        log(f"    {k:35} = {v}")

# ════════════════════════════════════════════════════════════════════════════
# C. GetTradeHistory — intraday tick data
# ════════════════════════════════════════════════════════════════════════════
sep("C. GetTradeHistory — intraday ticks (full field dump + stats)")

url = f"{CDN}/Trade/GetTradeHistory/{INS_CODE}/{dates[0]}/false"
r = session.get(url, timeout=30)
data = r.json()
trades = data.get("tradeHistory") or []
log(f"\n  {dates[0]}: {len(trades)} trades, {len(r.content)} bytes")

if trades:
    log(f"\n  ALL fields in one trade record:")
    for k, v in sorted(trades[0].items()):
        log(f"    {k:35} = {v}")

    # Stats
    prices  = [t["pTran"] for t in trades if t.get("pTran")]
    volumes = [t["qTitTran"] for t in trades if t.get("qTitTran")]
    times   = [t["hEven"] for t in trades if t.get("hEven")]
    log(f"\n  Price range: {min(prices):.0f} – {max(prices):.0f}")
    log(f"  Total volume: {sum(volumes):,}")
    log(f"  Time range:  {min(times)} – {max(times)}  (HHMMSS int)")
    log(f"  First trade: {trades[-1]}")  # oldest
    log(f"  Last trade:  {trades[0]}")   # newest (sorted desc)

    # Show first 10 trades (earliest in the day)
    log(f"\n  First 10 trades of the day (earliest):")
    for t in sorted(trades, key=lambda x: x.get("nTran", 0))[:10]:
        h = str(t.get("hEven", 0)).zfill(6)
        log(f"    {h[:2]}:{h[2:4]}:{h[4:]}  price={t.get('pTran'):,.0f}  "
            f"vol={t.get('qTitTran'):,}  seq={t.get('nTran')}")

# ════════════════════════════════════════════════════════════════════════════
# D. GetTrade — today's live trades
# ════════════════════════════════════════════════════════════════════════════
sep("D. GetTrade — today's live tick stream (first 10 + last 10)")

url = f"{CDN}/Trade/GetTrade/{INS_CODE}"
r = session.get(url, timeout=20)
data = r.json()
trades2 = data.get("trade") or []
log(f"\n  Live trades count: {len(trades2)}, {len(r.content)} bytes")
if trades2:
    log(f"  ALL fields: {list(trades2[0].keys())}")
    log(f"\n  First 5 (oldest):")
    for t in sorted(trades2, key=lambda x: x.get("nTran", 0))[:5]:
        h = str(t.get("hEven", 0)).zfill(6)
        log(f"    {h[:2]}:{h[2:4]}:{h[4:]}  price={t.get('pTran'):,.0f}  vol={t.get('qTitTran'):,}")
    log(f"\n  Last 5 (newest):")
    for t in sorted(trades2, key=lambda x: x.get("nTran", 0), reverse=True)[:5]:
        h = str(t.get("hEven", 0)).zfill(6)
        log(f"    {h[:2]}:{h[2:4]}:{h[4:]}  price={t.get('pTran'):,.0f}  vol={t.get('qTitTran'):,}")

# ════════════════════════════════════════════════════════════════════════════
# E. Discover all fixed-income ETFs via multiple search terms
# ════════════════════════════════════════════════════════════════════════════
sep("E. Fixed-income ETF discovery — exhaustive search")

from urllib.parse import quote
found = {}   # insCode → info

# All search terms that might match fixed-income ETF fund names
keywords = [
    "درآمد ثابت", "صندوق درآمد", "پارند", "كمند", "كيان", "افران",
    "لبخند", "آفاق", "اعتماد", "گنجين", "خاتم", "اوصتا", "فردا",
    "سخند", "ياقوت", "فيروزا", "صايند", "هماي", "امين يكم", "ماني",
    "تصميم", "درآمد", "ثابت",
]

for kw in keywords:
    try:
        url = f"{CDN}/Instrument/GetInstrumentSearch/{quote(kw)}"
        r = session.get(url, timeout=10)
        results = r.json().get("instrumentSearch", [])
        for inst in results:
            name = inst.get("lVal30", "")
            market = inst.get("cgrValCotTitle", "")
            ic = inst.get("insCode", "")
            # Only keep ETF funds (not options, not regular stocks)
            if "صندوق" in name and "اختيار" not in name and ic and ic not in found:
                found[ic] = {
                    "insCode": ic,
                    "symbol":  inst.get("lVal18AFC", "").strip(),
                    "name":    name.strip(),
                    "market":  market,
                }
    except Exception as e:
        log(f"  Error for '{kw}': {e}")

log(f"\n  Total unique ETF funds found: {len(found)}")
log()

# Separate fixed-income from others
fi    = {ic: f for ic, f in found.items()
         if any(x in f["name"] for x in ("درآمد ثابت", "درآمد ثا"))}
other = {ic: f for ic, f in found.items() if ic not in fi}

log(f"  ── Fixed-income (درآمد ثابت): {len(fi)} ──")
for ic, f in sorted(fi.items(), key=lambda x: x[1]["symbol"]):
    log(f"    {f['symbol']:12}  {ic:20}  {f['name'][:50]}")

log()
log(f"  ── Other صندوق ETFs (not fixed-income): {len(other)} ──")
for ic, f in sorted(other.items(), key=lambda x: x[1]["symbol"]):
    log(f"    {f['symbol']:12}  {ic:20}  {f['name'][:50]}")

# ════════════════════════════════════════════════════════════════════════════
# DONE
# ════════════════════════════════════════════════════════════════════════════
sep("DONE")
report = "\n".join(lines)
OUT.write_text(report, encoding="utf-8")
log(f"Report saved: {OUT}")
