#!/usr/bin/env python3
"""
Local page-structure analyzer for TSETMC.

Run this on a machine with access to Iran internet:
    python tools/analyze_tsetmc_page.py

It fetches several TSETMC endpoints for کمند (insCode 34718633636164421)
and writes a detailed structure report to:
  - stdout (human-readable summary)
  - tools/tsetmc_analysis_report.txt  (full dump, safe to commit/paste)
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

INS_CODE    = "34718633636164421"   # کمند — confirmed by user
CDN_BASE    = "https://cdn.tsetmc.com/api"
MAIN_BASE   = "https://www.tsetmc.com"
OUT_FILE    = Path(__file__).parent / "tsetmc_analysis_report.txt"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Referer":         "https://www.tsetmc.com/",
}

session = requests.Session()
session.headers.update(HEADERS)

lines = []   # will be joined → report file

def log(msg=""):
    print(msg)
    lines.append(msg)

def sep(title=""):
    bar = "─" * 70
    log()
    log(bar)
    if title:
        log(f"  {title}")
        log(bar)

def fetch(url, json_mode=False, silent=False):
    try:
        r = session.get(url, timeout=20)
        log(f"  GET {url}")
        log(f"  → HTTP {r.status_code}  size={len(r.content)} bytes  "
            f"Content-Type={r.headers.get('Content-Type','?')[:60]}")
        if json_mode:
            try:
                return r, r.json()
            except Exception:
                return r, None
        return r, r.text
    except Exception as e:
        if not silent:
            log(f"  ERROR: {e}")
        return None, None


# ── 1. CDN ClosingPriceHistory ──────────────────────────────────────────────
sep("1. CDN ClosingPrice/GetClosingPriceHistory")
url = f"{CDN_BASE}/ClosingPrice/GetClosingPriceHistory/{INS_CODE}/0"
_, data = fetch(url, json_mode=True)
if data:
    history = (data.get("closingPriceHistory") or
               data.get("ClosingPriceHistory") or
               data.get("history") or [])
    log(f"  Top-level keys: {list(data.keys())}")
    log(f"  History entries: {len(history)}")
    if history:
        log(f"  ALL keys in one history entry: {sorted(history[0].keys())}")
        log()
        log("  Last 3 entries (most recent first by dEven):")
        sorted_h = sorted(history, key=lambda r: r.get("dEven", 0), reverse=True)
        for entry in sorted_h[:3]:
            log(f"    dEven={entry.get('dEven')}  "
                f"pClosing={entry.get('pClosing')}  "
                f"navStat={entry.get('navStat')}  "
                f"ALL={json.dumps({k:v for k,v in entry.items() if v not in (0,None,'')}, ensure_ascii=False)}")
else:
    log("  No JSON data returned")


# ── 2. CDN ClosingPriceInfo ─────────────────────────────────────────────────
sep("2. CDN ClosingPrice/GetClosingPriceInfo")
url = f"{CDN_BASE}/ClosingPrice/GetClosingPriceInfo/{INS_CODE}"
_, data = fetch(url, json_mode=True)
if data:
    log(f"  Top-level keys: {list(data.keys())}")
    info = data.get("closingPriceInfo", {})
    if info:
        log(f"  closingPriceInfo keys: {sorted(info.keys())}")
        nonzero = {k: v for k, v in info.items() if v not in (0, None, "", [])}
        log(f"  Non-zero fields:\n    {json.dumps(nonzero, ensure_ascii=False, indent=4)}")


# ── 3. CDN InstrumentInfo ───────────────────────────────────────────────────
sep("3. CDN Instrument/GetInstrumentInfo")
url = f"{CDN_BASE}/Instrument/GetInstrumentInfo/{INS_CODE}"
_, data = fetch(url, json_mode=True)
if data:
    log(f"  Top-level keys: {list(data.keys())}")
    info = data.get("instrumentInfo", {})
    if info:
        nonzero = {k: v for k, v in info.items() if v not in (0, None, "", [])}
        log(f"  Non-zero fields:\n    {json.dumps(nonzero, ensure_ascii=False, indent=4)}")


# ── 4. CDN ETF endpoints ────────────────────────────────────────────────────
sep("4. CDN ETF endpoints")
for path in ("ETF/ETFByInsCode", "ETF/GetETFByInsCode", "ETF/GetETFInfo",
             "ETF/ETFList"):
    url = f"{CDN_BASE}/{path}" + (f"/{INS_CODE}" if "List" not in path else "")
    _, data = fetch(url, json_mode=True, silent=True)
    if data:
        log(f"  [{path}] top-level keys: {list(data.keys())[:10]}")
        # Find any non-zero nav-like field
        raw = json.dumps(data, ensure_ascii=False)
        for kw in ("navStat", "cancelNav", "issueNav", "statisticalNav", "nav"):
            idx = raw.lower().find(f'"{kw.lower()}"')
            if idx >= 0:
                snippet = raw[max(0, idx-5):idx+60]
                log(f"    Found '{kw}' near: {snippet}")
                break
    else:
        log(f"  [{path}] → no data")


# ── 5. TSETMC main History page ─────────────────────────────────────────────
sep("5. www.tsetmc.com/History/{insCode}/{YYYYMMDD}")

today = datetime.now()
for days_back in range(0, 7):
    d = today - timedelta(days=days_back)
    if d.weekday() in (4, 5):   # Fri/Sat = Iranian weekend
        continue
    date_str = d.strftime("%Y%m%d")
    url = f"{MAIN_BASE}/History/{INS_CODE}/{date_str}"
    r, html = fetch(url)
    if not r:
        continue

    size = len(r.content)
    log(f"  Size: {size} bytes")

    if size <= 900:
        log("  ⚠ Looks like SPA shell — skipping")
        continue

    # Is it JSON?
    stripped = (html or "").strip()
    if stripped and stripped[0] in ('{', '['):
        log("  ✓ Response starts with JSON character!")
        try:
            obj = json.loads(stripped)
            log(f"  JSON top-level keys: {list(obj.keys()) if isinstance(obj, dict) else 'list'}")
            raw = json.dumps(obj, ensure_ascii=False)
            for kw in ("navStat", "cancelNav", "issueNav", "statisticalNav",
                       "nav", "pClosing", "dEven"):
                idx = raw.lower().find(f'"{kw.lower()}"')
                if idx >= 0:
                    snippet = raw[max(0, idx-2):idx+80]
                    log(f"  Field '{kw}': ...{snippet}...")
            log(f"\n  FULL JSON (first 3000 chars):\n{raw[:3000]}")
        except Exception as e:
            log(f"  JSON parse error: {e}")
            log(f"  First 500 chars: {stripped[:500]}")
    else:
        log("  Response is HTML")
        soup = BeautifulSoup(html, "html.parser")

        # Script tags
        scripts = [s.string or "" for s in soup.find_all("script") if s.string]
        log(f"  <script> tags with content: {len(scripts)}")
        for i, sc in enumerate(scripts[:5]):
            log(f"  Script[{i}] (first 200): {sc[:200].replace(chr(10),' ')}")

        # Tables
        tables = soup.find_all("table")
        log(f"  <table> elements: {len(tables)}")
        for ti, table in enumerate(tables[:3]):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            first_row = [td.get_text(strip=True) for td in
                         (table.find("tr") or {}).find_all("td")]  # type: ignore
            log(f"  Table[{ti}] headers: {headers}")
            log(f"  Table[{ti}] first data row: {first_row[:8]}")

        # __NEXT_DATA__
        nd_tag = soup.find("script", id="__NEXT_DATA__")
        if nd_tag:
            log("  Found __NEXT_DATA__ script tag!")
            try:
                nd = json.loads(nd_tag.string or "{}")
                log(f"  __NEXT_DATA__ keys: {list(nd.keys())}")
                raw_nd = json.dumps(nd, ensure_ascii=False)
                log(f"  __NEXT_DATA__ (first 2000): {raw_nd[:2000]}")
            except Exception as e:
                log(f"  __NEXT_DATA__ parse error: {e}")

        # NAV-like patterns in scripts
        full_script = " ".join(scripts)
        for pattern, label in [
            (r'navStat["\']?\s*[:=]\s*["\']?(\d[\d,]*)', "navStat"),
            (r'cancelNav["\']?\s*[:=]\s*["\']?(\d[\d,]*)', "cancelNav"),
            (r'قیمت\s*ابطال[^:]{0,10}[:=]\s*["\']?(\d[\d,]*)', "قیمت ابطال"),
        ]:
            m = re.search(pattern, full_script, re.IGNORECASE)
            if m:
                log(f"  Found {label} = {m.group(1)} in scripts")

        # Save full HTML for inspection
        html_file = Path(__file__).parent / f"tsetmc_history_{date_str}.html"
        html_file.write_text(html or "", encoding="utf-8")
        log(f"  Full HTML saved → {html_file}")

    log("  (stopping after first real page)")
    break


# ── 6. TSETMC main /instrument page (for comparison) ───────────────────────
sep("6. www.tsetmc.com/instrument/{insCode}  [comparison — expect SPA shell]")
url = f"{MAIN_BASE}/instrument/{INS_CODE}"
r, html = fetch(url)
if r:
    log(f"  Size: {len(r.content)} bytes")
    if len(r.content) <= 900:
        log("  ✓ Confirmed SPA shell (too small for real data)")
    else:
        log("  Unexpectedly large — may have data")


# ── 7. Save report ──────────────────────────────────────────────────────────
sep("DONE")
report = "\n".join(lines)
OUT_FILE.write_text(report, encoding="utf-8")
log(f"Full report saved to: {OUT_FILE}")
log()
log("Share the file tools/tsetmc_analysis_report.txt (or paste its contents)")
log("so the scraper can be written to match the actual page structure.")
