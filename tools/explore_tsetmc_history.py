"""explore_tsetmc_history.py — کاوش ساختار داده صفحات سابقه TSETMC

این اسکریپت روی ماشین شما (نه cloud) اجرا می‌شود و ساختار کامل داده‌ای
که TSETMC در تب سابقه ارائه می‌دهد را بررسی و خروجی را ذخیره می‌کند.

اجرا:
  python tools/explore_tsetmc_history.py
  python tools/explore_tsetmc_history.py --ins 3846143218462419 --date 20260525
  python tools/explore_tsetmc_history.py --section ob   # فقط اردربوک

خروجی:
  فایل tsetmc_explore_<insCode>_<date>.json در پوشه جاری ذخیره می‌شود.
  محتوای آن را برای Claude کپی کنید.
"""

from __future__ import annotations
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# اگر به عنوان ابزار مستقل اجرا می‌شود، ROOT را تنظیم کن
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

# ── تنظیمات ─────────────────────────────────────────────────────────────────
TSETMC_CDN = "https://cdn.tsetmc.com/api"
TSETMC_WEB = "https://www.tsetmc.ir"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.tsetmc.ir/",
    "Origin":          "https://www.tsetmc.ir",
}

DEFAULT_INS  = "3846143218462419"
DEFAULT_DATE = "20260525"

S = requests.Session()
S.headers.update(HEADERS)

# ── نتایج برای ذخیره ─────────────────────────────────────────────────────────
REPORT: dict = {"meta": {}, "endpoints": {}, "analysis": {}}


def sep(title: str, char="═", width=72):
    n = max(0, width - len(title) - 4)
    print(f"\n{char*2} {title} {char*n}")


def fetch(url: str, label: str, extra_headers: dict | None = None) -> tuple[int, str, dict | list | str | None]:
    """Fetch a URL, return (status, content_type, parsed_body)."""
    try:
        hdrs = {}
        if extra_headers:
            hdrs.update(extra_headers)
        r = S.get(url, headers=hdrs, timeout=20)
        status = r.status_code
        ct = r.headers.get("Content-Type", "")
        size = len(r.content)
        print(f"  ► {label}")
        print(f"    {url}")
        print(f"    status={status}  size={size:,}B  type={ct[:60]}")

        if status != 200:
            print(f"    body: {r.text[:150]}")
            return status, ct, None

        if "json" in ct or r.text.strip().startswith(("{", "[")):
            try:
                body = r.json()
                return status, ct, body
            except Exception:
                pass
        return status, ct, r.text

    except Exception as e:
        print(f"  ✗ {label}: {e}")
        return 0, "", None


def show_keys(obj, depth=0, max_depth=3):
    indent = "    " * depth
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                print(f"{indent}{k}: list[{len(v)}]")
                if depth < max_depth and v:
                    show_keys(v[0], depth + 1)
            elif isinstance(v, dict):
                print(f"{indent}{k}: dict{{{list(v.keys())[:6]}}}")
                if depth < max_depth:
                    show_keys(v, depth + 1)
            else:
                print(f"{indent}{k}: {repr(v)[:100]}")
    elif isinstance(obj, list) and obj:
        print(f"{indent}[0]: {type(obj[0]).__name__}")
        show_keys(obj[0], depth + 1, max_depth)


# ════════════════════════════════════════════════════════════════════════════
#  بخش ۱ — CDN API (همه endpoint‌های شناخته‌شده)
# ════════════════════════════════════════════════════════════════════════════

def explore_cdn(ins: str, date: str):
    sep("CDN API — بررسی endpoint‌ها")

    endpoints = [
        # اطلاعات نماد
        ("instrument_info",     f"Instrument/GetInstrumentInfo/{ins}"),
        ("closing_price_info",  f"ClosingPrice/GetClosingPriceInfo/{ins}"),
        # اردربوک — زنده
        ("ob_live",             f"BestLimits/{ins}"),
        # اردربوک — تاریخی (ممکن است نیاز به Referer داشته باشد)
        ("ob_history",          f"BestLimits/{ins}/{date}"),
        # معاملات روزانه
        ("trades_history",      f"Trade/GetTradeHistory/{ins}/{date}/false"),
        ("trades_with_cancel",  f"Trade/GetTradeHistory/{ins}/{date}/true"),
        # NAV صندوق
        ("etf_info",            f"MutualFund/GetETFByInsCode/{ins}"),
        ("etf_nav_history",     f"MutualFund/GetETFHistory/{ins}"),
        # قیمت تاریخی روزانه
        ("daily_history",       f"ClosingPrice/GetClosingPriceHistory/{ins}"),
        ("daily_on_date",       f"ClosingPrice/GetClosingPriceHistory/{ins}/{date}"),
        # نوع معامله‌گر
        ("client_type",         f"ClientType/GetClientType/{ins}"),
        ("client_type_hist",    f"ClientType/GetClientTypeHistory/{ins}/{date}"),
        # صنعت
        ("group_instruments",   f"Instrument/GetInstrumentGroupInstruments/{ins}"),
    ]

    results = {}
    for key, path in endpoints:
        url = f"{TSETMC_CDN}/{path}"
        status, ct, body = fetch(url, key)
        REPORT["endpoints"][key] = {
            "url": url, "status": status, "has_data": body is not None,
        }
        if body is not None:
            results[key] = body
        time.sleep(0.25)

    return results


# ════════════════════════════════════════════════════════════════════════════
#  بخش ۲ — صفحات وب و بررسی HTML
# ════════════════════════════════════════════════════════════════════════════

def explore_web_pages(ins: str, date: str):
    sep("صفحات وب TSETMC")

    pages = [
        ("instinfo_page",  f"{TSETMC_WEB}/instInfo/{ins}"),
        ("history_index",  f"{TSETMC_WEB}/History/{ins}"),
        ("history_date",   f"{TSETMC_WEB}/History/{ins}/{date}"),
    ]

    for key, url in pages:
        status, ct, body = fetch(url, key)
        if not isinstance(body, str):
            continue

        html = body
        print(f"    → HTML length: {len(html):,}")

        # جستجوی JSON دیتا داخل HTML
        json_blobs = re.findall(r'<script[^>]*>\s*window\.(\w+)\s*=\s*(\{.*?\}|\[.*?\])\s*;?\s*</script>',
                                html, re.DOTALL)
        if json_blobs:
            print(f"    → window.X = ... found: {[b[0] for b in json_blobs]}")
            for varname, blob in json_blobs:
                try:
                    parsed = json.loads(blob)
                    print(f"      window.{varname} keys: {list(parsed.keys())[:10]}")
                    REPORT["analysis"][f"window_{varname}"] = parsed
                except Exception:
                    pass

        # API endpoint‌های جاسازی‌شده
        api_refs = re.findall(
            r'(?:url|endpoint|path|api)["\']?\s*[:=]\s*["\']([/][^"\'<> ]{4,})["\']',
            html, re.IGNORECASE
        )
        if api_refs:
            print(f"    → API refs embedded: {list(set(api_refs))[:10]}")

        # دیتای جاسازی‌شده به صورت JSON
        embedded = re.findall(r'data-[\w-]+=["\'](\{[^"\']{20,}\})["\']', html)
        if embedded:
            print(f"    → data-* attributes with JSON: {len(embedded)} found")

        # بررسی script src
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
        print(f"    → scripts: {[s[-60:] for s in scripts[:6]]}")

        REPORT["endpoints"][key] = {"url": url, "status": status,
                                     "html_len": len(html)}


# ════════════════════════════════════════════════════════════════════════════
#  بخش ۳ — تحلیل عمیق OB تاریخی
# ════════════════════════════════════════════════════════════════════════════

def deep_dive_ob(ins: str, date: str, cdn_results: dict):
    sep("اردربوک تاریخی — تحلیل کامل")

    data = cdn_results.get("ob_history")

    if not data:
        print("  داده OB تاریخی دریافت نشد — تلاش با Accept header متفاوت...")
        for accept in ["*/*", "application/json", "text/html,application/json"]:
            url = f"{TSETMC_CDN}/BestLimits/{ins}/{date}"
            status, ct, data = fetch(url, f"ob_history (Accept={accept})",
                                     extra_headers={"Accept": accept})
            if data:
                break
            time.sleep(0.3)

    if not data:
        print("  ✗ هیچ داده‌ای دریافت نشد")
        return

    print(f"\n  ── ساختار کلی ──")
    show_keys(data, depth=0)

    # پیدا کردن لیست اصلی
    rows = None
    list_key = None
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                rows = v
                list_key = k
                break
    elif isinstance(data, list):
        rows = data
        list_key = "(root)"

    if not rows:
        print("  → لیست خالی است")
        REPORT["analysis"]["ob_history"] = {"empty": True, "raw": data}
        return

    print(f"\n  ── کلید اصلی: '{list_key}' — {len(rows)} ردیف ──")

    if rows:
        print(f"\n  ── ستون‌های row[0] ──")
        row0 = rows[0]
        for k, v in row0.items():
            print(f"    {k:30s} = {v!r}")

    # توزیع زمانی
    time_field = next((f for f in ["hEven", "time", "dEven"] if f in (rows[0] if rows else {})), None)
    level_field = next((f for f in ["number", "num", "level", "row", "idn"] if f in (rows[0] if rows else {})), None)
    print(f"\n  ── فیلدهای کلیدی ──")
    print(f"    time_field  = {time_field!r}")
    print(f"    level_field = {level_field!r}")

    if time_field:
        times = sorted(set(r.get(time_field, 0) for r in rows))
        print(f"\n  ── توزیع زمانی ──")
        print(f"    timestamp‌های منحصربه‌فرد: {len(times)}")
        print(f"    اولین: {times[0]}  آخرین: {times[-1]}")
        if len(times) <= 20:
            print(f"    همه: {times}")

        # یک timestamp کامل
        t0 = times[0]
        t0_rows = [r for r in rows if r.get(time_field) == t0]
        print(f"\n  ── همه ردیف‌های timestamp={t0} ({len(t0_rows)} ردیف) ──")
        for r in t0_rows:
            print(f"    {json.dumps(r, ensure_ascii=False)}")

    if level_field:
        levels = sorted(set(r.get(level_field, 0) for r in rows))
        print(f"\n  ── سطوح (levels): {levels} ──")

    # نمونه ۱۰ ردیف اول
    print(f"\n  ── ۱۰ ردیف اول ──")
    for r in rows[:10]:
        print(f"    {json.dumps(r, ensure_ascii=False)}")

    REPORT["analysis"]["ob_history"] = {
        "list_key": list_key,
        "total_rows": len(rows),
        "time_field": time_field,
        "level_field": level_field,
        "sample_5": rows[:5],
        "fields": list(rows[0].keys()) if rows else [],
    }


# ════════════════════════════════════════════════════════════════════════════
#  بخش ۴ — مقایسه زنده vs تاریخی
# ════════════════════════════════════════════════════════════════════════════

def compare_ob_formats(cdn_results: dict):
    sep("مقایسه ساختار OB زنده vs تاریخی")

    live = cdn_results.get("ob_live")
    hist = cdn_results.get("ob_history")

    print("\n  ── OB زنده ──")
    if isinstance(live, dict):
        show_keys(live, depth=0, max_depth=2)
        for k, v in live.items():
            if isinstance(v, list) and v:
                print(f"\n    نمونه {k}[0]: {json.dumps(v[0], ensure_ascii=False)}")
    else:
        print("  ندارد")

    print("\n  ── OB تاریخی ──")
    if isinstance(hist, dict):
        show_keys(hist, depth=0, max_depth=2)
    else:
        print("  ندارد")

    REPORT["analysis"]["ob_live_sample"] = live
    REPORT["analysis"]["ob_hist_sample"] = hist


# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ins",  default=DEFAULT_INS,  metavar="INS_CODE",
                    help=f"ins_code نماد (پیش‌فرض: {DEFAULT_INS})")
    ap.add_argument("--date", default=DEFAULT_DATE, metavar="YYYYMMDD",
                    help=f"تاریخ میلادی (پیش‌فرض: {DEFAULT_DATE})")
    ap.add_argument("--section",
                    choices=["cdn", "web", "ob", "compare", "all"],
                    default="all",
                    help="کدام بخش اجرا شود (پیش‌فرض: all)")
    ap.add_argument("--out", metavar="FILE",
                    help="مسیر فایل خروجی JSON (پیش‌فرض: خودکار)")
    args = ap.parse_args()

    ins  = args.ins.strip()
    date = args.date.strip()

    REPORT["meta"] = {
        "ins_code": ins,
        "date": date,
        "run_at": datetime.now().isoformat(),
    }

    print("=" * 72)
    print(f"  TSETMC History Explorer")
    print(f"  ins_code : {ins}")
    print(f"  date     : {date}")
    print(f"  section  : {args.section}")
    print("=" * 72)

    cdn_results = {}
    if args.section in ("cdn", "all"):
        cdn_results = explore_cdn(ins, date)

    if args.section in ("web", "all"):
        explore_web_pages(ins, date)

    if args.section in ("ob", "all"):
        if "ob_history" not in cdn_results:
            # Fetch OB history standalone
            url = f"{TSETMC_CDN}/BestLimits/{ins}/{date}"
            _, _, body = fetch(url, "ob_history (standalone)")
            if body is not None:
                cdn_results["ob_history"] = body
        deep_dive_ob(ins, date, cdn_results)

    if args.section in ("compare", "all"):
        compare_ob_formats(cdn_results)

    # ذخیره JSON
    out_path = args.out or f"tsetmc_explore_{ins}_{date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(REPORT, f, ensure_ascii=False, indent=2, default=str)

    sep("خلاصه", char="─")
    print(f"\n  خروجی ذخیره شد: {out_path}")
    print(f"  این فایل را برای Claude ارسال کنید.\n")

    # نمایش status همه endpoint‌ها
    print("  Status endpoint‌ها:")
    for key, info in REPORT["endpoints"].items():
        ok = "✓" if info.get("has_data") or info.get("status") == 200 else "✗"
        print(f"    {ok} {key:30s}  {info.get('status','—')}  {info.get('url','')[-50:]}")


if __name__ == "__main__":
    main()
