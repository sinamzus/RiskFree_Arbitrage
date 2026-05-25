"""fetch_intraday.py — دریافت داده معاملات ثانیه‌به‌ثانیه درون‌روزی

داده را از صفحه سابقه معاملات tsetmc.com می‌گیرد:
  Trade/GetTradeHistory/{insCode}/{YYYYMMDD}/false

اجرا:
  # امروز — همه ۳۰ صندوق
  python tools/fetch_intraday.py

  # تاریخ مشخص (شمسی یا میلادی)
  python tools/fetch_intraday.py --date 20260524

  # فقط یک صندوق
  python tools/fetch_intraday.py --symbol کمند

  # بازه چند روزه
  python tools/fetch_intraday.py --days 5

  # گزارش وضعیت DB بدون دریافت
  python tools/fetch_intraday.py --status
"""

from __future__ import annotations
import argparse
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

# ── path setup ─────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import FIXED_INCOME_ETFS, TSETMC_CDN, REQUEST_HEADERS
from database import Database
import requests

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

S = requests.Session()
S.headers.update(REQUEST_HEADERS)
S.headers["Referer"] = "https://www.tsetmc.com/"

DB = Database()


# ── helpers ─────────────────────────────────────────────────────────────────

def trading_dates(n: int, end: datetime | None = None) -> list[int]:
    """Return last *n* TSE trading days as YYYYMMDD ints (TSE: Sat–Wed)."""
    TSE_WEEKEND = {3, 4}   # Thursday=3, Friday=4 in Python weekday()
    dates, d = [], (end or datetime.now())
    while len(dates) < n:
        if d.weekday() not in TSE_WEEKEND:
            dates.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return dates


def fetch_ticks(ins_code: str, date_int: int) -> list[dict]:
    """Fetch all trades for *ins_code* on *date_int* from TSETMC سابقه API."""
    url = f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins_code}/{date_int}/false"
    try:
        r = S.get(url, timeout=20)
        if r.status_code != 200:
            return []
        raw = r.json().get("tradeHistory") or []
        result = []
        for t in raw:
            result.append({
                "seq":      t.get("nTran", 0),
                "time":     t.get("hEven", 0),     # HHMMSS int
                "price":    t.get("pTran", 0),
                "volume":   t.get("qTitTran", 0),
                "canceled": 1 if t.get("canceled") else 0,
            })
        result.sort(key=lambda x: x["seq"])
        return result
    except Exception as e:
        logger.warning("fetch_ticks error %s %s: %s", ins_code, date_int, e)
        return []


def hhmm(t: int) -> str:
    """91530 → '09:15:30'"""
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


# ── status report ────────────────────────────────────────────────────────────

def show_status(symbol_filter: str | None):
    print()
    print("=" * 72)
    print("  وضعیت داده درون‌روزی در DB")
    print("=" * 72)
    print(f"  {'نماد':12s}  {'تاریخ‌های موجود':30s}  تیک")
    print("  " + "─" * 65)

    total_ticks = 0
    for fund in FIXED_INCOME_ETFS:
        sym = fund["symbol"]
        if symbol_filter and sym != symbol_filter:
            continue
        dates = DB.get_intraday_dates(sym)
        ticks_per_date = []
        for d in dates[-5:]:   # last 5
            ticks = DB.get_intraday_trades(sym, d)
            ticks_per_date.append(f"{d}({len(ticks)})")
            total_ticks += len(ticks)
        dates_str = "  ".join(ticks_per_date) if ticks_per_date else "ندارد"
        print(f"  {sym:12s}  {dates_str}")

    print()
    print(f"  مجموع تیک در DB: {total_ticks:,}")
    print()


# ── main fetch ───────────────────────────────────────────────────────────────

def fetch(dates: list[int], symbol_filter: str | None, force: bool):
    print()
    print("=" * 72)
    print(f"  دریافت داده معاملات درون‌روزی — {len(dates)} روز")
    print(f"  تاریخ‌ها: {dates}")
    print("=" * 72)

    grand_new = 0

    for i, fund in enumerate(FIXED_INCOME_ETFS, 1):
        sym      = fund["symbol"]
        ins_code = fund.get("ins_code", "").strip()

        if symbol_filter and sym != symbol_filter:
            continue
        if not ins_code:
            print(f"  [{i:2d}] {sym:12s}  ← بدون ins_code، رد شد")
            continue

        fund_new = 0
        have_dates = set(DB.get_intraday_dates(sym)) if not force else set()

        for date_int in dates:
            if date_int in have_dates:
                print(f"  [{i:2d}] {sym:12s}  {date_int}  ← قبلاً دریافت شده، رد شد")
                continue

            ticks = fetch_ticks(ins_code, date_int)

            if not ticks:
                print(f"  [{i:2d}] {sym:12s}  {date_int}  ← بدون داده (بازار بسته یا خطا)")
                time.sleep(0.2)
                continue

            # Filter cancelled
            valid = [t for t in ticks if not t["canceled"]]
            new_rows = DB.save_intraday_trades(sym, ins_code, date_int, valid)
            fund_new  += new_rows
            grand_new += new_rows

            # Quick summary
            prices = [t["price"] for t in valid if t["price"] > 0]
            p_min  = min(prices) if prices else 0
            p_max  = max(prices) if prices else 0
            t_first = hhmm(valid[0]["time"]) if valid else "—"
            t_last  = hhmm(valid[-1]["time"]) if valid else "—"
            print(
                f"  [{i:2d}] {sym:12s}  {date_int}  "
                f"{len(valid):>5,} تیک  "
                f"قیمت {p_min:>10,.0f}–{p_max:>10,.0f}  "
                f"زمان {t_first}–{t_last}  "
                f"({new_rows} جدید)"
            )
            time.sleep(0.25)

        if fund_new == 0 and symbol_filter is None:
            pass  # already printed per-date

    print()
    print(f"  ✓ مجموع تیک جدید ذخیره‌شده: {grand_new:,}")
    print()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="دریافت داده معاملات درون‌روزی (سابقه) برای همه صندوق‌ها"
    )
    ap.add_argument("--date",   help="تاریخ YYYYMMDD (پیش‌فرض: امروز)")
    ap.add_argument("--days",   type=int, default=1,
                    help="چند روز گذشته (پیش‌فرض: ۱ = فقط امروز)")
    ap.add_argument("--symbol", help="فقط یک نماد مشخص")
    ap.add_argument("--force",  action="store_true",
                    help="حتی اگر قبلاً دریافت شده، دوباره بگیر")
    ap.add_argument("--status", action="store_true",
                    help="فقط نمایش وضعیت DB، بدون دریافت جدید")
    args = ap.parse_args()

    if args.status:
        show_status(args.symbol)
        return

    if args.date:
        try:
            dt = datetime.strptime(args.date, "%Y%m%d")
        except ValueError:
            print(f"خطا: فرمت تاریخ باید YYYYMMDD باشد (مثلاً 20260524)")
            sys.exit(1)
        dates = trading_dates(args.days, end=dt)
    else:
        dates = trading_dates(args.days)

    fetch(dates, args.symbol, args.force)
    show_status(args.symbol)


if __name__ == "__main__":
    main()
