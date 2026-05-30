#!/usr/bin/env python3
"""
آنالیزور API صفحه نماد TSETMC
================================
این اسکریپت تمام endpoint های شناخته‌شده و احتمالی TSETMC را
برای یک نماد آزمایش می‌کند و فرمت دقیق پاسخ‌ها را نمایش می‌دهد.
هدف: پیدا کردن بهترین منبع داده تیک‌به‌تیک لحظه‌ای.

اجرا:
    python tools/analyze_tsetmc_api.py
    python tools/analyze_tsetmc_api.py --symbol کیان
    python tools/analyze_tsetmc_api.py --ins-code 35425587644337450
"""

import sys, argparse, json, time
from pathlib import Path
from datetime import datetime, timedelta
from pprint import pformat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import TSETMC_CDN
from data_fetcher import TSETMCFetcher
import sqlite3

TSETMC_MAIN = "https://www.tsetmc.com"

# ─────────────────────────────────────────────────────────────────────────────

def hdr(title: str):
    bar = "─" * 70
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)

def ok(label, val=""):
    print(f"  ✓  {label}" + (f"  →  {val}" if val else ""))

def fail(label, val=""):
    print(f"  ✗  {label}" + (f"  →  {val}" if val else ""))

def show_keys(data, indent=4):
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                print(f"{' '*indent}{k}: list[{len(v)}]" +
                      (f"  keys={list(v[0].keys())}" if v and isinstance(v[0], dict) else ""))
            elif isinstance(v, dict):
                print(f"{' '*indent}{k}: dict  keys={list(v.keys())}")
            else:
                print(f"{' '*indent}{k}: {repr(v)[:80]}")
    elif isinstance(data, list):
        print(f"{' '*indent}list[{len(data)}]" +
              (f"  item_keys={list(data[0].keys())}" if data and isinstance(data[0], dict) else ""))

def show_sample(lst, n=3):
    if not lst:
        return
    for item in lst[:n]:
        print(f"    {json.dumps(item, ensure_ascii=False)}")
    if len(lst) > n:
        print(f"    ... ({len(lst)-n} more rows)")


# ─────────────────────────────────────────────────────────────────────────────

def analyze(ins_code: str, symbol: str):
    f = TSETMCFetcher()
    today = int(datetime.now().strftime("%Y%m%d"))
    yesterday = int((datetime.now() - timedelta(days=1)).strftime("%Y%m%d"))

    print(f"\n{'═'*70}")
    print(f"  TSETMC API Analyzer")
    print(f"  Symbol: {symbol}   insCode: {ins_code}")
    print(f"  Date:   {today}    Yesterday: {yesterday}")
    print(f"{'═'*70}")

    # ── 1. ClosingPriceInfo (live price bar) ─────────────────────────────────
    hdr("1. ClosingPrice/GetClosingPriceInfo  (live OHLCV)")
    url = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}"
    data = f._get(url)
    if data:
        ok("endpoint responded")
        show_keys(data)
        cp = data.get("closingPriceInfo") or {}
        if cp:
            print(f"    last={cp.get('pDrCotVal')}  close={cp.get('pClosing')}"
                  f"  open={cp.get('priceFirst')}  high={cp.get('priceMax')}"
                  f"  low={cp.get('priceMin')}  volume={cp.get('qTotTran5J')}"
                  f"  trades={cp.get('zTotTran')}")
    else:
        fail("no response")

    # ── 2. Trade/GetTradeHistory today ───────────────────────────────────────
    hdr(f"2. Trade/GetTradeHistory/{ins_code}/{today}/false  (today's ticks)")
    url2 = f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins_code}/{today}/false"
    data2 = f._get(url2, silent=True)
    if data2:
        ok("endpoint responded")
        show_keys(data2)
        trades = data2.get("tradeHistory") or []
        ok(f"trades count: {len(trades)}")
        if trades:
            t = trades[0]
            print(f"    first trade fields: {list(t.keys())}")
            show_sample(trades, 3)
            show_sample(trades[-3:], 3)
            # Check if hEven is present (time)
            if "hEven" in t:
                ok(f"hEven (time) field present  → e.g. {trades[0]['hEven']}")
            if "nTran" in t:
                ok(f"nTran (seq) field present   → e.g. {trades[0]['nTran']}")
    else:
        fail("no response / empty")
    time.sleep(0.3)

    # ── 3. Trade/GetTradeHistory yesterday ───────────────────────────────────
    hdr(f"3. Trade/GetTradeHistory/{ins_code}/{yesterday}/false  (yesterday ticks)")
    url3 = f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins_code}/{yesterday}/false"
    data3 = f._get(url3, silent=True)
    if data3:
        trades3 = data3.get("tradeHistory") or []
        ok(f"trades count: {len(trades3)}")
        if trades3:
            show_sample(trades3[:2], 2)
    else:
        fail("no response")
    time.sleep(0.3)

    # ── 4. Trade/GetTradeHistory with canceled=true ───────────────────────────
    hdr(f"4. Trade/GetTradeHistory/{ins_code}/{today}/true  (with canceled trades)")
    url4 = f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins_code}/{today}/true"
    data4 = f._get(url4, silent=True)
    if data4:
        trades4 = data4.get("tradeHistory") or []
        ok(f"trades count (with canceled): {len(trades4)}")
        canceled = [t for t in trades4 if t.get("canceled")]
        ok(f"canceled trades: {len(canceled)}")
    else:
        fail("no response")
    time.sleep(0.3)

    # ── 5. BestLimits live OB ─────────────────────────────────────────────────
    hdr(f"5. BestLimits/{ins_code}  (live order book)")
    url5 = f"{TSETMC_CDN}/BestLimits/{ins_code}"
    data5 = f._get(url5, silent=True)
    if data5:
        ok("endpoint responded")
        show_keys(data5)
        bl = data5.get("bestLimits") or []
        ok(f"levels: {len(bl)}")
        if bl:
            show_sample(bl[:2], 2)
    else:
        fail("no response")
    time.sleep(0.3)

    # ── 6. BestLimits historical (today) ─────────────────────────────────────
    hdr(f"6. BestLimits/{ins_code}/{today}  (today OB history)")
    url6 = f"{TSETMC_CDN}/BestLimits/{ins_code}/{today}"
    data6 = f._get(url6, silent=True)
    if data6:
        ok("endpoint responded")
        rows = data6.get("bestLimitsHistory") or []
        ok(f"delta rows: {len(rows)}")
        if rows:
            print(f"    fields: {list(rows[0].keys())}")
            show_sample(rows[:2], 2)
            show_sample(rows[-2:], 2)
    else:
        fail("no response / empty")
    time.sleep(0.3)

    # ── 7. BestLimits historical (yesterday) ────────────────────────────────
    hdr(f"7. BestLimits/{ins_code}/{yesterday}  (yesterday OB history)")
    url7 = f"{TSETMC_CDN}/BestLimits/{ins_code}/{yesterday}"
    data7 = f._get(url7, silent=True)
    if data7:
        rows7 = data7.get("bestLimitsHistory") or []
        ok(f"delta rows: {len(rows7)}")
    else:
        fail("no response / empty")
    time.sleep(0.3)

    # ── 8. ClosingPrice/GetClosingPriceDailyList ──────────────────────────────
    hdr(f"8. ClosingPrice/GetClosingPriceDailyList/{ins_code}/30  (30-day OHLCV)")
    url8 = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/30"
    data8 = f._get(url8, silent=True)
    if data8:
        ok("endpoint responded")
        show_keys(data8)
        dl = data8.get("closingPriceDailyList") or []
        ok(f"daily bars: {len(dl)}")
        if dl:
            print(f"    fields: {list(dl[0].keys())}")
            show_sample(dl[:2], 2)
    else:
        fail("no response")
    time.sleep(0.3)

    # ── 9. InstrumentInfo ────────────────────────────────────────────────────
    hdr(f"9. Instrument/GetInstrumentInfo/{ins_code}  (fund metadata)")
    url9 = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
    data9 = f._get(url9, silent=True)
    if data9:
        ok("endpoint responded")
        show_keys(data9)
    else:
        fail("no response")
    time.sleep(0.3)

    # ── 10. Try additional patterns found in TSETMC page source ─────────────
    hdr("10. Additional endpoints (experimental)")

    extra = [
        (f"{TSETMC_CDN}/Instrument/GetInstrumentStatistic/{ins_code}",
         "Instrument/GetInstrumentStatistic"),
        (f"{TSETMC_CDN}/Trade/GetFutureStateStats/{ins_code}/{today}",
         "Trade/GetFutureStateStats"),
        (f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceHistory/{ins_code}/1",
         "ClosingPrice/GetClosingPriceHistory/1"),
        (f"{TSETMC_CDN}/MarketData/GetTseClientTypeAll",
         "MarketData/GetTseClientTypeAll"),
        (f"{TSETMC_CDN}/Trade/GetClientTypeHistory/{ins_code}/{today}",
         "Trade/GetClientTypeHistory/{today}"),
        (f"{TSETMC_CDN}/Trade/GetClientTypeHistory/{ins_code}/0",
         "Trade/GetClientTypeHistory/0 (live?)"),
    ]

    for url_e, label in extra:
        data_e = f._get(url_e, silent=True)
        if data_e and data_e != {}:
            ok(f"FOUND: {label}")
            show_keys(data_e)
        else:
            print(f"  ·  (empty) {label}")
        time.sleep(0.2)

    # ── 11. Cross-check against DB ──────────────────────────────────────────
    hdr("11. Cross-check: DB intraday_trades for this symbol")
    try:
        db = sqlite3.connect(ROOT / "data" / "arbitrage.db")
        db.row_factory = sqlite3.Row
        dates = db.execute(
            "SELECT date, COUNT(*) as n FROM intraday_trades WHERE symbol=? "
            "GROUP BY date ORDER BY date DESC LIMIT 10",
            (symbol,)
        ).fetchall()
        if dates:
            ok(f"intraday_trades found for {symbol}")
            for d in dates:
                print(f"    date={d['date']}  ticks={d['n']}")
            latest_date = dates[0]["date"]
            sample = db.execute(
                "SELECT time, price, volume, canceled FROM intraday_trades "
                "WHERE symbol=? AND date=? ORDER BY seq LIMIT 5",
                (symbol, latest_date)
            ).fetchall()
            print(f"    First 5 ticks on {latest_date}:")
            for t in sample:
                t_str = str(t['time']).zfill(6)
                print(f"      {t_str[:2]}:{t_str[2:4]}:{t_str[4:]}  "
                      f"price={t['price']:,}  vol={t['volume']:,}"
                      f"{'  [canceled]' if t['canceled'] else ''}")
        else:
            fail(f"No intraday_trades rows for symbol '{symbol}' in DB")
        db.close()
    except Exception as e:
        fail(f"DB check failed: {e}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("  SUMMARY")
    print(f"{'═'*70}")

    td_count = 0
    if data2:
        td_count = len(data2.get("tradeHistory") or [])

    ob_today = len(data6.get("bestLimitsHistory") or []) if data6 else 0
    ob_hist  = len(data7.get("bestLimitsHistory") or []) if data7 else 0

    print(f"  Trade ticks (today):            {td_count:>6,} rows   ← Trade/GetTradeHistory")
    print(f"  OB deltas (today):              {ob_today:>6,} rows   ← BestLimits/{ins_code}/{today}")
    print(f"  OB deltas (yesterday):          {ob_hist:>6,} rows   ← BestLimits/{ins_code}/{yesterday}")
    print()

    if td_count > 0:
        print("  ✓ Trade/GetTradeHistory/{insCode}/{YYYYMMDD}/false  ← این endpoint کار می‌کند")
        print("    این همان 'ریز معاملات' صفحه نماد TSETMC است.")
        print("    برای دریافت داده لحظه‌ای امروز باید با poll هر N ثانیه صدا زده شود.")
    else:
        print("  ⚠ داده تیک امروز دریافت نشد (بازار بسته یا خارج از ساعت کاری)")

    if ob_today > 0:
        print()
        print("  ✓ BestLimits/{insCode}/{YYYYMMDD}  ← تاریخچه OB روزانه (delta stream)")
        print("    برای reconstruct دفتر سفارش لحظه‌به‌لحظه استفاده می‌شود.")
    print()


# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="TSETMC API endpoint analyzer")
    ap.add_argument("--symbol",   default="کیان",
                    help="نماد صندوق (پیش‌فرض: کیان)")
    ap.add_argument("--ins-code", default=None,
                    help="insCode مستقیم (اختیاری، جایگزین --symbol)")
    args = ap.parse_args()

    ins_code = args.ins_code
    symbol   = args.symbol

    if not ins_code:
        # look up from DB first
        try:
            db = sqlite3.connect(ROOT / "data" / "arbitrage.db")
            row = db.execute(
                "SELECT ins_code FROM daily_history WHERE symbol=? AND ins_code!='' LIMIT 1",
                (symbol,)
            ).fetchone()
            db.close()
            if row:
                ins_code = row[0]
                print(f"  Found ins_code={ins_code} for {symbol} in DB")
        except Exception:
            pass

    if not ins_code:
        # fallback: search TSETMC
        f = TSETMCFetcher()
        results = f.search_instrument(symbol)
        if results:
            ins_code = results[0]["ins_code"]
            print(f"  Resolved {symbol} → ins_code={ins_code} via search")
        else:
            print(f"  ERROR: could not resolve ins_code for '{symbol}'")
            sys.exit(1)

    analyze(ins_code, symbol)


if __name__ == "__main__":
    main()
