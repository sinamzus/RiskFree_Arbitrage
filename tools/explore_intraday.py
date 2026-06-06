#!/usr/bin/env python3
"""
اشراف کامل بر دیتای درون‌روز TSETMC
======================================
تمام endpoint های ممکن برای دیتای intraday — هم امروز (real-time) هم تاریخی.

هدف: پیدا کردن:
  1. میله‌های دقیقه‌ای (OHLCV per minute) برای روزهای قبل
  2. تیک‌های معاملاتی تاریخی
  3. تغییرات اردربوک تاریخی (delta stream)
  4. ورود/خروج حقیقی/حقوقی درون‌روز

اجرا:
    python tools/explore_intraday.py --symbol کیان
    python tools/explore_intraday.py --ins-code 53251602435454519

خروجی:
    intraday_full.txt   (در ریشه پروژه)
    intraday_full.json
"""

import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from config import TSETMC_CDN, REQUEST_HEADERS, REQUEST_TIMEOUT

CDN = TSETMC_CDN

sess = requests.Session()
sess.headers.update(REQUEST_HEADERS)
sess.headers["Referer"] = "https://www.tsetmc.com/"

_lines: list[str] = []
_data:  dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get(url: str, params: dict = None) -> tuple[int, Any, float]:
    t0 = time.time()
    try:
        r = sess.get(url, params=params, timeout=REQUEST_TIMEOUT)
        ms = (time.time() - t0) * 1000
        try:
            return r.status_code, r.json(), ms
        except Exception:
            return r.status_code, r.text[:300] if r.text else None, ms
    except Exception as e:
        return 0, str(e)[:200], (time.time() - t0) * 1000


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


def is_html(data) -> bool:
    return isinstance(data, str) and "<!doctype" in data.lower()


def fmt_rows(data, list_key: str = None) -> tuple[int, list]:
    """Return (count, rows) from a JSON response."""
    if not isinstance(data, dict):
        return 0, []
    if list_key:
        rows = data.get(list_key, [])
        return len(rows), rows
    for v in data.values():
        if isinstance(v, list):
            return len(v), v
    return 0, []


def probe(label: str, url: str, params: dict = None, delay: float = 0.25) -> tuple[bool, Any]:
    status, data, ms = get(url, params)

    if is_html(data):
        mark = "h"   # HTML SPA route — not an API
        hint = "  (HTML — SPA route)"
        ok = False
    elif status == 200 and data and data != {} and data != [] and data != {"tradeHistory": []}:
        n, rows = fmt_rows(data)
        hint = f"  [{list(data.keys())[0] if isinstance(data, dict) else ''}] {n} rows" if n else ""
        mark = "✓"
        ok = True
    elif status == 200:
        mark = "·"
        hint = "  (200 empty)"
        ok = False
    else:
        mark = f"{status}"
        hint = ""
        ok = False

    p(f"  {mark}  {ms:5.0f}ms  {label}{hint}")

    if ok and isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                p(f"         {k!r:32s} → list[{len(v)}]" + (
                    f"  keys={list(v[0].keys())}" if v and isinstance(v[0], dict) else ""))
                if v and isinstance(v[0], dict):
                    p(f"           sample[0]: {json.dumps(v[0], ensure_ascii=False)[:250]}")
                    if len(v) > 1:
                        p(f"           sample[-1]: {json.dumps(v[-1], ensure_ascii=False)[:250]}")
            elif isinstance(v, dict):
                p(f"         {k!r:32s} → dict  keys={list(v.keys())}")
                for dk, dv in v.items():
                    p(f"              {dk!r:28s} = {repr(dv)[:80]}")
            else:
                p(f"         {k!r:32s} = {repr(v)[:100]}")

    _data[label] = {"url": url, "params": params, "status": status,
                    "ms": round(ms), "data": data if ok else None,
                    "is_html": is_html(data)}
    time.sleep(delay)
    return ok, data


# ═════════════════════════════════════════════════════════════════════════════
def run(ins_code: str, symbol: str, trade_date: int, today: int, yesterday: int):

    p(f"TSETMC Intraday Deep Dive")
    p(f"Symbol: {symbol}   insCode: {ins_code}")
    p(f"Trade date (last known): {trade_date}")
    p(f"Today: {today}   Yesterday: {yesterday}")
    p(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── تیک‌های معاملاتی ─────────────────────────────────────────────────────
    section("1. Trade/GetTradeHistory — ریز معاملات (تیک)")

    sub("تاریخ کاری (last trade date)")
    probe(f"GetTradeHistory/{trade_date}/false",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/{trade_date}/false")
    probe(f"GetTradeHistory/{trade_date}/true (with canceled)",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/{trade_date}/true")

    sub("امروز")
    probe(f"GetTradeHistory/{today}/false",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/{today}/false")

    sub("دیروز")
    probe(f"GetTradeHistory/{yesterday}/false",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/{yesterday}/false")

    sub("پارامتر 0 (همه؟ آخرین؟)")
    probe("GetTradeHistory/0/false",
          f"{CDN}/Trade/GetTradeHistory/{ins_code}/0/false")

    # ── میله‌های دقیقه‌ای ─────────────────────────────────────────────────────
    section("2. Trade/GetTradeIntraday — میله‌های درون‌روز (OHLCV per ~minute)")

    sub("امروز (بدون تاریخ)")
    probe("GetTradeIntraday (today)",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}")

    sub("تاریخ کاری (آیا تاریخی پشتیبانی می‌شود؟)")
    probe(f"GetTradeIntraday/{trade_date}",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}/{trade_date}")

    sub("دیروز")
    probe(f"GetTradeIntraday/{yesterday}",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}/{yesterday}")

    sub("امروز با تاریخ")
    probe(f"GetTradeIntraday/{today}",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}/{today}")

    sub("بازه تاریخ (start/end) — تست‌های احتمالی")
    probe(f"GetTradeIntraday/{trade_date}/{today}",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}/{trade_date}/{today}")
    probe(f"GetTradeIntraday/{trade_date}/{trade_date}",
          f"{CDN}/Trade/GetTradeIntraday/{ins_code}/{trade_date}/{trade_date}")

    # ── نام‌های جایگزین میله درون‌روز ────────────────────────────────────────
    section("3. نام‌های جایگزین Intraday (discovery)")

    candidates = [
        f"{CDN}/Trade/GetIntraDayTrade/{ins_code}",
        f"{CDN}/Trade/GetIntraDayTrade/{ins_code}/{trade_date}",
        f"{CDN}/Trade/GetIntraDayHistory/{ins_code}/{trade_date}",
        f"{CDN}/Trade/GetIntraDayPrice/{ins_code}/{trade_date}",
        f"{CDN}/Trade/GetTradeHistory1Min/{ins_code}/{trade_date}",
        f"{CDN}/Trade/GetMinuteHistory/{ins_code}/{trade_date}",
        f"{CDN}/Trade/GetCandleHistory/{ins_code}/{trade_date}",
        f"{CDN}/Trade/GetOHLCV/{ins_code}/{trade_date}",
        f"{CDN}/ClosingPrice/GetIntraDayHistory/{ins_code}/{trade_date}",
        f"{CDN}/ClosingPrice/GetIntraDayPrice/{ins_code}/{trade_date}",
        f"{CDN}/ClosingPrice/GetPricePerMinute/{ins_code}/{trade_date}",
        f"{CDN}/ClosingPrice/GetMinuteHistory/{ins_code}/{trade_date}",
        f"{CDN}/ClosingPrice/GetOHLCVHistory/{ins_code}/{trade_date}",
        f"{CDN}/MarketData/GetIntraDayHistory/{ins_code}/{trade_date}",
    ]
    for url in candidates:
        label = url.replace(CDN + "/", "").replace(f"/{ins_code}", "/{ins}").replace(f"/{trade_date}", "/{date}")
        probe(label, url)

    # ── GetTrade (real-time stream) ───────────────────────────────────────────
    section("4. Trade/GetTrade — معاملات real-time (امروز)")

    probe("GetTrade (live)",
          f"{CDN}/Trade/GetTrade/{ins_code}")
    probe("GetTrade/0",
          f"{CDN}/Trade/GetTrade/{ins_code}/0")
    probe(f"GetTrade/{trade_date}",
          f"{CDN}/Trade/GetTrade/{ins_code}/{trade_date}")
    probe(f"GetTrade/{today}",
          f"{CDN}/Trade/GetTrade/{ins_code}/{today}")

    # ── اردربوک تاریخی ───────────────────────────────────────────────────────
    section("5. BestLimits — اردربوک (زنده و تاریخی)")

    sub("زنده (بدون تاریخ)")
    probe("BestLimits (live)",
          f"{CDN}/BestLimits/{ins_code}")

    sub("تاریخ کاری")
    ok, d = probe(f"BestLimits/{trade_date}",
                  f"{CDN}/BestLimits/{ins_code}/{trade_date}")
    if ok and isinstance(d, dict):
        rows = d.get("bestLimitsHistory", [])
        if rows:
            times = sorted(set(r.get("hEven", 0) for r in rows))
            p(f"         زمان اول: {times[0]:06d}  آخر: {times[-1]:06d}  تعداد timeها: {len(times)}")
            refs = sorted(set(r.get("refID", 0) for r in rows))
            p(f"         refID اول: {refs[0]}  آخر: {refs[-1]}  تعداد event: {len(refs)}")

    sub("امروز — آیا real-time ذخیره می‌شود؟")
    probe(f"BestLimits/{today}",
          f"{CDN}/BestLimits/{ins_code}/{today}")

    sub("دیروز")
    probe(f"BestLimits/{yesterday}",
          f"{CDN}/BestLimits/{ins_code}/{yesterday}")

    sub("بازه زمانی OB (آیا تکه‌تکه می‌شود؟)")
    probe(f"BestLimits/{trade_date}/83000",
          f"{CDN}/BestLimits/{ins_code}/{trade_date}/83000")
    probe(f"BestLimits/{trade_date}/83000/150000",
          f"{CDN}/BestLimits/{ins_code}/{trade_date}/83000/150000")
    probe(f"BestLimits/{trade_date}/0",
          f"{CDN}/BestLimits/{ins_code}/{trade_date}/0")

    # ── حقیقی/حقوقی تاریخی ───────────────────────────────────────────────────
    section("6. ClientType — حقیقی/حقوقی (تاریخی)")

    sub("تاریخ کاری (تأیید شده)")
    ok, d = probe(f"ClientType/GetClientTypeHistory/{trade_date}",
                  f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{trade_date}")
    if ok and isinstance(d, dict):
        ct = d.get("clientType", {})
        if isinstance(ct, dict):
            buy_i  = ct.get("buy_I_Volume", 0)
            buy_n  = ct.get("buy_N_Volume", 0)
            sell_i = ct.get("sell_I_Volume", 0)
            sell_n = ct.get("sell_N_Volume", 0)
            total  = (buy_i + buy_n) or 1
            p(f"         حقیقی خرید:  {buy_i:>15,.0f}  ({buy_i/total*100:.1f}%)")
            p(f"         حقوقی خرید:  {buy_n:>15,.0f}  ({buy_n/total*100:.1f}%)")
            p(f"         حقیقی فروش:  {sell_i:>15,.0f}")
            p(f"         حقوقی فروش:  {sell_n:>15,.0f}")

    sub("امروز — آیا real-time داده دارد؟")
    probe(f"ClientType/GetClientTypeHistory/{today}",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{today}")

    sub("دیروز")
    probe(f"ClientType/GetClientTypeHistory/{yesterday}",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{yesterday}")

    sub("پارامتر 0")
    probe("ClientType/GetClientTypeHistory/0",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/0")

    sub("آیا تاریخچه تمام روزها وجود دارد؟")
    for label, date in [("7 days ago", int((datetime.now() - timedelta(days=7)).strftime("%Y%m%d"))),
                        ("30 days ago", int((datetime.now() - timedelta(days=30)).strftime("%Y%m%d"))),
                        ("90 days ago", int((datetime.now() - timedelta(days=90)).strftime("%Y%m%d")))]:
        probe(f"ClientType/{date} ({label})",
              f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{date}")

    sub("آیا multi-day history endpoint وجود دارد؟")
    week_ago = int((datetime.now() - timedelta(days=7)).strftime("%Y%m%d"))
    probe(f"ClientType/GetClientTypeHistoryAll/{trade_date}/{today}",
          f"{CDN}/ClientType/GetClientTypeHistoryAll/{ins_code}/{trade_date}/{today}")
    probe(f"ClientType/GetClientTypeHistory (multi?)",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/{week_ago}/{today}")
    probe(f"ClientType/GetClientTypeHistoryN/30",
          f"{CDN}/ClientType/GetClientTypeHistory/{ins_code}/30")

    # ── MarketWatch بررسی عمیق ────────────────────────────────────────────────
    section("7. MarketWatch — تابلو ETF ها (بررسی پارامترها)")

    mw_base = f"{CDN}/MarketWatch/GetMarketWatch"

    sub("آیا بدون پارامتر کار می‌کند؟")
    probe("GetMarketWatch (no params)", mw_base)

    sub("paperType=6 (ETF صندوق)")
    ok, d = probe("GetMarketWatch?paperTypes=[6]",
                  mw_base, params={"paperTypes": "[6]"})
    if ok and isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, list) and v:
                p(f"         [{k}]: {len(v)} items  keys={list(v[0].keys()) if isinstance(v[0], dict) else '?'}")

    sub("با withBestLimits=true")
    ok, d = probe("GetMarketWatch?paperTypes=[6]&withBestLimits=true",
                  mw_base,
                  params={"paperTypes": "[6]", "showTraded": "true",
                          "withBestLimits": "true", "hEven": "0"})
    if ok and isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, list) and v:
                p(f"         [{k}]: {len(v)} items")
                if isinstance(v[0], dict):
                    p(f"           fields: {list(v[0].keys())}")
                    # Find our ins_code
                    found = [x for x in v if str(x.get("insCode", "")) == ins_code]
                    if found:
                        p(f"           {symbol} found:")
                        for fk, fv in found[0].items():
                            p(f"             {fk!r:28s} = {repr(fv)[:80]}")

    sub("با hEven (بازه زمانی؟)")
    probe("GetMarketWatch?hEven=83000",
          mw_base, params={"paperTypes": "[6]", "hEven": "83000"})
    probe("GetMarketWatch?refID=...",
          mw_base, params={"paperTypes": "[6]", "refID": "0"})

    # ── قیمت لحظه‌ای با زمان ─────────────────────────────────────────────────
    section("8. ClosingPriceInfo — آیا تاریخ/زمان را قبول می‌کند؟")

    sub("امروز با تاریخ")
    probe(f"GetClosingPriceInfo (no param)", f"{CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}")
    probe(f"GetClosingPriceInfo/{today}", f"{CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}/{today}")
    probe(f"GetClosingPriceInfo/{trade_date}", f"{CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}/{trade_date}")

    sub("GetClosingPriceDailyList — آیا از تاریخ شروع می‌کند؟")
    probe(f"GetClosingPriceDailyList/365/{today}",
          f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/365/{today}")
    probe(f"GetClosingPriceDailyList/365/{trade_date}",
          f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/365/{trade_date}")

    sub("GetClosingPriceHistory — بازه تاریخ")
    probe(f"GetClosingPriceHistory/{trade_date}",
          f"{CDN}/ClosingPrice/GetClosingPriceHistory/{ins_code}/{trade_date}")
    probe(f"GetClosingPriceHistory/{trade_date}/{today}",
          f"{CDN}/ClosingPrice/GetClosingPriceHistory/{ins_code}/{trade_date}/{today}")

    # ── چارت TradingView ─────────────────────────────────────────────────────
    section("9. TradingView / Charting Library endpoint (کندل تاریخی)")
    # TSETMC uses TradingView charting lib — data must come from somewhere

    tv_base = "https://cdn.tsetmc.com"
    tv_candidates = [
        f"{tv_base}/api/Trade/GetChartData/{ins_code}/{trade_date}",
        f"{tv_base}/api/Trade/GetCandleData/{ins_code}/{trade_date}",
        f"{tv_base}/api/Chart/GetData/{ins_code}/{trade_date}",
        f"{tv_base}/api/Chart/GetHistory/{ins_code}",
        f"{tv_base}/api/ClosingPrice/GetChartHistory/{ins_code}/{trade_date}",
        f"{tv_base}/api/Trade/GetTradeChartHistory/{ins_code}/{trade_date}",
        "https://cdn.tsetmc.com/History",
        f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{ins_code}/{trade_date}/false",
    ]
    for url in tv_candidates:
        label = url.replace(f"{tv_base}/api/", "").replace(f"/{ins_code}", "/{ins}").replace(f"/{trade_date}", "/{date}")
        probe(label, url)

    sub("TradingView UDF — /history endpoint (برای کندل‌های minute-level)")
    # TradingView UDF protocol
    udf_base = "https://cdn.tsetmc.com"
    udf_probes = [
        (f"{udf_base}/history", {"symbol": ins_code, "resolution": "1",
                                  "from": "1748227200", "to": "1748313600"}),
        (f"{udf_base}/history", {"symbol": ins_code, "resolution": "D",
                                  "from": "1748227200", "to": "1748313600"}),
        (f"{udf_base}/api/history", {"symbol": ins_code, "resolution": "1",
                                      "from": "1748227200", "to": "1748313600"}),
        ("https://api.tsetmc.com/history", {"symbol": ins_code, "resolution": "1",
                                              "from": "1748227200", "to": "1748313600"}),
        (f"https://www.tsetmc.com/history", {"symbol": ins_code, "resolution": "1",
                                               "from": "1748227200", "to": "1748313600"}),
    ]
    for url, params in udf_probes:
        short = url.replace("https://", "")
        probe(f"TradingView UDF: {short}", url, params=params)

    # ── خلاصه ────────────────────────────────────────────────────────────────
    section("SUMMARY — نتایج")

    ok_endpoints = [(k, v) for k, v in _data.items()
                    if v.get("status") == 200 and v.get("data") and not v.get("is_html")]
    html_endpoints = [(k, v) for k, v in _data.items() if v.get("is_html")]
    fail_endpoints = [(k, v) for k, v in _data.items()
                      if v.get("status") not in (200, 0, None) and not v.get("is_html")]

    p(f"\n  ✓ داده واقعی: {len(ok_endpoints)}")
    for k, v in ok_endpoints:
        d_ = v.get("data") or {}
        hint = ""
        if isinstance(d_, dict):
            for key, val in d_.items():
                if isinstance(val, list):
                    hint = f"  [{key}] {len(val)} rows"
                    break
        p(f"    ✓ {k:60s}  {v['status']}  {v['ms']}ms{hint}")

    p(f"\n  h HTML (SPA): {len(html_endpoints)}")
    for k, v in html_endpoints:
        p(f"    h {k}")

    p(f"\n  ✗ 404/5xx: {len(fail_endpoints)}")
    for k, v in fail_endpoints:
        p(f"    ✗ [{v.get('status')}] {k}")


# ═════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol",   default="کیان")
    ap.add_argument("--ins-code", default=None, dest="ins_code")
    ap.add_argument("--date",     type=int, default=None,
                    help="تاریخ کاری YYYYMMDD (پیش‌فرض: آخرین تاریخ در DB)")
    args = ap.parse_args()

    symbol, ins_code = args.symbol, args.ins_code
    trade_date = args.date

    # Resolve ins_code + last trade date from DB
    try:
        import sqlite3
        conn = sqlite3.connect(ROOT / "data" / "arbitrage.db")
        if not ins_code:
            row = conn.execute(
                "SELECT ins_code FROM daily_history WHERE symbol=? AND ins_code!='' LIMIT 1",
                (symbol,)
            ).fetchone()
            if row:
                ins_code = row[0]
        if not trade_date:
            row2 = conn.execute(
                "SELECT MAX(date) FROM daily_history WHERE symbol=?", (symbol,)
            ).fetchone()
            if row2 and row2[0]:
                trade_date = row2[0]
        conn.close()
    except Exception:
        pass

    if not ins_code:
        print(f"ERROR: ins_code not found for '{symbol}'")
        sys.exit(1)

    now = datetime.now()
    today     = int(now.strftime("%Y%m%d"))
    yesterday = int((now - timedelta(days=1)).strftime("%Y%m%d"))
    if not trade_date:
        trade_date = int((now - timedelta(days=2)).strftime("%Y%m%d"))

    txt_path  = ROOT / "intraday_full.txt"
    json_path = ROOT / "intraday_full.json"

    try:
        run(ins_code, symbol, trade_date, today, yesterday)
    except Exception as e:
        p(f"\n[CRASH] {type(e).__name__}: {e}")
        import traceback
        p(traceback.format_exc())
    finally:
        txt_path.write_text("\n".join(_lines), encoding="utf-8")
        json_path.write_text(
            json.dumps(_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )
        print(f"\n{'='*60}")
        print(f"✓ Text: {txt_path}")
        print(f"✓ JSON: {json_path}")
        print()
        print("git add intraday_full.txt intraday_full.json")
        print("git commit -m \"debug: intraday API deep dive\"")
        print("git push origin claude/charming-volta-BsNnm")


if __name__ == "__main__":
    main()
