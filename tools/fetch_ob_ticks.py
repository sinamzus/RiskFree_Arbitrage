"""fetch_ob_ticks.py — جمع‌آوری موازی tick-by-tick اردربوک برای همه صندوق‌ها

داده خام (تمام delta events) از API TSETMC را دانلود و در جدول ob_ticks ذخیره می‌کند.

موازی‌سازی:
  - N worker thread موازی (پیش‌فرض: ۳)
  - rate limiter سراسری: حداکثر R درخواست در ثانیه (پیش‌فرض: ۳ req/s)
  - retry با exponential backoff روی خطاهای ۴۲۹/۵xx
  - jitter تصادفی بین درخواست‌ها برای جلوگیری از الگوی منظم

اجرا:
  python tools/fetch_ob_ticks.py --days 180                # ۱۸۰ روز، ۳ worker
  python tools/fetch_ob_ticks.py --days 180 --workers 5   # سریع‌تر (با احتیاط)
  python tools/fetch_ob_ticks.py --probe                   # تا کجا تاریخ موجود است؟
  python tools/fetch_ob_ticks.py --status                  # وضعیت DB
  python tools/fetch_ob_ticks.py --days 30 --dry-run      # بدون ذخیره
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Optional

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

DB = Database()


# ═══════════════════════════════════════════════════════════════════════════
#  Rate Limiter — جلوگیری از ban شدن IP
# ═══════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Token-bucket rate limiter — حداکثر max_rps درخواست در ثانیه به صورت سراسری."""

    def __init__(self, max_rps: float = 3.0):
        self._interval = 1.0 / max_rps   # فاصله کمینه بین درخواست‌ها
        self._lock     = threading.Lock()
        self._last     = 0.0             # زمان آخرین درخواست

    def acquire(self) -> None:
        """صبر کن تا token در دسترس باشد (blocking)."""
        with self._lock:
            now  = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP session per thread
# ═══════════════════════════════════════════════════════════════════════════

_thread_local = threading.local()

def _session() -> requests.Session:
    """یک requests.Session اختصاصی برای هر thread (thread-safe)."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(REQUEST_HEADERS)
        s.headers.update({
            "Referer": "https://www.tsetmc.ir/",
            "Origin":  "https://www.tsetmc.ir",
        })
        _thread_local.session = s
    return _thread_local.session


# ═══════════════════════════════════════════════════════════════════════════
#  Fetch با retry
# ═══════════════════════════════════════════════════════════════════════════

class FetchResult(NamedTuple):
    sym:      str
    ins_code: str
    date_int: int
    rows:     list[dict]    # خالی = بدون داده یا خطا
    error:    Optional[str] # پیغام خطا


def fetch_raw(sym: str, ins_code: str, date_int: int,
              rate: RateLimiter,
              max_retries: int = 4) -> FetchResult:
    """دانلود یک (fund, date) با rate limiting و retry."""

    url = f"{TSETMC_CDN}/BestLimits/{ins_code}/{date_int}"

    for attempt in range(max_retries):
        rate.acquire()                          # صبر برای rate limit
        # jitter تصادفی ۰–۰.۵ ثانیه برای جلوگیری از الگوی synchronized
        time.sleep(random.uniform(0.0, 0.5))

        try:
            r = _session().get(url, timeout=25)

            if r.status_code == 200:
                rows = r.json().get("bestLimitsHistory") or []
                return FetchResult(sym, ins_code, date_int, rows, None)

            if r.status_code == 429:
                # Too Many Requests — صبر طولانی‌تر
                wait = 30 * (attempt + 1)
                logger.warning("429 TooManyRequests — صبر %ds (attempt %d/%d)",
                               wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue

            if r.status_code in (403, 404):
                # احتمالاً داده‌ای وجود ندارد
                return FetchResult(sym, ins_code, date_int, [], f"HTTP {r.status_code}")

            if r.status_code >= 500:
                wait = 2 ** (attempt + 1)      # 2, 4, 8, 16 sec
                logger.warning("HTTP %d — retry %d/%d پس از %ds",
                               r.status_code, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue

            return FetchResult(sym, ins_code, date_int, [], f"HTTP {r.status_code}")

        except requests.exceptions.ConnectionError as e:
            wait = 2 ** (attempt + 1)
            logger.warning("Connection error — retry %d/%d پس از %ds: %s",
                           attempt + 1, max_retries, wait, e)
            time.sleep(wait)
        except requests.exceptions.Timeout:
            wait = 2 ** (attempt + 1)
            logger.warning("Timeout — retry %d/%d پس از %ds", attempt + 1, max_retries, wait)
            time.sleep(wait)
        except Exception as e:
            return FetchResult(sym, ins_code, date_int, [], str(e))

    return FetchResult(sym, ins_code, date_int, [], f"failed after {max_retries} attempts")


# ═══════════════════════════════════════════════════════════════════════════
#  DB write lock — SQLite با WAL می‌تواند concurrent بنویسد
#  اما یک lock ساده از race condition جلوگیری می‌کند
# ═══════════════════════════════════════════════════════════════════════════

_db_lock = threading.Lock()

def save(result: FetchResult, dry_run: bool) -> int:
    """ذخیره نتیجه در DB. Returns تعداد ردیف جدید."""
    if not result.rows:
        return 0
    if dry_run:
        return len(result.rows)
    with _db_lock:
        return DB.save_ob_ticks(result.sym, result.ins_code,
                                result.date_int, result.rows)


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════

def trading_dates(n: int, end: datetime | None = None) -> list[int]:
    TSE_WEEKEND = {3, 4}
    dates, d = [], (end or datetime.now())
    while len(dates) < n:
        if d.weekday() not in TSE_WEEKEND:
            dates.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return dates


def hhmm(t: int) -> str:
    s = str(t).zfill(6)
    return f"{s[:2]}:{s[2:4]}"


def fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


# ═══════════════════════════════════════════════════════════════════════════
#  جمع‌آوری موازی اصلی
# ═══════════════════════════════════════════════════════════════════════════

def fetch_all(dates: list[int], symbol_filter: str | None,
              force: bool, dry_run: bool,
              workers: int, max_rps: float):

    funds = [f for f in FIXED_INCOME_ETFS
             if (not symbol_filter or f["symbol"] == symbol_filter)
             and f.get("ins_code", "").strip()]

    # ساخت لیست task های (sym, ins_code, date_int)
    tasks: list[tuple[str, str, int]] = []
    for fund in funds:
        sym      = fund["symbol"]
        ins_code = fund["ins_code"].strip()
        have     = set(DB.get_ob_tick_dates(sym)) if not force else set()
        for d in dates:
            if d not in have:
                tasks.append((sym, ins_code, d))

    total   = len(tasks)
    skipped = len(funds) * len(dates) - total

    if total == 0:
        print("  همه داده‌ها قبلاً ذخیره شده‌اند. برای re-fetch از --force استفاده کنید.")
        return

    # تخمین زمان
    est_sec = total / max_rps + total * 0.25   # rough estimate
    print()
    print("═" * 72)
    print(f"  جمع‌آوری OB tick-by-tick (موازی)")
    print(f"  صندوق‌ها: {len(funds)}  تاریخ‌ها: {len(dates)}  task: {total:,}  رد شده: {skipped:,}")
    print(f"  workers: {workers}  rate limit: {max_rps:.1f} req/s")
    print(f"  تخمین زمان: {fmt_eta(est_sec)} (بهترین حالت)")
    if dry_run:
        print("  *** DRY RUN — ذخیره نمی‌شود ***")
    print("═" * 72)

    rate      = RateLimiter(max_rps)
    done      = 0
    new_rows  = 0
    errors    = 0
    t_start   = time.monotonic()
    print_lock = threading.Lock()

    def worker(task: tuple[str, str, int]) -> FetchResult:
        sym, ins_code, date_int = task
        return fetch_raw(sym, ins_code, date_int, rate)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, t): t for t in tasks}

        for future in as_completed(futures):
            result = future.result()
            saved  = save(result, dry_run)

            with print_lock:
                done += 1
                new_rows += saved
                if result.error and not result.rows:
                    errors += 1

                elapsed = time.monotonic() - t_start
                rps_actual = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rps_actual if rps_actual > 0 else 0

                # progress bar
                pct  = done / total
                bar  = "█" * int(pct * 30) + "░" * (30 - int(pct * 30))
                status = f"✓ {saved:>5,}" if saved > 0 else (f"– {result.error or 'empty':>15}" if result.error else "– empty")
                print(
                    f"\r  [{bar}] {pct:>5.1%}  {done:>5}/{total}  "
                    f"{result.sym:10s} {result.date_int}  {status}"
                    f"  ETA:{fmt_eta(eta)}  {rps_actual:.2f}req/s",
                    end="", flush=True,
                )

    print()   # newline بعد از progress bar
    elapsed = time.monotonic() - t_start
    print()
    print(f"  ✓ مجموع ردیف جدید: {new_rows:,}")
    print(f"  ✗ task های ناموفق: {errors}")
    print(f"  زمان کل: {fmt_eta(elapsed)}")
    print(f"  throughput واقعی: {total/elapsed:.2f} req/s")
    print()


# ═══════════════════════════════════════════════════════════════════════════
#  probe: تا کجا تاریخ موجود است؟
# ═══════════════════════════════════════════════════════════════════════════

def probe_history(ins_code: str = "3846143218462419"):
    """بررسی عمق تاریخی API با یک صندوق نمونه."""
    rate = RateLimiter(max_rps=1.5)
    print()
    print("═" * 60)
    print("  بررسی عمق تاریخی API")
    print("═" * 60)
    TSE_WEEKEND = {3, 4}
    today = datetime.now()
    first_available = None
    for days_back in [5, 10, 20, 30, 60, 90, 120, 180, 250, 365]:
        d = today - timedelta(days=days_back)
        while d.weekday() in TSE_WEEKEND:
            d -= timedelta(days=1)
        date_int = int(d.strftime("%Y%m%d"))
        result = fetch_raw("probe", ins_code, date_int, rate)
        ok = "✓" if result.rows else "✗"
        print(f"  {ok}  {days_back:>3} روز پیش ({date_int})  →  {len(result.rows):>5,} ردیف")
        if result.rows:
            first_available = date_int
    print()
    if first_available:
        print(f"  قدیمی‌ترین تاریخ موجود: {first_available}")
        diff = (datetime.now() - datetime.strptime(str(first_available), "%Y%m%d")).days
        print(f"  عمق تاریخی: ~{diff} روز")
    print()


# ═══════════════════════════════════════════════════════════════════════════
#  status
# ═══════════════════════════════════════════════════════════════════════════

def show_status(symbol_filter: str | None):
    print()
    print("═" * 72)
    print("  وضعیت ob_ticks در DB")
    print(f"  {'نماد':12s}  {'قدیمی‌ترین':10s}  {'جدیدترین':10s}  {'روز':>5}  {'مجموع ردیف':>12}")
    print("  " + "─" * 60)

    grand_rows = 0
    grand_days = 0
    for fund in FIXED_INCOME_ETFS:
        sym = fund["symbol"]
        if symbol_filter and sym != symbol_filter:
            continue
        dates = DB.get_ob_tick_dates(sym)
        if not dates:
            continue
        # شمارش ردیف فقط برای آخرین تاریخ (سریع‌تر)
        last_rows = len(DB.get_ob_ticks(sym, dates[-1]))
        grand_rows += last_rows * len(dates)  # تخمین
        grand_days += len(dates)
        print(f"  {sym:12s}  {dates[0]}  {dates[-1]}  {len(dates):>5}  ~{last_rows:>10,}/روز")

    print()
    print(f"  مجموع روز: {grand_days:,}  تخمین ردیف: ~{grand_rows:,}")
    print()


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--days",    type=int,   default=30,
                    help="چند روز معاملاتی گذشته (پیش‌فرض: ۳۰)")
    ap.add_argument("--date",    metavar="YYYYMMDD",
                    help="تاریخ پایان بازه (پیش‌فرض: امروز)")
    ap.add_argument("--symbol",  help="فقط یک نماد مشخص")
    ap.add_argument("--force",   action="store_true",
                    help="حتی اگر قبلاً ذخیره شده، دوباره دانلود کن")
    ap.add_argument("--workers", type=int, default=3,
                    metavar="N",
                    help="تعداد thread موازی (پیش‌فرض: ۳، پیشنهاد: ۳-۵)")
    ap.add_argument("--rps",     type=float, default=3.0,
                    metavar="N",
                    help="حداکثر درخواست در ثانیه — سراسری (پیش‌فرض: ۳)")
    ap.add_argument("--probe",   action="store_true",
                    help="بررسی عمق تاریخی API")
    ap.add_argument("--dry-run", action="store_true",
                    help="دانلود کن ولی ذخیره نکن")
    ap.add_argument("--status",  action="store_true",
                    help="فقط وضعیت DB")
    args = ap.parse_args()

    # اعتبارسنجی
    if args.workers > 8:
        print("⚠  workers بیش از ۸ توصیه نمی‌شود — ریسک ban شدن IP")
    if args.rps > 6:
        print("⚠  rps بیش از ۶ توصیه نمی‌شود — ریسک ban شدن IP")

    if args.status:
        show_status(args.symbol)
        return

    if args.probe:
        probe_history()
        return

    end = None
    if args.date:
        try:
            end = datetime.strptime(args.date, "%Y%m%d")
        except ValueError:
            print("خطا: فرمت تاریخ باید YYYYMMDD باشد")
            sys.exit(1)

    dates = trading_dates(args.days, end=end)

    fetch_all(
        dates        = dates,
        symbol_filter= args.symbol,
        force        = args.force,
        dry_run      = args.dry_run,
        workers      = args.workers,
        max_rps      = args.rps,
    )
    show_status(args.symbol)


if __name__ == "__main__":
    main()
