"""fetch_ob_ticks.py — جمع‌آوری tick-by-tick اردربوک برای همه صندوق‌ها

داده خام (تمام delta events) از API TSETMC را دانلود و در جدول ob_ticks ذخیره می‌کند.
برای backtest دقیق: هر ردیف = یک تغییر در یک سطح اردربوک.
برای بازسازی کتاب کامل در زمان T: مرتب‌سازی بر اساس ref_id و replay کردن.

اجرا:
  # ۶ ماه گذشته (اجرای اول)
  python tools/fetch_ob_ticks.py --days 180

  # چک کردن تا کجا تاریخ موجود است
  python tools/fetch_ob_ticks.py --probe

  # یک نماد خاص
  python tools/fetch_ob_ticks.py --days 60 --symbol کمند

  # وضعیت DB
  python tools/fetch_ob_ticks.py --status
"""
from __future__ import annotations
import argparse
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import FIXED_INCOME_ETFS, TSETMC_CDN, REQUEST_HEADERS
from database import Database
import requests

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

S = requests.Session()
S.headers.update(REQUEST_HEADERS)
S.headers.update({"Referer": "https://www.tsetmc.ir/",
                   "Origin":  "https://www.tsetmc.ir"})

DB = Database()


# ── helpers ──────────────────────────────────────────────────────────────────

def trading_dates(n: int, end: datetime | None = None) -> list[int]:
    """آخرین n روز معاملاتی بورس تهران (شنبه تا چهارشنبه)."""
    TSE_WEEKEND = {3, 4}   # Thursday=3, Friday=4
    dates, d = [], (end or datetime.now())
    while len(dates) < n:
        if d.weekday() not in TSE_WEEKEND:
            dates.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return dates


def fetch_raw(ins_code: str, date_int: int) -> list[dict]:
    """دریافت تمام ردیف‌های خام bestLimitsHistory برای یک نماد و تاریخ."""
    url = f"{TSETMC_CDN}/BestLimits/{ins_code}/{date_int}"
    try:
        r = S.get(url, timeout=25)
        if r.status_code != 200:
            return []
        return r.json().get("bestLimitsHistory") or []
    except Exception as e:
        logger.debug("fetch_raw %s %s: %s", ins_code, date_int, e)
        return []


def hhmm(t: int) -> str:
    s = str(t).zfill(6)
    return f"{s[:2]}:{s[2:4]}"


# ── probe: تا کجا تاریخ موجود است؟ ─────────────────────────────────────────

def probe_history(ins_code: str = "3846143218462419"):
    """بررسی می‌کند که API چقدر به تاریخ گذشته دسترسی دارد."""
    print()
    print("═" * 60)
    print("  بررسی عمق تاریخی API — چقدر به گذشته می‌رود؟")
    print("═" * 60)
    TSE_WEEKEND = {3, 4}
    today = datetime.now()
    for days_back in [5, 10, 20, 30, 60, 90, 120, 180, 250, 365]:
        d = today - timedelta(days=days_back)
        while d.weekday() in TSE_WEEKEND:
            d -= timedelta(days=1)
        date_int = int(d.strftime("%Y%m%d"))
        rows = fetch_raw(ins_code, date_int)
        ok = "✓" if rows else "✗"
        print(f"  {ok}  {days_back:>3} روز پیش ({date_int})  →  {len(rows):>5} ردیف")
        time.sleep(0.4)
    print()


# ── fetch & store ────────────────────────────────────────────────────────────

def fetch_all(dates: list[int], symbol_filter: str | None,
              force: bool, dry_run: bool = False):
    """جمع‌آوری tick-by-tick اردربوک برای همه صندوق‌ها و تاریخ‌های داده‌شده."""
    funds = [f for f in FIXED_INCOME_ETFS
             if (not symbol_filter or f["symbol"] == symbol_filter)
             and f.get("ins_code", "").strip()]

    total_new = 0
    total_err = 0

    print()
    print("═" * 72)
    print(f"  جمع‌آوری OB tick-by-tick — {len(funds)} صندوق × {len(dates)} روز")
    print(f"  تاریخ‌ها: {dates[0]} → {dates[-1]}  (مجموع: {len(dates)})")
    if dry_run:
        print("  *** DRY RUN — ذخیره نمی‌شود ***")
    print("═" * 72)

    for i, fund in enumerate(funds, 1):
        sym      = fund["symbol"]
        ins_code = fund["ins_code"].strip()

        # تاریخ‌هایی که قبلاً دارند
        have = set(DB.get_ob_tick_dates(sym)) if not force else set()

        fund_new = 0
        fund_err = 0
        date_results = []

        for date_int in dates:
            if date_int in have:
                date_results.append(f"{date_int}(skip)")
                continue

            rows = fetch_raw(ins_code, date_int)

            if not rows:
                fund_err += 1
                total_err += 1
                date_results.append(f"{date_int}(–)")
                time.sleep(0.2)
                continue

            if not dry_run:
                saved = DB.save_ob_ticks(sym, ins_code, date_int, rows)
            else:
                saved = len(rows)

            fund_new  += saved
            total_new += saved

            # آمار این تاریخ
            ref_ids = sorted(set(r["refID"] for r in rows))
            hevens  = sorted(set(r["hEven"] for r in rows))
            t_first = hhmm(hevens[0])  if hevens  else "—"
            t_last  = hhmm(hevens[-1]) if hevens  else "—"
            date_results.append(
                f"{date_int}({len(rows)} rows/{len(ref_ids)} events, {t_first}–{t_last})"
            )
            time.sleep(0.3)

        # خلاصه هر صندوق
        print(f"  [{i:2d}/{len(funds)}] {sym:12s}  +{fund_new:>7,} ردیف  خطا:{fund_err}")
        if len(date_results) <= 6:
            for dr in date_results:
                print(f"          {dr}")
        else:
            print(f"          {date_results[0]} … {date_results[-1]}")

    print()
    print(f"  ✓ مجموع ردیف جدید: {total_new:,}")
    print(f"  ✗ تاریخ‌های بدون داده: {total_err}")
    print()


# ── status ───────────────────────────────────────────────────────────────────

def show_status(symbol_filter: str | None):
    print()
    print("═" * 72)
    print("  وضعیت ob_ticks در DB")
    print("═" * 72)
    print(f"  {'نماد':12s}  {'تاریخ‌های موجود (آخرین ۳)':40s}  مجموع ردیف")
    print("  " + "─" * 65)

    grand_total = 0
    for fund in FIXED_INCOME_ETFS:
        sym = fund["symbol"]
        if symbol_filter and sym != symbol_filter:
            continue
        dates = DB.get_ob_tick_dates(sym)
        if not dates:
            continue
        # آخرین ۳ تاریخ
        last3 = dates[-3:]
        counts = []
        total = 0
        for d in last3:
            rows = DB.get_ob_ticks(sym, d)
            counts.append(f"{d}({len(rows)})")
            total += len(rows)
        grand_total += total
        dates_str = "  ".join(counts)
        print(f"  {sym:12s}  {dates_str:40s}  {total:,}")

    print()
    print(f"  مجموع کل ردیف (آخرین ۳ تاریخ): {grand_total:,}")
    print()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--days",   type=int, default=30,
                    help="چند روز معاملاتی گذشته (پیش‌فرض: ۳۰)")
    ap.add_argument("--date",   metavar="YYYYMMDD",
                    help="تاریخ پایان بازه (پیش‌فرض: امروز)")
    ap.add_argument("--symbol", help="فقط یک نماد مشخص")
    ap.add_argument("--force",  action="store_true",
                    help="حتی اگر قبلاً ذخیره شده، دوباره دانلود کن")
    ap.add_argument("--probe",  action="store_true",
                    help="بررسی عمق تاریخی API (چقدر به گذشته می‌رود)")
    ap.add_argument("--dry-run", action="store_true",
                    help="دانلود کن ولی ذخیره نکن (فقط آمار نشان بده)")
    ap.add_argument("--status", action="store_true",
                    help="فقط وضعیت DB را نشان بده")
    args = ap.parse_args()

    if args.status:
        show_status(args.symbol)
        return

    if args.probe:
        probe_history()
        return

    if args.date:
        try:
            end = datetime.strptime(args.date, "%Y%m%d")
        except ValueError:
            print("خطا: فرمت تاریخ باید YYYYMMDD باشد")
            sys.exit(1)
    else:
        end = None

    dates = trading_dates(args.days, end=end)

    fetch_all(dates, args.symbol, args.force, dry_run=args.dry_run)
    show_status(args.symbol)


if __name__ == "__main__":
    main()
