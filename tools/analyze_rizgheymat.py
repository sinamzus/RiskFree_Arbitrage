#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
آنالیز endpointهای TSETMC برای پیدا کردن جدول «ریز قیمت» درون‌روز امروز
=====================================================================
این اسکریپت را از داخل ایران (جایی که cdn.tsetmc.com باز است) اجرا کن.
ده‌ها endpoint کاندید را صدا می‌زند و برای هرکدام گزارش می‌دهد:
  • کد وضعیت HTTP
  • نوع محتوا (JSON / متن / HTML)
  • تعداد ردیف داده
  • آخرین زمان موجود (hEven) — یعنی آیا دیتای «همین الان» را دارد؟
  • نمونه‌ای از چند ردیف اول و آخر

خروجی هم در ترمینال چاپ می‌شود و هم در فایل
`tsetmc_analysis_output.txt` ذخیره می‌شود.

اجرا:
    python tools/analyze_rizgheymat.py
    python tools/analyze_rizgheymat.py 3846143218462419
    python tools/analyze_rizgheymat.py 3846143218462419 20260606

سپس کل محتوای فایل tsetmc_analysis_output.txt را برایم بفرست.
"""

import sys
import io
import json
from datetime import datetime

try:
    import requests
except ImportError:
    print("نیاز به نصب requests:  pip install requests")
    sys.exit(1)

# ── ورودی‌ها ───────────────────────────────────────────────────────────────
INS = sys.argv[1] if len(sys.argv) > 1 else "3846143218462419"
TODAY = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")

# خروجی هم‌زمان در ترمینال و فایل
_buf = io.StringIO()
def out(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    _buf.write(line + "\n")

# ── هدرها (شبیه مرورگر) ─────────────────────────────────────────────────────
HDRS_API = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Referer": "https://www.tsetmc.com/",
    "Origin": "https://www.tsetmc.com",
}
HDRS_CLASSIC = {
    "User-Agent": HDRS_API["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Referer": "http://www.tsetmc.com/",
}

# ── لیست endpointهای کاندید ─────────────────────────────────────────────────
# (برچسب، URL، نوع هدر)
CANDIDATES = [
    # --- API جدید cdn.tsetmc.com/api ---
    ("ClosingPriceInfo (لحظه‌ای)",
     f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceInfo/{INS}", "api"),
    ("ClosingPriceHistory/{today}",
     f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceHistory/{INS}/{TODAY}", "api"),
    ("ClosingPriceHistory (بدون تاریخ)",
     f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceHistory/{INS}", "api"),
    ("ClosingPriceDaily/{today}",
     f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDaily/{INS}/{TODAY}", "api"),
    ("Trade/GetTrade (تیک‌های امروز)",
     f"https://cdn.tsetmc.com/api/Trade/GetTrade/{INS}", "api"),
    ("Trade/GetTradeIntraday",
     f"https://cdn.tsetmc.com/api/Trade/GetTradeIntraday/{INS}", "api"),
    ("Trade/GetTradeHistory/{today}/false",
     f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{INS}/{TODAY}/false", "api"),
    ("Trade/GetTradeHistory/{today}/true",
     f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{INS}/{TODAY}/true", "api"),
    ("BestLimits (لحظه‌ای)",
     f"https://cdn.tsetmc.com/api/BestLimits/{INS}", "api"),
    ("ClientType/GetClientType (لحظه‌ای)",
     f"https://cdn.tsetmc.com/api/ClientType/GetClientType/{INS}", "api"),
    ("ClientType/GetClientTypeHistory/{today}",
     f"https://cdn.tsetmc.com/api/ClientType/GetClientTypeHistory/{INS}/{TODAY}", "api"),
    ("MarketData/GetStaticThreshold/{today}",
     f"https://cdn.tsetmc.com/api/MarketData/GetStaticThreshold/{INS}/{TODAY}", "api"),
    ("InstrumentInfo",
     f"https://cdn.tsetmc.com/api/Instrument/GetInstrumentInfo/{INS}", "api"),

    # --- endpointهای کلاسیک tsev2 (سایت قدیمی، اغلب هنوز کار می‌کنند) ---
    ("کلاسیک: InstTradeHistory (ریز معاملات)",
     f"http://www.tsetmc.com/tsev2/data/InstTradeHistory.aspx?i={INS}&Top=999999&A=0", "classic"),
    ("کلاسیک: instinfodata",
     f"http://www.tsetmc.com/tsev2/data/instinfodata.aspx?i={INS}&c=27%20", "classic"),
    ("کلاسیک: Loader ریز قیمت (ParTree=151311)",
     f"http://www.tsetmc.com/Loader.aspx?ParTree=151311&i={INS}", "classic"),
    ("کلاسیک: IntraDayPrice chart data",
     f"http://www.tsetmc.com/tsev2/chart/data/IntraDayPrice.aspx?i={INS}", "classic"),
    ("کلاسیک: TradeDetail (ریز قیمت)",
     f"http://cdn.tsetmc.com/api/Trade/GetTradeHistory/{INS}/{TODAY}/false", "api"),
]


def find_times(obj):
    """تمام مقادیر hEven / time را در یک ساختار JSON جمع می‌کند."""
    times = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in ("heven", "time", "htime") and isinstance(v, (int, float)):
                    times.append(int(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    return times


def fmt_hhmmss(t):
    """123045 → 12:30:45"""
    t = int(t)
    return f"{t//10000:02d}:{(t//100)%100:02d}:{t%100:02d}"


def preview_json(data):
    if isinstance(data, dict):
        out("    نوع: dict | کلیدها:", list(data.keys()))
        for k, v in data.items():
            if isinstance(v, list):
                out(f"    لیست '{k}': {len(v)} ردیف")
                if v:
                    out("      ردیف اول:", json.dumps(v[0], ensure_ascii=False)[:300])
                    out("      ردیف آخر:", json.dumps(v[-1], ensure_ascii=False)[:300])
            elif isinstance(v, dict):
                out(f"    dict '{k}':", json.dumps(v, ensure_ascii=False)[:300])
    elif isinstance(data, list):
        out(f"    نوع: list | {len(data)} ردیف")
        if data:
            out("      ردیف اول:", json.dumps(data[0], ensure_ascii=False)[:300])
            out("      ردیف آخر:", json.dumps(data[-1], ensure_ascii=False)[:300])

    times = find_times(data)
    if times:
        out(f"    ⏱ تعداد رکورد زمان‌دار: {len(times)} | "
            f"اولین: {fmt_hhmmss(min(times))} | آخرین: {fmt_hhmmss(max(times))}")


def preview_text(text):
    text = text.strip()
    rows = text.split(";")
    out(f"    نوع: متن | طول: {len(text)} | تعداد بخش (;): {len(rows)}")
    out("    شروع:", text[:400].replace("\n", " "))
    if len(text) > 400:
        out("    پایان:", text[-200:].replace("\n", " "))
    # تلاش برای استخراج زمان از ردیف‌های ساعت‌دار مثل  HH:MM:SS@...
    import re
    found = re.findall(r"\b([0-2]?\d:[0-5]\d(?::[0-5]\d)?)\b", text)
    if found:
        out(f"    ⏱ زمان‌های پیداشده: {len(found)} | اولین: {found[0]} | آخرین: {found[-1]}")


def probe(label, url, kind):
    out("\n" + "═" * 70)
    out("►", label)
    out("  URL:", url)
    hdrs = HDRS_API if kind == "api" else HDRS_CLASSIC
    try:
        r = requests.get(url, headers=hdrs, timeout=20)
    except Exception as e:
        out("  ✗ خطای شبکه:", repr(e))
        return
    ctype = r.headers.get("Content-Type", "")
    out(f"  وضعیت: {r.status_code} | نوع محتوا: {ctype} | طول: {len(r.text)}")
    if r.status_code != 200:
        out("  بدنه:", r.text[:200].replace("\n", " "))
        return
    body = r.text.strip()
    # تلاش برای JSON
    if body[:1] in ("{", "["):
        try:
            preview_json(json.loads(body))
            return
        except Exception as e:
            out("  (تجزیه JSON ناموفق:", repr(e), ")")
    # در غیر این صورت متن
    if "<html" in body[:200].lower() or "<!doctype" in body[:200].lower():
        out("    نوع: HTML (احتمالاً صفحه SPA — داده مفید ندارد)")
        out("    شروع:", body[:200].replace("\n", " "))
    else:
        preview_text(body)


def main():
    out("╔" + "═" * 68 + "╗")
    out("  آنالیز TSETMC — جستجوی منبع جدول «ریز قیمت» درون‌روز")
    out(f"  insCode = {INS}")
    out(f"  تاریخ امروز (YYYYMMDD) = {TODAY}")
    out(f"  زمان اجرا = {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out("╚" + "═" * 68 + "╝")

    for label, url, kind in CANDIDATES:
        probe(label, url, kind)

    out("\n" + "═" * 70)
    out("پایان. لطفاً کل محتوای فایل tsetmc_analysis_output.txt را برای من بفرست.")

    # ذخیره خروجی
    try:
        with open("tsetmc_analysis_output.txt", "w", encoding="utf-8") as f:
            f.write(_buf.getvalue())
        print("\n✅ خروجی در فایل tsetmc_analysis_output.txt ذخیره شد.")
    except Exception as e:
        print("خطا در ذخیره فایل:", e)


if __name__ == "__main__":
    main()
