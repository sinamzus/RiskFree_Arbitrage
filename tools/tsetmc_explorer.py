#!/usr/bin/env python3
"""
TSETMC Comprehensive API Explorer
===================================
تمام endpoint های شناخته‌شده و احتمالی TSETMC را آزمایش می‌کند
و ساختار کامل پاسخ‌ها را ضبط می‌کند.

خروجی:
  tsetmc_full_analysis.txt   ← گزارش خواندنی
  tsetmc_full_analysis.json  ← داده خام برای آنالیز دقیق‌تر

اجرا:
  python tools/tsetmc_explorer.py
  python tools/tsetmc_explorer.py --ins-code 53251602435454519 --symbol کیان
"""

import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

# ── UTF-8 on Windows ─────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from config import TSETMC_CDN, REQUEST_HEADERS, REQUEST_TIMEOUT

CDN  = TSETMC_CDN           # https://cdn.tsetmc.com/api
MAIN = "https://www.tsetmc.com"

# ── HTTP Session ──────────────────────────────────────────────────────────────
sess = requests.Session()
sess.headers.update(REQUEST_HEADERS)
sess.headers["Referer"] = "https://www.tsetmc.com/"

def get(url: str, params: dict = None) -> tuple[int, Any, float]:
    """Return (http_status, parsed_json_or_None, elapsed_ms)."""
    t0 = time.time()
    try:
        r = sess.get(url, params=params, timeout=REQUEST_TIMEOUT)
        ms = (time.time() - t0) * 1000
        try:
            return r.status_code, r.json(), ms
        except Exception:
            return r.status_code, r.text[:500] if r.text else None, ms
    except Exception as e:
        ms = (time.time() - t0) * 1000
        return 0, str(e)[:200], ms

def describe(val: Any, depth: int = 0) -> str:
    """Recursively describe the shape of a JSON value."""
    pad = "  " * depth
    if val is None:
        return "null"
    if isinstance(val, bool):
        return f"bool  = {val}"
    if isinstance(val, (int, float)):
        return f"number  = {val}"
    if isinstance(val, str):
        return f"string  = {repr(val[:80])}"
    if isinstance(val, list):
        if not val:
            return "list[]  (empty)"
        item0 = val[0]
        if isinstance(item0, dict):
            keys = list(item0.keys())
            return (f"list[{len(val)}]  item_keys={keys}\n"
                    + f"{pad}  sample[0]: {json.dumps(item0, ensure_ascii=False)[:300]}")
        return f"list[{len(val)}]  = {repr(val[:3])[:120]}"
    if isinstance(val, dict):
        lines = [f"dict({len(val)} keys)"]
        for k, v in val.items():
            lines.append(f"{pad}  {k!r:30s} → {describe(v, depth+2)}")
        return "\n".join(lines)
    return repr(val)[:120]

# ── Output capture ─────────────────────────────────────────────────────────────
_lines: list[str] = []
_data:  dict = {}   # raw results for JSON dump

def p(s=""):
    print(s)
    _lines.append(s)

def section(title: str):
    bar = "═" * 76
    p(f"\n{bar}")
    p(f"  {title}")
    p(bar)

def sub(title: str):
    p(f"\n  ── {title} ──")

def probe(label: str, url: str, params: dict = None, delay: float = 0.3) -> tuple[bool, Any]:
    status, data, ms = get(url, params)
    key = label
    _data[key] = {"url": url, "params": params, "status": status,
                  "ms": round(ms), "data": data}

    ok_flag = status == 200 and data and data != {} and data != []
    mark = "✓" if ok_flag else ("·" if status == 200 else "✗")

    content_hint = ""
    if ok_flag and isinstance(data, dict):
        top_keys = list(data.keys())
        content_hint = f"  keys={top_keys}"
        # For the first list-valued key, show count
        for k, v in data.items():
            if isinstance(v, list):
                content_hint += f"  [{k}]={len(v)} rows"
                break
            elif isinstance(v, dict):
                content_hint += f"  [{k}]=dict({len(v)})"
                break

    p(f"  {mark}  [{status:3d}] {ms:5.0f}ms  {label}{content_hint}")

    if ok_flag and isinstance(data, dict):
        for k, v in data.items():
            p(f"         {k!r:32s} → {describe(v, 3)[:200]}")

    time.sleep(delay)
    return ok_flag, data


# ═════════════════════════════════════════════════════════════════════════════
def run(ins_code: str, symbol: str):

    today     = int(datetime.now().strftime("%Y%m%d"))
    yesterday = int((datetime.now() - timedelta(days=1)).strftime("%Y%m%d"))
    # Find a recent trading day from DB (fallback: 5 days ago)
    last_trade_date = today
    try:
        import sqlite3
        db = sqlite3.connect(ROOT / "data" / "arbitrage.db")
        row = db.execute(
            "SELECT MAX(date) FROM daily_history WHERE symbol=?", (symbol,)
        ).fetchone()
        if row and row[0]:
            last_trade_date = row[0]
        db.close()
    except Exception:
        pass

    p(f"TSETMC Comprehensive API Explorer")
    p(f"Symbol: {symbol}   insCode: {ins_code}")
    p(f"Today: {today}   Yesterday: {yesterday}   Last trade date in DB: {last_trade_date}")
    p(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ─────────────────────────────────────────────────────────────────────────
    section("A. ClosingPrice — قیمت و تاریخچه روزانه")
    # ─────────────────────────────────────────────────────────────────────────

    sub("A1. GetClosingPriceInfo (live OHLCV + embedded fields)")
    ok, d = probe("A1.GetClosingPriceInfo",
                  f"{CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}")
    if ok and d:
        cpi = d.get("closingPriceInfo") or {}
        p(f"         ALL fields in closingPriceInfo:")
        for k, v in cpi.items():
            p(f"           {k!r:30s} = {repr(v)[:80]}")
        # Nested objects
        for k in ["instrument", "instrumentState", "thirtyDayClosingHistory"]:
            if k in cpi:
                p(f"         >> nested '{k}':")
                nested = cpi[k]
                if isinstance(nested, dict):
                    for nk, nv in nested.items():
                        p(f"              {nk!r:28s} = {repr(nv)[:80]}")
                elif isinstance(nested, list):
                    p(f"              list[{len(nested)}] first: {nested[0] if nested else 'empty'}")

    sub("A2. GetClosingPriceDailyList (N-day OHLCV bars)")
    probe("A2.GetClosingPriceDailyList.30",
          f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/30")
    probe("A2.GetClosingPriceDailyList.365",
          f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/365")

    sub("A3. GetLastPriceHistory (date-range price history)")
    probe("A3.GetLastPriceHistory.today",
          f"{CDN}/ClosingPrice/GetLastPriceHistory/{ins_code}/{today}/{today}")
    probe("A3.GetLastPriceHistory.range",
          f"{CDN}/ClosingPrice/GetLastPriceHistory/{ins_code}/{last_trade_date}/{today}")

    sub("A4. GetClosingPriceHistory (by count)")
    probe("A4.GetClosingPriceHistory.30",
          f"{CDN}/ClosingPrice/GetClosingPriceHistory/{ins_code}/30")

    sub("A5. GetShareholderInfo (سهامداران عمده)")
    probe("A5.GetShareholderInfo",
          f"{CDN}/ClosingPrice/GetShareholderInfo/{ins_code}")
    probe("A5.GetShareholderInfo.with_date",
          f"{CDN}/ClosingPrice/GetShareholderInfo/{ins_code}/{last_trade_date}")

    sub("A6. GetIndexB1History / GetIndexB2History")
    probe("A6.GetIndexB1History.30",
          f"{CDN}/ClosingPrice/GetIndexB1History/30")
    probe("A6.GetIndexB2History.30",
          f"{CDN}/Index/GetIndexB2History/30")
    probe("A6.GetIndexHistory",
          f"{CDN}/Index/GetIndexB2History/30")

    sub("A7. GetDailyInfoHistory (اطلاعات روزانه جامع)")
    probe("A7.GetDailyInfoHistory",
          f"{CDN}/ClosingPrice/GetDailyInfoHistory/{ins_code}/{last_trade_date}/{today}")
    probe("A7.GetDailyInfoHistory.0",
          f"{CDN}/ClosingPrice/GetDailyInfoHistory/{ins_code}/0/0")

    # ─────────────────────────────────────────────────────────────────────────
    section("B. Trade — معاملات و ریز معاملات")
    # ─────────────────────────────────────────────────────────────────────────

    sub("B1. GetTradeHistory (ریز معاملات روزانه)")
    ok, d = probe("B1.GetTradeHistory.last_trade_false",
                  f"{CDN}/Trade/GetTradeHistory/{ins_code}/{last_trade_date}/false")
    if ok and d:
        trades = d.get("tradeHistory") or []
        p(f"         trades: {len(trades)}")
        if trades:
            p(f"         ALL trade fields: {list(trades[0].keys())}")
            for i, t in enumerate(trades[:3]):
                p(f"         [{i}] {json.dumps(t, ensure_ascii=False)}")
            p(f"         ... last trade: {json.dumps(trades[-1], ensure_ascii=False)}")

    probe("B1.GetTradeHistory.last_trade_true",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/{last_trade_date}/true")
    probe("B1.GetTradeHistory.today_false",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/{today}/false")

    sub("B2. GetClientTypeHistory (حقیقی vs حقوقی)")
    ok, d = probe("B2.GetClientTypeHistory.last_trade",
                  f"{CDN}/Trade/GetClientTypeHistory/{ins_code}/{last_trade_date}")
    if ok and isinstance(d, dict):
        ct = d.get("clientType") or d.get("clientTypeHistory") or d.get("clientTypes") or {}
        p(f"         full response keys: {list(d.keys())}")
        for k, v in d.items():
            p(f"           {k!r:30s} → {describe(v, 3)[:200]}")
    elif ok and d:
        p(f"         raw response: {str(d)[:300]}")

    probe("B2.GetClientTypeHistory.today",
          f"{CDN}/Trade/GetClientTypeHistory/{ins_code}/{today}")
    probe("B2.GetClientTypeHistory.0",
          f"{CDN}/Trade/GetClientTypeHistory/{ins_code}/0")

    sub("B3. GetBriefTradeHistory (خلاصه معاملات)")
    probe("B3.GetBriefTradeHistory",
          f"{CDN}/Trade/GetBriefTradeHistory/{ins_code}/{last_trade_date}")
    probe("B3.GetBriefTradeHistory.today",
          f"{CDN}/Trade/GetBriefTradeHistory/{ins_code}/{today}")

    sub("B4. GetFutureStateStats")
    probe("B4.GetFutureStateStats",
          f"{CDN}/Trade/GetFutureStateStats/{ins_code}/{last_trade_date}")

    sub("B5. Misc trade endpoints")
    probe("B5.GetTradeIntraday",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}")
    probe("B5.GetTrade",
          f"{CDN}/Trade/GetTrade/{ins_code}")
    probe("B5.GetStateHistory",
          f"{CDN}/Trade/GetStateHistory/{ins_code}/{last_trade_date}")

    # ─────────────────────────────────────────────────────────────────────────
    section("C. BestLimits — دفتر سفارش")
    # ─────────────────────────────────────────────────────────────────────────

    sub("C1. Live order book")
    ok, d = probe("C1.BestLimits.live",
                  f"{CDN}/BestLimits/{ins_code}")
    if ok and d:
        bl = d.get("bestLimits") or []
        if bl:
            p(f"         ALL bestLimits fields: {list(bl[0].keys())}")
            for i, level in enumerate(bl[:2]):
                p(f"         level {i+1}: {json.dumps(level, ensure_ascii=False)}")

    sub("C2. Historical OB delta stream")
    ok, d = probe("C2.BestLimits.history.last_trade",
                  f"{CDN}/BestLimits/{ins_code}/{last_trade_date}")
    if ok and d:
        rows = d.get("bestLimitsHistory") or []
        p(f"         delta rows: {len(rows)}")
        if rows:
            p(f"         ALL fields: {list(rows[0].keys())}")
            for r in rows[:3]:
                p(f"           {json.dumps(r, ensure_ascii=False)}")
            p(f"         last: {json.dumps(rows[-1], ensure_ascii=False)}")

    probe("C2.BestLimits.history.today",
          f"{CDN}/BestLimits/{ins_code}/{today}")
    probe("C2.BestLimits.history.yesterday",
          f"{CDN}/BestLimits/{ins_code}/{yesterday}")

    # ─────────────────────────────────────────────────────────────────────────
    section("D. Instrument — اطلاعات ابزار")
    # ─────────────────────────────────────────────────────────────────────────

    sub("D1. GetInstrumentInfo (اطلاعات پایه ابزار)")
    ok, d = probe("D1.GetInstrumentInfo",
                  f"{CDN}/Instrument/GetInstrumentInfo/{ins_code}")
    if ok and d:
        ii = d.get("instrumentInfo") or {}
        p(f"         ALL fields in instrumentInfo:")
        for k, v in ii.items():
            p(f"           {k!r:30s} = {repr(v)[:80]}")

    sub("D2. GetInstrumentStatistic (آمار ابزار)")
    ok, d = probe("D2.GetInstrumentStatistic",
                  f"{CDN}/Instrument/GetInstrumentStatistic/{ins_code}")
    if ok and isinstance(d, dict):
        for k, v in d.items():
            p(f"           {k!r:30s} → {describe(v, 3)[:200]}")
    elif ok and d:
        p(f"         raw response: {str(d)[:300]}")

    sub("D3. GetInstrumentSearch")
    probe("D3.GetInstrumentSearch.symbol",
          f"{CDN}/Instrument/GetInstrumentSearch/{symbol}")

    sub("D4. GetInstrumentOptionInfo")
    probe("D4.GetInstrumentOptionInfo",
          f"{CDN}/Instrument/GetInstrumentOptionInfo/{ins_code}")

    sub("D5. GetInstrumentHistory (تاریخچه تغییرات)")
    probe("D5.GetInstrumentHistory.30",
          f"{CDN}/Instrument/GetInstrumentHistory/{ins_code}/30")

    sub("D6. GetRelatedInstruments")
    probe("D6.GetRelatedInstruments",
          f"{CDN}/Instrument/GetRelatedInstruments/{ins_code}")

    sub("D7. GetCodal / GetAnnouncement")
    probe("D7.GetCodal",
          f"{CDN}/Instrument/GetCodal/{ins_code}")

    # ─────────────────────────────────────────────────────────────────────────
    section("E. MutualFund — صندوق‌های سرمایه‌گذاری")
    # ─────────────────────────────────────────────────────────────────────────

    sub("E1. GetMFNav (NAV صندوق)")
    ok, d = probe("E1.GetMFNav",
                  f"{CDN}/MutualFund/GetMFNav/{ins_code}")
    if ok and isinstance(d, dict):
        for k, v in d.items():
            p(f"           {k!r:30s} → {describe(v, 3)[:300]}")
    elif ok and d:
        p(f"         raw response: {str(d)[:300]}")

    sub("E2. GetMFNavHistory (تاریخچه NAV)")
    from_date = int((datetime.now() - timedelta(days=30)).strftime("%Y%m%d"))
    probe("E2.GetMFNavHistory",
          f"{CDN}/MutualFund/GetMFNavHistory/{ins_code}/{from_date}/{today}")
    probe("E2.GetMFNavHistory.broad",
          f"{CDN}/MutualFund/GetMFNavHistory/{ins_code}/0/{today}")

    sub("E3. GetMFNavList / GetETFList (لیست صندوق‌ها)")
    probe("E3.GetMFNavList",
          f"{CDN}/MutualFund/GetMFNavList")
    probe("E3.GetMFNavAll",
          f"{CDN}/MutualFund/GetMFNavAll")
    probe("E3.GetETFList",
          f"{CDN}/MutualFund/GetETFList")
    probe("E3.GetMutualFundList",
          f"{CDN}/MutualFund/GetMutualFundList")
    probe("E3.GetMFNavByType",
          f"{CDN}/MutualFund/GetMFNavByType/6")  # type 6 = fixed income ETF

    sub("E4. GetMFData / GetETFHistory (اطلاعات جامع صندوق)")
    probe("E4.GetMFData",
          f"{CDN}/MutualFund/GetMFData/{ins_code}")
    probe("E4.GetFundInfo",
          f"{CDN}/MutualFund/GetFundInfo/{ins_code}")
    probe("E4.GetETFHistory",
          f"{CDN}/MutualFund/GetETFHistory/{ins_code}")
    probe("E4.GetETFByInsCode",
          f"{CDN}/MutualFund/GetETFByInsCode/{ins_code}")
    probe("E4.GetETFInfo",
          f"{CDN}/MutualFund/GetETFInfo/{ins_code}")

    sub("E5. ClientType/GetClientTypeHistory (حقیقی/حقوقی تاریخچه)")
    ok, d = probe("E5.GetClientTypeHistory",
                  f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{last_trade_date}")
    if ok and isinstance(d, dict):
        p(f"         keys: {list(d.keys())}")
        for k, v in d.items():
            p(f"           {k!r:30s} → {describe(v, 3)[:300]}")
    elif ok and d:
        p(f"         raw response: {str(d)[:300]}")
    probe("E5.GetClientTypeHistory.today",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{today}")
    probe("E5.GetClientTypeHistory.all",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/0")

    # ─────────────────────────────────────────────────────────────────────────
    section("F. MarketData — اطلاعات کل بازار")
    # ─────────────────────────────────────────────────────────────────────────

    sub("F1. GetTseClientTypeAll (حقیقی/حقوقی کل بازار)")
    ok, d = probe("F1.GetTseClientTypeAll",
                  f"{CDN}/MarketData/GetTseClientTypeAll")
    if ok and isinstance(d, dict):
        for k, v in d.items():
            p(f"           {k!r:30s} → {describe(v, 3)[:300]}")
    elif ok and d:
        p(f"         raw response: {str(d)[:300]}")

    sub("F2. GetMarketOverview (نمای کلی بازار)")
    probe("F2.GetMarketOverview",
          f"{CDN}/MarketData/GetMarketOverview")
    probe("F2.GetMarketSummary",
          f"{CDN}/MarketData/GetMarketSummary")
    probe("F2.GetMarketState",
          f"{CDN}/MarketData/GetMarketState")

    sub("F3. GetMarketWatch (تابلو کل بازار)")
    # paperTypes: 1=سهام, 2=حق‌تقدم, 3=اوراق بدهی, 4=آتی, 5=اختیار, 6=صندوق ETF, 300=اوراق تامین مالی
    probe("F3.GetMarketWatch.ETF",
          f"{CDN}/MarketWatch/GetMarketWatch",
          params={"paperTypes": "[6]", "showTraded": "true", "withBestLimits": "true"})
    probe("F3.GetMarketWatch.allFund",
          f"{CDN}/MarketWatch/GetMarketWatch",
          params={"paperTypes": "[6,300]", "showTraded": "false", "withBestLimits": "false"})
    probe("F3.GetMarketWatch.full",
          f"{CDN}/MarketWatch/GetMarketWatch",
          params={"paperTypes": "[1,2,3,4,5,6,300]", "showTraded": "true", "withBestLimits": "false"})

    sub("F4. GetMarketActors (بازیگران بازار)")
    probe("F4.GetMarketActors",
          f"{CDN}/MarketData/GetMarketActors/{ins_code}")

    sub("F5. GetSectorList (فهرست صنایع)")
    probe("F5.GetSectorList",
          f"{CDN}/Instrument/GetSectorList")
    probe("F5.GetSectorInfo",
          f"{CDN}/Instrument/GetSectorInfo/{ins_code}")

    # ─────────────────────────────────────────────────────────────────────────
    section("G. Index — شاخص‌ها")
    # ─────────────────────────────────────────────────────────────────────────

    sub("G1. GetIndexB2History")
    probe("G1.GetIndexB2History.30",
          f"{CDN}/Index/GetIndexB2History/30")
    probe("G1.GetIndexB2History.365",
          f"{CDN}/Index/GetIndexB2History/365")

    sub("G2. GetIndexHistory by index ID")
    # Common TSETMC index codes
    for idx_id, name in [
        ("32097828799138957", "شاخص کل"),
        ("67130298613737946", "شاخص هم‌وزن"),
        ("62752761908513005", "شاخص صنعت"),
    ]:
        probe(f"G2.GetIndexHistory.{name}",
              f"{CDN}/Index/GetIndexHistory/{idx_id}/30")

    sub("G3. GetIndexByInstrument (شاخص ابزار)")
    probe("G3.GetIndexByInstrument.30",
          f"{CDN}/Index/GetIndexByInstrument/{ins_code}/30")

    sub("G4. Index list")
    probe("G4.GetIndexList",
          f"{CDN}/Index/GetIndexList")
    probe("G4.GetIndexGroupList",
          f"{CDN}/Index/GetIndexGroupList")

    # ─────────────────────────────────────────────────────────────────────────
    section("H. Portfolio — ترکیب دارایی‌ها")
    # ─────────────────────────────────────────────────────────────────────────

    sub("H1. GetPortfolioByGroup (ترکیب دارایی صندوق)")
    probe("H1.GetPortfolioByGroup",
          f"{CDN}/Portfolio/GetPortfolioByGroup/{ins_code}/{last_trade_date}")
    probe("H1.GetPortfolioByGroup.today",
          f"{CDN}/Portfolio/GetPortfolioByGroup/{ins_code}/{today}")
    probe("H1.GetPortfolioByGroup.0",
          f"{CDN}/Portfolio/GetPortfolioByGroup/{ins_code}/0")

    sub("H2. GetPortfolioHistory (تاریخچه ترکیب)")
    probe("H2.GetPortfolioHistory",
          f"{CDN}/Portfolio/GetPortfolioHistory/{ins_code}/{last_trade_date}/{today}")
    probe("H2.GetPortfolioByGroupHistory",
          f"{CDN}/Portfolio/GetPortfolioByGroupHistory/{ins_code}/{last_trade_date}/{today}")

    # ─────────────────────────────────────────────────────────────────────────
    section("I. Capital / Dividend — سود و افزایش سرمایه")
    # ─────────────────────────────────────────────────────────────────────────

    probe("I1.GetCapitalIncreaseHistory",
          f"{CDN}/Capital/GetCapitalIncreaseHistory/{ins_code}")
    probe("I2.GetDividendHistory",
          f"{CDN}/Dividend/GetDividendHistory/{ins_code}")
    probe("I2.GetDividend",
          f"{CDN}/ClosingPrice/GetDividend/{ins_code}")
    probe("I3.GetMeetingHistory",
          f"{CDN}/Meeting/GetMeetingHistory/{ins_code}")

    # ─────────────────────────────────────────────────────────────────────────
    section("J. News / Announcement — اخبار و اطلاعیه‌ها")
    # ─────────────────────────────────────────────────────────────────────────

    probe("J1.GetNews",
          f"{CDN}/News/GetNews/{ins_code}")
    probe("J1.GetLatestNews",
          f"{CDN}/News/GetLatestNews")
    probe("J2.GetAnnouncement",
          f"{CDN}/Announcement/GetAnnouncement/{ins_code}")
    probe("J2.GetCodalAnnouncement",
          f"{CDN}/Codal/GetCodalAnnouncement/{ins_code}")
    probe("J3.GetEPS",
          f"{CDN}/ClosingPrice/GetEPS/{ins_code}")

    # ─────────────────────────────────────────────────────────────────────────
    section("K. Statistics — آمار تجمیعی")
    # ─────────────────────────────────────────────────────────────────────────

    probe("K1.GetInstrumentStats30",
          f"{CDN}/Statistics/GetInstrumentStats30/{ins_code}")
    probe("K1.GetInstrumentStatistic",
          f"{CDN}/Instrument/GetInstrumentStatistic/{ins_code}")
    probe("K2.GetGroupStats",
          f"{CDN}/Statistics/GetGroupStats")
    probe("K3.GetMarketStats",
          f"{CDN}/Statistics/GetMarketStats/{last_trade_date}")

    # ─────────────────────────────────────────────────────────────────────────
    section("L. Supervision — نظارت و محدودیت‌ها")
    # ─────────────────────────────────────────────────────────────────────────

    probe("L1.GetThreshold",
          f"{CDN}/ClosingPrice/GetThreshold/{ins_code}")
    probe("L1.GetStaticThreshold",
          f"{CDN}/Instrument/GetStaticThreshold/{ins_code}")
    probe("L2.GetSupervision",
          f"{CDN}/Supervision/GetSupervision/{ins_code}")
    probe("L3.GetInstrumentLock",
          f"{CDN}/Instrument/GetInstrumentLock/{ins_code}")

    # ─────────────────────────────────────────────────────────────────────────
    section("M. MAIN SITE — www.tsetmc.com (non-CDN endpoints)")
    # ─────────────────────────────────────────────────────────────────────────

    sub("M1. Symbol page (HTML — for scraping check)")
    status, data, ms = get(f"{MAIN}/Loader.aspx?ParTree=15131W&i={ins_code}")
    is_html = isinstance(data, str) and "<html" in data.lower()
    mark = "✓" if (status == 200 and is_html) else "·"
    p(f"  {mark}  [{status:3d}] {ms:5.0f}ms  M1.SymbolPage.HTML  len={len(data) if data else 0}")
    _data["M1.SymbolPage.HTML"] = {"url": f"{MAIN}/Loader.aspx?ParTree=15131W&i={ins_code}",
                                    "status": status, "ms": round(ms), "is_html": is_html}

    sub("M2. tsev2 legacy data API")
    probe("M2.tsev2.GetAjax",
          f"{MAIN}/tsev2/data/InstTradeHistory.aspx?i={ins_code}&Top=999999&A=1")
    probe("M2.tsev2.ClientType",
          f"{MAIN}/tsev2/data/clienttype.aspx?i={ins_code}")
    probe("M2.tsev2.BestLimit",
          f"{MAIN}/tsev2/data/BestLimit.aspx?i={ins_code}")
    probe("M2.tsev2.InstrumentInfo",
          f"{MAIN}/tsev2/data/Instrument.aspx?i={ins_code}&t=1")

    sub("M3. MAIN API (api.tsetmc.com or www.tsetmc.com/api)")
    probe("M3.MainAPI.ClosingInfo",
          f"https://api.tsetmc.com/api/ClosingPrice/GetClosingPriceInfo/{ins_code}")
    probe("M3.CDN.ClosingInfo",
          f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceInfo/{ins_code}")

    # ─────────────────────────────────────────────────────────────────────────
    section("N. ClosingPriceInfo — FULL FIELD DEEP DIVE")
    # ─────────────────────────────────────────────────────────────────────────
    # Already probed but let's capture ALL nested fields clearly

    p("  This section captures EVERY field from GetClosingPriceInfo for reference")
    p()
    status, data, ms = get(f"{CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}")
    if status == 200 and data:
        cpi_full = data.get("closingPriceInfo") or {}
        p(f"  closingPriceInfo — {len(cpi_full)} top-level fields:")
        for k, v in cpi_full.items():
            if isinstance(v, dict):
                p(f"    {k!r:30s} → DICT keys: {list(v.keys())}")
                for nk, nv in v.items():
                    p(f"        {nk!r:28s} = {repr(nv)[:80]}")
            elif isinstance(v, list):
                p(f"    {k!r:30s} → LIST[{len(v)}]" + (f" keys: {list(v[0].keys())}" if v and isinstance(v[0], dict) else ""))
                if v:
                    p(f"        sample[0]: {json.dumps(v[0], ensure_ascii=False)[:200]}")
            else:
                p(f"    {k!r:30s} = {repr(v)[:80]}")
        _data["N.ClosingPriceInfo.full"] = cpi_full

    # ─────────────────────────────────────────────────────────────────────────
    section("O. GetMarketWatch — FULL FIELD DEEP DIVE")
    # ─────────────────────────────────────────────────────────────────────────

    p("  Fetching full ETF market watch data (paperTypes=[6]) ...")
    status, data, ms = get(f"{CDN}/MarketWatch/GetMarketWatch",
                           params={"paperTypes": "[6]", "showTraded": "true", "withBestLimits": "true"})
    if status == 200 and data and isinstance(data, dict):
        p(f"  Response keys: {list(data.keys())}")
        for k, v in data.items():
            p(f"    {k!r:30s} → {describe(v, 2)[:300]}")
        # Find our ins_code in the result
        for k, v in data.items():
            if isinstance(v, list) and v:
                items = v
                target = [x for x in items if str(x.get("insCode","")) == str(ins_code)]
                if target:
                    p(f"\n  Found {symbol} ({ins_code}) in [{k}]:")
                    p(f"  ALL fields:")
                    for fk, fv in target[0].items():
                        p(f"    {fk!r:30s} = {repr(fv)[:80]}")
                    break
        _data["O.GetMarketWatch.ETF.full"] = data
    else:
        p(f"  [{status}] {ms:.0f}ms — no data")

    # ─────────────────────────────────────────────────────────────────────────
    section("P. Discovery — unknown endpoint brute-force")
    # ─────────────────────────────────────────────────────────────────────────
    p("  Testing less-known action names across controllers ...")

    unknown_probes = [
        # ClosingPrice extras
        f"{CDN}/ClosingPrice/GetClosingPriceDaily/{ins_code}",
        f"{CDN}/ClosingPrice/GetMonthDayTseHistory/{ins_code}",
        f"{CDN}/ClosingPrice/GetWeeklyStats/{ins_code}",
        f"{CDN}/ClosingPrice/GetAdjustedHistory/{ins_code}/30",
        # Trade extras
        f"{CDN}/Trade/GetTopBuyers/{ins_code}/{last_trade_date}",
        f"{CDN}/Trade/GetTopSellers/{ins_code}/{last_trade_date}",
        f"{CDN}/Trade/GetInstTradeHistory/{ins_code}/{last_trade_date}",
        f"{CDN}/Trade/GetTradeVolume/{ins_code}/{last_trade_date}/{today}",
        # Instrument extras
        f"{CDN}/Instrument/GetInstrumentList",
        f"{CDN}/Instrument/GetAllInstruments",
        f"{CDN}/Instrument/GetETFByType/6",
        f"{CDN}/Instrument/GetFixedIncomeFunds",
        f"{CDN}/Instrument/GetInstrumentByFlow/2",
        # MutualFund extras
        f"{CDN}/MutualFund/GetIssuedUnit/{ins_code}",
        f"{CDN}/MutualFund/GetIssuedUnitHistory/{ins_code}",
        f"{CDN}/MutualFund/GetNAVHistory/{ins_code}/{last_trade_date}/{today}",
        f"{CDN}/MutualFund/GetFundPortfolio/{ins_code}",
        f"{CDN}/MutualFund/GetFundType",
        # Market wide
        f"{CDN}/MarketData/GetMarketHours",
        f"{CDN}/MarketData/GetHolidays",
        f"{CDN}/MarketData/GetTradingCalendar",
        f"{CDN}/MarketData/GetTradingHours",
        f"{CDN}/Calendar/GetHolidays",
        f"{CDN}/Calendar/GetTradingDays",
        f"{CDN}/MarketWatch/GetMWByPaperType/6",
        f"{CDN}/MarketWatch/GetMarketWatchPlus",
        # Ownership
        f"{CDN}/Owner/GetOwnershipHistory/{ins_code}",
        f"{CDN}/Owner/GetLegalOwnership/{ins_code}",
        f"{CDN}/Ownership/GetOwnershipChange/{ins_code}/{last_trade_date}",
        # Financial statements
        f"{CDN}/Financial/GetFinancialData/{ins_code}",
        f"{CDN}/Statement/GetStatement/{ins_code}",
    ]

    for url in unknown_probes:
        label = "P." + url.replace(CDN + "/", "").replace(f"/{ins_code}", "/{ins}").replace(f"/{last_trade_date}", "/{date}").replace(f"/{today}", "/{today}")
        status, data, ms = get(url)
        ok_flag = status == 200 and data and data != {} and data != [] and data != {"tradeHistory": []}
        mark = "✓" if ok_flag else ("·" if status == 200 else "-")
        hint = ""
        if ok_flag and isinstance(data, dict):
            hint = f"  keys={list(data.keys())[:6]}"
        p(f"  {mark}  [{status:3d}] {ms:5.0f}ms  {label}{hint}")
        if ok_flag and isinstance(data, dict):
            for k, v in data.items():
                p(f"         {k!r:32s} → {describe(v, 3)[:200]}")
        elif ok_flag and data:
            p(f"         raw: {str(data)[:200]}")
        _data[label] = {"url": url, "status": status, "ms": round(ms),
                        "data": data if ok_flag else None}
        time.sleep(0.2)

    # ─────────────────────────────────────────────────────────────────────────
    section("SUMMARY")
    # ─────────────────────────────────────────────────────────────────────────

    working  = [(k, v) for k, v in _data.items()
                if v.get("status") == 200 and v.get("data") and v.get("data") != {}]
    empty    = [(k, v) for k, v in _data.items()
                if v.get("status") == 200 and (not v.get("data") or v.get("data") == {})]
    failed   = [(k, v) for k, v in _data.items()
                if v.get("status") not in (200, None) and v.get("status", 0) != 0]

    p(f"\n  Endpoints with DATA:    {len(working)}")
    for k, v in working:
        d = v.get("data") or {}
        hint = ""
        if isinstance(d, dict):
            for key, val in d.items():
                if isinstance(val, list):
                    hint = f"  [{key}] {len(val)} rows"
                    break
        p(f"    ✓ {k:50s}  {v['status']}  {v['ms']}ms{hint}")

    p(f"\n  Endpoints EMPTY (200 but no data):  {len(empty)}")
    for k, v in empty:
        p(f"    · {k}")

    p(f"\n  Endpoints FAILED:  {len(failed)}")
    for k, v in failed:
        p(f"    ✗ {k}  [{v.get('status')}]")


# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol",   default="کیان")
    ap.add_argument("--ins-code", default=None)
    args = ap.parse_args()

    symbol   = args.symbol
    ins_code = args.ins_code

    if not ins_code:
        try:
            import sqlite3
            db = sqlite3.connect(ROOT / "data" / "arbitrage.db")
            row = db.execute(
                "SELECT ins_code FROM daily_history WHERE symbol=? AND ins_code!='' LIMIT 1",
                (symbol,)
            ).fetchone()
            db.close()
            if row:
                ins_code = row[0]
        except Exception:
            pass

    if not ins_code:
        # Try search
        from urllib.parse import quote
        status, data, _ = get(f"{CDN}/Instrument/GetInstrumentSearch/{quote(symbol)}")
        if status == 200 and data:
            results = data.get("instrumentSearch") or []
            for r in results:
                if r.get("lVal18AFC", "").strip() == symbol:
                    ins_code = r.get("insCode", "")
                    break

    if not ins_code:
        print(f"ERROR: could not resolve ins_code for '{symbol}'")
        sys.exit(1)

    txt_path  = ROOT / "tsetmc_full_analysis.txt"
    json_path = ROOT / "tsetmc_full_analysis.json"

    try:
        run(ins_code, symbol)
    except Exception as e:
        p(f"\n[CRASH] {type(e).__name__}: {e}")
        import traceback
        p(traceback.format_exc())
    finally:
        # Always write outputs — even if run() crashed partway through
        txt_path.write_text("\n".join(_lines), encoding="utf-8")
        json_path.write_text(
            json.dumps(_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )
        print(f"\n{'='*60}")
        print(f"✓  Text report: {txt_path}")
        print(f"✓  JSON dump:   {json_path}")
        print()
        print("  Push هر دو فایل را:")
        print("  git add tsetmc_full_analysis.txt tsetmc_full_analysis.json")
        print("  git commit -m \"debug: TSETMC full API analysis\"")
        print("  git push origin claude/charming-volta-BsNnm")


if __name__ == "__main__":
    main()
