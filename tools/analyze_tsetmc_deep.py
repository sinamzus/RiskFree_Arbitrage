#!/usr/bin/env python3
"""
Deep TSETMC / tsetmc.ir structure analyzer.

Run on a machine WITH Iran internet access:
    python tools/analyze_tsetmc_deep.py

What it does
------------
1. Discovers all fixed-income ETF symbols from tsetmc.ir (or cdn.tsetmc.com)
2. For كمند (insCode 34718633636164421) probes every known API endpoint that
   might carry NAV or intraday data
3. Saves raw JSON responses for analysis
4. Writes a human-readable report to tools/tsetmc_deep_report.txt

Output files in tools/
    tsetmc_deep_report.txt     — summary of every probe
    raw/                       — raw JSON for every successful response
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── constants ──────────────────────────────────────────────────────────────────
KNOWN_INS_CODE = "34718633636164421"   # كمند — confirmed fixed-income ETF
CDN  = "https://cdn.tsetmc.com/api"
MAIN = "https://www.tsetmc.com"
IR   = "https://tsetmc.ir"            # alternative domain the user mentioned
OUT  = Path(__file__).parent / "tsetmc_deep_report.txt"
RAW  = Path(__file__).parent / "raw"
RAW.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/html, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Referer":         "https://www.tsetmc.com/",
}

session = requests.Session()
session.headers.update(HEADERS)

lines = []
def log(msg=""):
    print(msg)
    lines.append(str(msg))

def sep(title=""):
    log()
    log("─" * 72)
    if title:
        log(f"  {title}")
        log("─" * 72)

def save_raw(name: str, content):
    p = RAW / f"{name}.json"
    if isinstance(content, (dict, list)):
        p.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        p = RAW / f"{name}.txt"
        p.write_text(str(content), encoding="utf-8")
    return p

def get(url, label="", silent=False, is_json=True, timeout=20):
    try:
        r = session.get(url, timeout=timeout)
        size = len(r.content)
        ct   = r.headers.get("Content-Type", "")
        log(f"  GET {url}")
        log(f"  → {r.status_code}  {size} bytes  {ct[:60]}")
        if r.status_code >= 400:
            return None, None
        if is_json or "json" in ct:
            try:
                data = r.json()
                if label:
                    save_raw(label, data)
                return r, data
            except Exception:
                pass
        if label:
            save_raw(label, r.text)
        return r, r.text
    except Exception as e:
        if not silent:
            log(f"  ERROR: {e}")
        return None, None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Fixed-income ETF list discovery
# ══════════════════════════════════════════════════════════════════════════════
sep("SECTION 1: Fixed-income ETF fund list discovery")

# 1a. tsetmc.ir homepage — check if it's a different site
log("\n── 1a. tsetmc.ir homepage ──")
r, html = get(f"{IR}/", label="tsetmc_ir_home", is_json=False)
if html and isinstance(html, str):
    log(f"  tsetmc.ir title: {html[html.find('<title'):html.find('</title>')+8] if '<title' in html else '(no title tag)'}")
    log(f"  First 300 chars: {html[:300].replace(chr(10),' ')}")

# 1b. tsetmc.ir API endpoints for fund list
log("\n── 1b. tsetmc.ir API endpoint guesses ──")
ir_fund_urls = [
    f"{IR}/api/v1/fund/fixedincome",
    f"{IR}/api/fund/list",
    f"{IR}/api/v1/etf/list",
    f"{IR}/api/instruments/fixed-income",
    f"{IR}/api/Market/FixedIncome",
    f"{IR}/Loader.aspx?ParTree=15131P&t=etf",
    f"{IR}/Loader.aspx?ParTree=151318",
    f"{IR}/tsev2/data/instinfofast.aspx?d=i&g=5&t=1",  # type=5 = ETF?
]
for url in ir_fund_urls:
    r2, data = get(url, label=f"ir_fundlist_{len(lines)}", silent=True)
    if r2 and r2.status_code == 200 and len(r2.content) > 100:
        log(f"  ✓ {url}  → {len(r2.content)} bytes, preview: {str(data)[:200]}")
    else:
        log(f"  ✗ {url}")

# 1c. CDN instrument search for "صندوق درآمد ثابت" — bulk approach
log("\n── 1c. CDN search for درآمد ثابت ETFs ──")
fi_funds = []
for kw in ["صندوق درآمد ثابت", "درآمد ثابت", "صندوق ثابت"]:
    from urllib.parse import quote
    r3, data = get(f"{CDN}/Instrument/GetInstrumentSearch/{quote(kw)}",
                   label=f"search_{kw[:10]}", silent=True)
    if data and isinstance(data, dict):
        results = data.get("instrumentSearch", [])
        etfs = [x for x in results
                if "صندوق" in x.get("lVal30","") and
                   x.get("cgrValCotTitle","").find("صندوق") >= 0]
        log(f"  '{kw}' → {len(results)} total, {len(etfs)} ETF funds")
        for f in etfs[:5]:
            log(f"    {f.get('lVal18AFC',''):12}  {f.get('insCode',''):20}  {f.get('lVal30','')[:40]}")
        fi_funds.extend(etfs)

# 1d. CDN MarketWatch / category endpoints
log("\n── 1d. CDN category/market overview endpoints ──")
cat_urls = [
    f"{CDN}/MarketData/GetMarketOverview/1",
    f"{CDN}/MarketData/GetMarketOverview/2",
    f"{CDN}/Instrument/GetInstrumentGroupByMarket/H1",   # H1 = ETF market
    f"{CDN}/Instrument/GetInstrumentGroupByMarket/H2",
    f"{CDN}/Instrument/GetMarketGroupList",
    f"{CDN}/ClosingPrice/GetMarketClose/H1/1",
    f"{CDN}/ClosingPrice/GetMarketClose/H1/0",
    f"{CDN}/ClosingPrice/GetIndexB2/H1",
]
for url in cat_urls:
    r4, data = get(url, label=f"cat_{url.split('/')[-1]}", silent=True)
    if r4 and r4.status_code == 200 and data and len(str(data)) > 50:
        preview = str(data)[:200].replace("\n"," ")
        log(f"  ✓ {url.split('/')[-2]}/{url.split('/')[-1]} → {preview}")
    else:
        log(f"  ✗ {url.split('cdn.tsetmc.com/api')[1]}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — History tab (سابقه) data for كمند
# ══════════════════════════════════════════════════════════════════════════════
sep(f"SECTION 2: History / سابقه tab data — كمند ({KNOWN_INS_CODE})")

# 2a. CDN history with different type params
log("\n── 2a. CDN ClosingPriceHistory with type variants ──")
for t in range(0, 4):
    r5, data = get(f"{CDN}/ClosingPrice/GetClosingPriceHistory/{KNOWN_INS_CODE}/{t}",
                   label=f"history_type{t}", silent=True)
    if data and isinstance(data, dict):
        hist = data.get("closingPriceHistory") or []
        log(f"  type={t}: {len(hist)} entries, "
            f"keys={list(hist[0].keys()) if hist else '[]'}")
        if hist:
            entry = hist[-1]
            log(f"    Latest entry: {json.dumps({k:v for k,v in entry.items() if v not in (0,None,'')}, ensure_ascii=False)}")
    else:
        log(f"  type={t}: no data")

# 2b. tsetmc.ir history endpoints
log("\n── 2b. tsetmc.ir history endpoints ──")
today = datetime.now()
recent_dates = []
for d in range(0, 7):
    dd = today - timedelta(days=d)
    if dd.weekday() not in (4, 5):  # skip Fri/Sat
        recent_dates.append(dd.strftime("%Y%m%d"))
    if len(recent_dates) >= 3:
        break

for date_str in recent_dates:
    for base in (MAIN, IR):
        url = f"{base}/History/{KNOWN_INS_CODE}/{date_str}"
        r6, raw = get(url, label=f"history_page_{base.split('//')[1].split('.')[0]}_{date_str}",
                      is_json=False, silent=True)
        size = len(r6.content) if r6 else 0
        if size > 900:
            log(f"  ✓ {url} → {size} bytes (has real data!)")
            # Is it JSON?
            s = (raw or "").strip()
            if s and s[0] in ('{', '['):
                try:
                    obj = json.loads(s)
                    log(f"    → JSON! keys={list(obj.keys()) if isinstance(obj,dict) else 'list'}")
                    log(f"    → {json.dumps(obj, ensure_ascii=False)[:400]}")
                except Exception:
                    pass
            else:
                log(f"    → HTML, {s[:200].replace(chr(10),' ')}")
        else:
            log(f"  ✗ {url} → {size} bytes (SPA shell)")

# 2c. CDN-side history alternatives
log("\n── 2c. CDN alternative history endpoints ──")
hist_alt_urls = [
    f"{CDN}/ClosingPrice/GetCSAdjHistory/{KNOWN_INS_CODE}/0",
    f"{CDN}/ClosingPrice/GetCSAdjHistory/{KNOWN_INS_CODE}/1",
    f"{CDN}/ClosingPrice/GetMFHistory/{KNOWN_INS_CODE}/0",
    f"{CDN}/ClosingPrice/GetETFHistory/{KNOWN_INS_CODE}/0",
    f"{CDN}/ClosingPrice/GetNavHistory/{KNOWN_INS_CODE}/0",
    f"{CDN}/Nav/GetNavHistory/{KNOWN_INS_CODE}/0",
    f"{CDN}/Nav/GetNav/{KNOWN_INS_CODE}",
    f"{CDN}/ETF/GetETFByDate/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/ETF/GetETFHistory/{KNOWN_INS_CODE}",
    f"{CDN}/ETF/GetETFNav/{KNOWN_INS_CODE}",
]
for url in hist_alt_urls:
    r7, data = get(url, label=f"hist_alt_{url.split('/')[-2]}_{url.split('/')[-1][:8]}",
                   silent=True)
    if r7 and r7.status_code == 200 and len(r7.content) > 50:
        size = len(r7.content)
        preview = str(data)[:300].replace("\n","") if data else "(binary)"
        if size != 824:  # 824 = SPA shell → not useful
            log(f"  ✓ {url.split('/api/')[1]} → {size}b: {preview}")
        else:
            log(f"  ✗ {url.split('/api/')[1]} → SPA shell")
    else:
        log(f"  ✗ {url.split('/api/')[1] if '/api/' in url else url}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Intraday (درون‌روز) data
# ══════════════════════════════════════════════════════════════════════════════
sep(f"SECTION 3: Intraday / درون‌روز data — كمند ({KNOWN_INS_CODE})")

# 3a. CDN trade endpoints
log("\n── 3a. CDN trade history endpoints ──")
trade_urls = [
    f"{CDN}/Trade/GetTradeHistory/{KNOWN_INS_CODE}/0",
    f"{CDN}/Trade/GetTradeHistory/{KNOWN_INS_CODE}/{recent_dates[0]}/true",
    f"{CDN}/Trade/GetTradeHistory/{KNOWN_INS_CODE}/{recent_dates[0]}/false",
    f"{CDN}/Trade/GetTrade/{KNOWN_INS_CODE}",
    f"{CDN}/Trade/GetTradeList/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/ClosingPrice/GetTradeHistory/{KNOWN_INS_CODE}/0",
]
for url in trade_urls:
    r8, data = get(url, label=f"trade_{url.split('/')[-2]}_{url.split('/')[-1][:8]}",
                   silent=True)
    if r8 and r8.status_code == 200 and len(r8.content) > 50 and len(r8.content) != 824:
        preview = str(data)[:400].replace("\n","")
        log(f"  ✓ {url.split('/api/')[1]} → {len(r8.content)}b: {preview}")
    else:
        code = r8.status_code if r8 else "ERR"
        log(f"  ✗ {url.split('/api/')[1] if '/api/' in url else url} ({code})")

# 3b. Old TSETMC Loader.aspx endpoints for trade data
log("\n── 3b. Old Loader.aspx trade endpoints ──")
loader_urls = [
    f"{MAIN}/Loader.aspx?ParTree=15131W&i={KNOWN_INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=15131L&i={KNOWN_INS_CODE}",  # might be NAV
    f"{MAIN}/Loader.aspx?ParTree=15131V&i={KNOWN_INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=1513115&i={KNOWN_INS_CODE}",
    f"{MAIN}/tsev2/data/instinfofast.aspx?i={KNOWN_INS_CODE}&c=",
    f"{MAIN}/tsev2/data/TradeDetail.aspx?i={KNOWN_INS_CODE}&d={recent_dates[0]}",
    f"{MAIN}/tsev2/data/TradeDetail.aspx?i={KNOWN_INS_CODE}&d=0",
    f"{MAIN}/tsev2/data/TsePublicTrade.aspx?i={KNOWN_INS_CODE}",
    f"{MAIN}/tsev2/data/TsePublicTrade.aspx?i={KNOWN_INS_CODE}&d={recent_dates[0]}",
]
for url in loader_urls:
    r9, raw = get(url, label=f"loader_{url.split('?')[1][:20].replace('&','_')}",
                  is_json=False, silent=True)
    if r9 and r9.status_code == 200 and raw and len(r9.content) > 50 and len(r9.content) != 824:
        log(f"  ✓ {url.split('tsetmc.com')[1][:60]}  → {len(r9.content)}b")
        log(f"    Preview: {str(raw)[:300].replace(chr(10),' ')}")
    else:
        code = r9.status_code if r9 else "ERR"
        log(f"  ✗ {url.split('tsetmc.com')[1][:60]} ({code})")

# 3c. tsetmc.ir intraday endpoints
log("\n── 3c. tsetmc.ir intraday endpoints ──")
ir_trade_urls = [
    f"{IR}/api/Trade/GetTradeHistory/{KNOWN_INS_CODE}/0",
    f"{IR}/api/Trade/GetTradeHistory/{KNOWN_INS_CODE}/{recent_dates[0]}/true",
    f"{IR}/tsev2/data/TradeDetail.aspx?i={KNOWN_INS_CODE}&d={recent_dates[0]}",
    f"{IR}/tsev2/data/TradeDetail.aspx?i={KNOWN_INS_CODE}&d=0",
    f"{IR}/Loader.aspx?ParTree=15131W&i={KNOWN_INS_CODE}",
    f"{IR}/Loader.aspx?ParTree=15131L&i={KNOWN_INS_CODE}",
]
for url in ir_trade_urls:
    r10, raw = get(url, label=f"ir_trade_{url.split('/')[-2][:10]}",
                   is_json=False, silent=True)
    if r10 and r10.status_code == 200 and raw and len(r10.content) > 50 and len(r10.content) != 824:
        log(f"  ✓ {url}  → {len(r10.content)}b")
        log(f"    Preview: {str(raw)[:300].replace(chr(10),' ')}")
    else:
        code = r10.status_code if r10 else "ERR"
        log(f"  ✗ {url} ({code})")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Inspect a specific date's closing data in detail
# ══════════════════════════════════════════════════════════════════════════════
sep(f"SECTION 4: Detailed date probe — {recent_dates[0]}")

# 4a. ClosingPriceInfo for specific date
log("\n── 4a. CDN endpoints that accept a date param ──")
date_urls = [
    f"{CDN}/ClosingPrice/GetClosingPriceInfo/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/ClosingPrice/GetClosingPriceDaily/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/ClosingPrice/GetClosingPrice/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/Instrument/GetInstrumentInfoByDate/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/Nav/GetNavByDate/{KNOWN_INS_CODE}/{recent_dates[0]}",
    f"{CDN}/ClosingPrice/GetLastClosingPrice/{KNOWN_INS_CODE}/{recent_dates[0]}",
]
for url in date_urls:
    r11, data = get(url, label=f"date_{url.split('/')[-1]}", silent=True)
    if r11 and r11.status_code == 200 and len(r11.content) != 824:
        log(f"  ✓ {url.split('/api/')[1]} → {len(r11.content)}b: {str(data)[:200]}")
    else:
        code = r11.status_code if r11 else "ERR"
        log(f"  ✗ {url.split('/api/')[1] if '/api/' in url else url} ({code})")

# 4b. thirtyDayClosingHistory — was in ClosingPriceInfo response but empty
log("\n── 4b. thirtyDayClosingHistory variants ──")
thirty_urls = [
    f"{CDN}/ClosingPrice/GetThirtyDayHistory/{KNOWN_INS_CODE}",
    f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{KNOWN_INS_CODE}/30",
    f"{CDN}/ClosingPrice/GetThirtyDayClosingHistory/{KNOWN_INS_CODE}",
]
for url in thirty_urls:
    r12, data = get(url, label=f"thirty_{url.split('/')[-1][:15]}", silent=True)
    if r12 and r12.status_code == 200 and len(r12.content) not in (0, 824):
        log(f"  ✓ {url.split('/api/')[1]} → {len(r12.content)}b: {str(data)[:200]}")
    else:
        log(f"  ✗ {url.split('/api/')[1] if '/api/' in url else url}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Full tsetmc.ir probe (if it's a different/data site)
# ══════════════════════════════════════════════════════════════════════════════
sep("SECTION 5: tsetmc.ir deep probe")

ir_probe_urls = [
    f"{IR}/",
    f"{IR}/instrument/{KNOWN_INS_CODE}",
    f"{IR}/Symbol/{KNOWN_INS_CODE}",
    f"{IR}/Symbol/كمند",
    f"{IR}/api/v1/instrument/{KNOWN_INS_CODE}",
    f"{IR}/api/v1/nav/{KNOWN_INS_CODE}",
    f"{IR}/api/v1/ohlcv/{KNOWN_INS_CODE}",
    f"{IR}/api/v1/funds/fixed-income",
    f"{IR}/api/v1/fund/list?type=fixed",
    f"{IR}/api/v1/fund/etf",
    f"{IR}/api/Symbol/GetInstrument/{KNOWN_INS_CODE}",
    f"{IR}/api/Nav/GetNav/{KNOWN_INS_CODE}",
    f"{IR}/Home/ISClient/3/0",
    f"{IR}/Home/ISClient/3/{KNOWN_INS_CODE}",
]
for url in ir_probe_urls:
    r13, raw = get(url, label=f"ir5_{url.replace(IR,'').replace('/','_')[:20]}",
                   is_json=False, silent=True)
    size = len(r13.content) if r13 else 0
    code = r13.status_code if r13 else "ERR"
    if size > 100 and size != 824 and code == 200:
        s = str(raw or "")
        log(f"  ✓ {url}  → {size}b")
        log(f"    {s[:300].replace(chr(10),' ')}")
    else:
        log(f"  ✗ {url}  ({code}, {size}b)")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Capture full ClosingPriceInfo with ALL fields logged
# ══════════════════════════════════════════════════════════════════════════════
sep("SECTION 6: Complete ClosingPriceInfo — ALL fields (no filter)")

r14, data = get(f"{CDN}/ClosingPrice/GetClosingPriceInfo/{KNOWN_INS_CODE}",
                label="closing_all_fields")
if data and isinstance(data, dict):
    info = data.get("closingPriceInfo", {})
    log(f"  ALL {len(info)} fields:")
    for k, v in sorted(info.items()):
        log(f"    {k:30} = {v}")


# ══════════════════════════════════════════════════════════════════════════════
# DONE — save report
# ══════════════════════════════════════════════════════════════════════════════
sep("DONE")
report = "\n".join(lines)
OUT.write_text(report, encoding="utf-8")
log(f"Report saved: {OUT}")
log(f"Raw files:    {RAW}/")
log()
log("Push tools/tsetmc_deep_report.txt and tools/raw/*.json back to the repo.")
