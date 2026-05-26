"""explore_ob_ticks.py — بررسی دقیق ساختار tick-by-tick اردربوک TSETMC

روی ماشین شما اجرا کنید:
  python tools/explore_ob_ticks.py --ins 3846143218462419 --date 20260525
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from config import TSETMC_CDN, REQUEST_HEADERS, REQUEST_TIMEOUT

S = requests.Session()
S.headers.update(REQUEST_HEADERS)
S.headers.update({"Referer": "https://www.tsetmc.ir/", "Origin": "https://www.tsetmc.ir"})

REPORT = {}

def get(url, label=""):
    try:
        r = S.get(url, timeout=REQUEST_TIMEOUT)
        print(f"  {label or url[-60:]}")
        print(f"    status={r.status_code}  size={len(r.content):,}B")
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return r.text
        print(f"    body: {r.text[:100]}")
        return None
    except Exception as e:
        print(f"  ✗ {e}")
        return None

def sep(t): print(f"\n{'═'*2} {t} {'═'*max(0,68-len(t))}")


def analyze_ob_raw(ins, date):
    sep("۱ — بررسی bestLimitsHistory (داده خام)")
    data = get(f"{TSETMC_CDN}/BestLimits/{ins}/{date}", "BestLimits history")
    if not data:
        return
    rows = data.get("bestLimitsHistory") or []
    print(f"\n  تعداد کل ردیف‌های خام: {len(rows)}")

    if not rows:
        return

    REPORT["ob_raw_total"] = len(rows)
    REPORT["ob_raw_fields"] = list(rows[0].keys())
    REPORT["ob_raw_sample10"] = rows[:10]

    # آمار refID
    ref_ids = sorted(set(r["refID"] for r in rows))
    REPORT["unique_ref_ids"] = len(ref_ids)
    print(f"  refID منحصربه‌فرد: {len(ref_ids)}")
    print(f"  refID اول: {ref_ids[0]}  آخر: {ref_ids[-1]}")
    print(f"  بازه refID: {ref_ids[-1]-ref_ids[0]:,}")

    # آمار hEven
    hevens = sorted(set(r["hEven"] for r in rows))
    REPORT["unique_hevens"] = len(hevens)
    print(f"\n  hEven منحصربه‌فرد: {len(hevens)}")
    print(f"  hEven اول: {hevens[0]}  آخر: {hevens[-1]}")

    # آمار سطح (number)
    from collections import Counter
    level_cnt = Counter(r["number"] for r in rows)
    print(f"\n  توزیع سطح (number): {dict(sorted(level_cnt.items()))}")

    # چند ردیف به ازای هر refID؟
    from collections import defaultdict
    by_ref = defaultdict(list)
    for r in rows:
        by_ref[r["refID"]].append(r)
    rows_per_ref = Counter(len(v) for v in by_ref.values())
    REPORT["rows_per_refid"] = dict(rows_per_ref)
    print(f"\n  ردیف به ازای هر refID: {dict(sorted(rows_per_ref.items()))}")

    # چند refID به ازای هر hEven؟
    by_heven = defaultdict(list)
    for r in rows:
        by_heven[r["hEven"]].append(r["refID"])
    ref_per_heven = Counter(len(set(v)) for v in by_heven.values())
    REPORT["refs_per_heven"] = dict(ref_per_heven)
    print(f"  refID به ازای هر hEven: {dict(sorted(ref_per_heven.items()))}")

    # نمونه ۲۰ ردیف اول مرتب‌شده بر اساس refID
    sorted_rows = sorted(rows, key=lambda r: r["refID"])
    print(f"\n  ── ۱۰ ردیف اول (مرتب بر اساس refID) ──")
    for r in sorted_rows[:10]:
        print(f"    {json.dumps(r, ensure_ascii=False)}")

    # بررسی آیا refID و hEven همبستگی کامل دارند؟
    ref_to_heven = {r["refID"]: r["hEven"] for r in rows}
    # آیا افزایش refID همیشه با افزایش hEven همراه است؟
    prev_h = -1
    monotone = True
    for ref in sorted(ref_to_heven.keys()):
        h = ref_to_heven[ref]
        if h < prev_h:
            monotone = False
            print(f"\n  ⚠ hEven کاهش یافت: ref={ref} hEven={h} < {prev_h}")
            break
        prev_h = h
    REPORT["heven_monotone_with_refid"] = monotone
    print(f"\n  hEven با refID یکنواخت افزایشی است: {monotone}")

    return by_ref, sorted_rows


def analyze_trades(ins, date):
    sep("۲ — تیک‌های معاملات (Trade/GetTradeHistory)")
    data = get(f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins}/{date}/false", "TradeHistory")
    if not data:
        return
    rows = data.get("tradeHistory") or []
    print(f"\n  تعداد تیک معامله: {len(rows)}")
    REPORT["trade_total"] = len(rows)

    if rows:
        print(f"  فیلدها: {list(rows[0].keys())}")
        print(f"\n  ۵ تیک اول:")
        for r in rows[:5]:
            print(f"    {json.dumps(r, ensure_ascii=False)}")

        # بررسی فیلد seq / nTran برای تطبیق با refID اردربوک
        REPORT["trade_fields"] = list(rows[0].keys())
        REPORT["trade_sample5"] = rows[:5]

        # آمار زمانی
        times = sorted(set(r.get("hEven", r.get("nTran", 0)) for r in rows))
        print(f"\n  بازه زمانی معاملات: {times[0]} → {times[-1]}")


def check_alternate_ob_endpoints(ins, date):
    sep("۳ — endpoint های جایگزین برای اردربوک کامل")

    alts = [
        # آیا endpoint خاصی برای دریافت tick-by-tick اردربوک وجود دارد؟
        f"BestLimits/{ins}/{date}",
        f"MarketWatch/GetBestLimitsHistory/{ins}/{date}",
        f"MarketData/GetBestLimitsHistory/{ins}/{date}",
        # آیا یک endpoint combined (معامله + اردربوک) وجود دارد؟
        f"Trade/GetTradeAndLimits/{ins}/{date}",
        f"MarketWatch/GetMarketDepth/{ins}/{date}",
        f"ClosingPrice/GetCPC/{ins}/{date}",
        # با تاریخ‌های قدیمی‌تر آیا کار می‌کند؟
        f"BestLimits/{ins}/{_old_date(date, 30)}",
        f"BestLimits/{ins}/{_old_date(date, 90)}",
        f"BestLimits/{ins}/{_old_date(date, 180)}",
        f"BestLimits/{ins}/{_old_date(date, 365)}",
    ]

    results = {}
    for path in alts:
        url = f"{TSETMC_CDN}/{path}"
        data = get(url, path[-55:])
        if data and isinstance(data, dict):
            keys = list(data.keys())
            print(f"    keys: {keys}")
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"    {k}: list[{len(v)}]" + (f" → {json.dumps(v[0], ensure_ascii=False)[:120]}" if v else ""))
        results[path] = {"status": data is not None}
        time.sleep(0.3)

    REPORT["alt_endpoints"] = results


def _old_date(date_str: str, days_back: int) -> str:
    from datetime import datetime, timedelta
    d = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=days_back)
    return d.strftime("%Y%m%d")


def check_max_history(ins):
    sep("۴ — بررسی حداکثر تاریخ در دسترس (چقدر به گذشته می‌رود؟)")
    from datetime import datetime, timedelta

    today = datetime.now()
    results = {}
    for days_back in [7, 30, 60, 90, 120, 180, 250, 365]:
        d = today - timedelta(days=days_back)
        # skip weekends (Thursday=3, Friday=4)
        while d.weekday() in (3, 4):
            d -= timedelta(days=1)
        date_int = d.strftime("%Y%m%d")
        url = f"{TSETMC_CDN}/BestLimits/{ins}/{date_int}"
        data = get(url, f"BestLimits {days_back} روز پیش ({date_int})")
        rows = data.get("bestLimitsHistory") or [] if isinstance(data, dict) else []
        results[date_int] = len(rows)
        print(f"    → {len(rows)} ردیف")
        time.sleep(0.4)

    REPORT["history_depth"] = results
    available = {k: v for k, v in results.items() if v > 0}
    print(f"\n  تاریخ‌های موجود: {list(available.keys())}")
    print(f"  قدیمی‌ترین موجود: {min(available.keys()) if available else 'ندارد'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ins",  default="3846143218462419")
    ap.add_argument("--date", default="20260525")
    ap.add_argument("--section", choices=["raw","trades","alt","depth","all"], default="all")
    args = ap.parse_args()

    print("=" * 72)
    print(f"  OB Tick Explorer  ins={args.ins}  date={args.date}")
    print("=" * 72)

    if args.section in ("raw","all"):
        analyze_ob_raw(args.ins, args.date)

    if args.section in ("trades","all"):
        analyze_trades(args.ins, args.date)

    if args.section in ("alt","all"):
        check_alternate_ob_endpoints(args.ins, args.date)

    if args.section in ("depth","all"):
        check_max_history(args.ins)

    out = f"tsetmc_ob_ticks_{args.ins}_{args.date}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(REPORT, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  ✓ ذخیره شد: {out}")
    print("  این فایل را برای Claude push کنید.\n")

if __name__ == "__main__":
    main()
