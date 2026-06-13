#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بک‌فیل ۱۰ سالهٔ کامل اخزا (روزانه + تیک + اردربوک) — قابل‌ازسرگیری
====================================================================
کل تاریخچهٔ اخزا (اسناد خزانهٔ اسلامی) را برای ~۱۰ سال اخیر — *شاملِ نمادهای
سررسیدشده/حذف‌شده* — از TSETMC می‌گیرد و در همان جدول‌های symbol-keyed ذخیره
می‌کند که بک‌تست/بهینه‌ساز z-spread از آن‌ها می‌خوانند:

    daily_history        ← OHLCV روزانه (≈۱۰ سال)
    intraday_trades      ← تیک‌به‌تیک معاملات (در پنجرهٔ عمر هر نماد)
    intraday_orderbook   ← اردربوک بازسازی‌شده (per-minute) از bestLimitsHistory

چرا یک ابزار جدا؟  `bonds.collect_bond_history` سه محدودیت دارد که این کار را
ناممکن می‌کند: (۱) نمادهای سررسیدشده را عمداً حذف می‌کند، (۲) با rediscover
کل رجیستری را پاک می‌کند، (۳) پنجرهٔ درون‌روز را «N روز اخیر از امروز» می‌گیرد
نه پنجرهٔ واقعیِ عمر هر نماد.  این اسکریپت هر سه را حل می‌کند.

ویژگی‌ها
--------
• کشف فراگیر: چند جستجوی کلیدواژه‌ای تا کلِ فضای نام اخزای ۱۰ سال پوشش یابد؛
  نمادهای سررسیدشده با active=0 ذخیره می‌شوند (اسکنِ زنده آن‌ها را نادیده
  می‌گیرد، ولی بک‌تست با active_only=False می‌بیندشان).  رجیستری *پاک نمی‌شود*.
• پنجرهٔ هر نماد: تاریخ‌های درون‌روز از تقویمِ واقعیِ معاملاتیِ همان نماد
  (از daily_history) گرفته می‌شود؛ پس برای نمادی که ۶ ماه معامله شده، هزاران
  تاریخِ خالی درخواست نمی‌شود.
• قابل‌ازسرگیری: رد-شدن در سطح (نماد، تاریخ) از روی خود دیتابیس انجام می‌شود؛
  اگر اینترنت قطع شد یا Ctrl-C زدید، اجرای دوبارهٔ همان دستور از همان‌جا ادامه
  می‌دهد.  یک فایل checkpoint هم برای رد-شدنِ سریعِ نمادهای تمام‌شده نوشته
  می‌شود (logs/backfill_akhza_checkpoint.json).
• تاب‌آور: هر درخواست در fetcher سه بار با backoff نمایی تلاش می‌شود؛ خطای هر
  (نماد، تاریخ) لاگ و رد می‌شود، اجرا متوقف نمی‌گردد.  تاریخ‌هایی که TSETMC
  داده‌شان را ندارد (اردربوک خیلی قدیمی) خودکار رد می‌شوند.

اجرا (روی ویندوز خودتان — نه محیط ابری، چون TSETMC از IP دیتاسنتر 403 می‌دهد)
---------------------------------------------------------------------------
    # کل کار، مرحله‌ای (اول روزانهٔ همه، بعد درون‌روز) — پیش‌فرض
    python tools/backfill_akhza_10y.py

    # فقط فاز روزانه (سریع، چند دقیقه تا ساعت) تا داده روزانهٔ ۱۰ ساله آماده شود
    python tools/backfill_akhza_10y.py --phase daily

    # ادامهٔ فاز درون‌روز بعداً (تیک + اردربوک)
    python tools/backfill_akhza_10y.py --phase intraday

    # محدود به چند نماد، یا بدون اردربوک
    python tools/backfill_akhza_10y.py --symbols اخزا901,اخزا815
    python tools/backfill_akhza_10y.py --only ticks

اگر قطع شد: دقیقاً همان دستور را دوباره بزنید؛ از همان‌جا ادامه می‌دهد.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Persian output on the Windows console.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

DB_PATH_DEFAULT = ROOT / "data" / "arbitrage.db"
CKPT_PATH = ROOT / "logs" / "backfill_akhza_checkpoint.json"

logger = logging.getLogger("backfill_akhza_10y")


# --------------------------------------------------------------------------- #
#  Date helpers                                                                 #
# --------------------------------------------------------------------------- #

def _today_int() -> int:
    return int(datetime.now().strftime("%Y%m%d"))


def _int_to_date(d: int) -> date:
    return date(d // 10000, (d // 100) % 100, d % 100)


def _years_ago_int(years: int) -> int:
    t = datetime.now().date()
    try:
        return int(t.replace(year=t.year - years).strftime("%Y%m%d"))
    except ValueError:                      # 29 Feb edge
        return int((t - timedelta(days=365 * years)).strftime("%Y%m%d"))


def _fmt(d: int) -> str:
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else s


# --------------------------------------------------------------------------- #
#  Checkpoint (fast-skip of finished symbols; DB stays the source of truth)     #
# --------------------------------------------------------------------------- #

_ckpt_lock = threading.Lock()


def _load_ckpt() -> dict:
    try:
        return json.loads(CKPT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"daily_done": [], "intraday_done": []}


def _save_ckpt(ckpt: dict) -> None:
    with _ckpt_lock:
        ckpt["updated_at"] = datetime.now().isoformat(timespec="seconds")
        CKPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CKPT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(ckpt, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(CKPT_PATH)


def _mark_done(ckpt: dict, key: str, symbol: str) -> None:
    with _ckpt_lock:
        lst = ckpt.setdefault(key, [])
        if symbol not in lst:
            lst.append(symbol)
    _save_ckpt(ckpt)


# --------------------------------------------------------------------------- #
#  Discovery — ALL اخزا (active + matured), registry merged, never wiped        #
# --------------------------------------------------------------------------- #

# Broad keyword set: اخزا names embed the budget year as their leading digit(s),
# so اخزا0..اخزا9 sweeps the whole ~10-year name space; the bare "اخزا" and the
# full Persian phrase catch anything the digit-prefixed queries miss.  Each
# search is rate-limited and de-duplicated by ins_code inside discover_akhza.
_DISCOVERY_QUERIES = (
    ["اخزا"] + [f"اخزا{d}" for d in "0123456789"]
    + ["اسناد خزانه", "اسنادخزانه", "خزانه اسلامي", "خزانه اسلامی"]
)


def discover_all_akhza(db, fetcher, *, active_only: bool,
                       rediscover: bool) -> list[dict]:
    """Return the اخزا universe (incl. matured) and persist it without wiping.

    Merges freshly discovered bills into bond_series via upsert (INSERT OR
    REPLACE by symbol PK).  Matured bills keep active=0.  Existing registry
    rows are preserved, so re-runs never lose previously found delisted names.
    """
    existing = {}
    try:
        for s in db.get_bond_series(active_only=False):
            if s.get("symbol"):
                existing[s["symbol"]] = s
    except Exception:
        pass

    discovered: list[dict] = []
    if rediscover and hasattr(fetcher, "discover_akhza"):
        try:
            discovered = fetcher.discover_akhza(queries=_DISCOVERY_QUERIES)
            logger.info("کشف: %d ورق از TSETMC (%d فعال) — رجیستری ادغام شد (پاک نشد)",
                        len(discovered), sum(1 for d in discovered if d.get("active")))
            if discovered and hasattr(db, "upsert_bond_series"):
                db.upsert_bond_series(discovered)
        except Exception as exc:
            logger.warning("کشف ناموفق (%s) — از رجیستری موجود استفاده می‌شود", exc)

    # Merge discovered over existing (discovered is fresher).
    merged = dict(existing)
    for d in discovered:
        if d.get("symbol"):
            merged[d["symbol"]] = d

    universe = [s for s in merged.values() if (s.get("ins_code") or "").strip()]
    if active_only:
        today = _today_int()
        universe = [s for s in universe
                    if not int(s.get("maturity_date", 0) or 0)
                    or int(s["maturity_date"]) > today]
    # Oldest-maturity first → finished bills collected before live ones.
    universe.sort(key=lambda s: int(s.get("maturity_date", 0) or 99999999))
    n_mat = sum(1 for s in universe
                if int(s.get("maturity_date", 0) or 0)
                and int(s["maturity_date"]) <= _today_int())
    logger.info("جهان اخزا: %d نماد قابل‌جمع‌آوری (%d سررسیدشده، %d فعال)",
                len(universe), n_mat, len(universe) - n_mat)
    return universe


# --------------------------------------------------------------------------- #
#  Phase 1 — daily OHLCV (full ~10-year window)                                 #
# --------------------------------------------------------------------------- #

def _daily_request_count(years: int) -> int:
    # ~250 trading days/yr; pad generously so the window is always fully covered.
    return max(400, years * 300 + 200)


def phase_daily(db, fetcher, universe, *, from_date, to_date, years,
                force, workers, ckpt, stop) -> dict:
    done = set(ckpt.get("daily_done", []))
    todo = [s for s in universe if force or s["symbol"] not in done]
    logger.info("── فاز ۱: روزانه ── %d نماد (%d قبلاً انجام‌شده، رد شد)",
                len(todo), len(universe) - len(todo))
    n_req = _daily_request_count(years)
    totals = {"rows": 0, "syms": 0, "fail": 0}
    lock = threading.Lock()

    def _one(s: dict):
        if stop.is_set():
            return
        sym, ins = s["symbol"], (s.get("ins_code") or "").strip()
        try:
            entries = fetcher.get_historical_daily(ins, days=n_req)
            entries = [e for e in entries
                       if from_date <= e["date"] <= to_date]
            rows = db.save_daily_history(sym, ins, entries) if entries else 0
            with lock:
                totals["rows"] += rows
                totals["syms"] += 1
            logger.info("  %-14s روزانه: %d ردیف جدید (پنجره %d ردیف)",
                        sym, rows, len(entries))
        except Exception as exc:
            with lock:
                totals["fail"] += 1
            logger.warning("  %s روزانه ناموفق: %s", sym, exc)
        finally:
            _mark_done(ckpt, "daily_done", sym)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_one, todo))
    except KeyboardInterrupt:
        stop.set()
        raise
    logger.info("فاز ۱ تمام: %d نماد، %d ردیف روزانهٔ جدید، %d خطا",
                totals["syms"], totals["rows"], totals["fail"])
    return totals


# --------------------------------------------------------------------------- #
#  Phase 2 — intraday ticks + reconstructed order book                          #
# --------------------------------------------------------------------------- #

def _symbol_trading_dates(db_path, symbol, from_date, to_date) -> list[int]:
    """Actual trading calendar for *symbol* = dates present in daily_history
    inside the window.  Drives which dates we even try for intraday."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT date FROM daily_history WHERE symbol=? AND date BETWEEN ? AND ? "
            "ORDER BY date", (symbol, from_date, to_date)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def phase_intraday(db, fetcher, universe, *, db_path, from_date, to_date,
                   do_ticks, do_ob, force, workers, ckpt, stop) -> dict:
    done = set(ckpt.get("intraday_done", []))
    todo = [s for s in universe if force or s["symbol"] not in done]
    logger.info("── فاز ۲: درون‌روز (%s%s%s) ── %d نماد (%d قبلاً انجام‌شده، رد شد)",
                "تیک" if do_ticks else "", " + " if (do_ticks and do_ob) else "",
                "اردربوک" if do_ob else "", len(todo), len(universe) - len(todo))
    today_int = _today_int()
    totals = {"ticks": 0, "ob": 0, "dates": 0, "empty": 0, "fail": 0}
    lock = threading.Lock()

    def _one(s: dict):
        if stop.is_set():
            return
        sym, ins = s["symbol"], (s.get("ins_code") or "").strip()
        trading = _symbol_trading_dates(db_path, sym, from_date, to_date)
        if not trading:
            logger.info("  %-14s درون‌روز: تقویم روزانه خالی است — رد شد", sym)
            _mark_done(ckpt, "intraday_done", sym)
            return
        have_tk = set(db.get_intraday_dates(sym)) if do_ticks else set()
        have_ob = set(db.get_ob_dates(sym)) if do_ob else set()
        t_new = o_new = n_dates = n_empty = 0
        try:
            for d in trading:
                if stop.is_set():
                    return                  # leave symbol unmarked → resume later
                if d >= today_int:          # today is still forming; skip
                    continue
                got_any = False
                # ── ticks ──
                if do_ticks and (force or d not in have_tk):
                    try:
                        trd = fetcher.get_intraday_trades(ins, d)
                        if trd:
                            t_new += db.save_intraday_trades(sym, ins, d, trd)
                            got_any = True
                    except Exception as exc:
                        with lock:
                            totals["fail"] += 1
                        logger.debug("    %s %s تیک ناموفق: %s", sym, d, exc)
                # ── order book ──
                if do_ob and (force or d not in have_ob):
                    try:
                        snaps = fetcher.get_best_limits_history(ins, d)
                        if snaps:
                            for sn in snaps:
                                if db.save_orderbook_snapshot(
                                    sym, ins, d, sn.get("time", 0),
                                    {"bids": sn.get("bids", []),
                                     "asks": sn.get("asks", [])}, nav=0.0):
                                    o_new += 1
                            got_any = True
                    except Exception as exc:
                        with lock:
                            totals["fail"] += 1
                        logger.debug("    %s %s اردربوک ناموفق: %s", sym, d, exc)
                n_dates += 1
                if not got_any:
                    n_empty += 1
        except KeyboardInterrupt:
            stop.set()
            raise
        with lock:
            totals["ticks"] += t_new
            totals["ob"] += o_new
            totals["dates"] += n_dates
            totals["empty"] += n_empty
        logger.info("  %-14s درون‌روز: %d تاریخ | تیک+%d | اردربوک+%d | %d خالی",
                    sym, n_dates, t_new, o_new, n_empty)
        _mark_done(ckpt, "intraday_done", sym)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_one, todo))
    except KeyboardInterrupt:
        stop.set()
        raise
    logger.info("فاز ۲ تمام: %d تاریخ | تیک+%d | اردربوک+%d | %d تاریخ خالی | %d خطا",
                totals["dates"], totals["ticks"], totals["ob"],
                totals["empty"], totals["fail"])
    return totals


# --------------------------------------------------------------------------- #
#  Main                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description="بک‌فیل ۱۰ سالهٔ کامل اخزا (روزانه + تیک + اردربوک) — قابل‌ازسرگیری",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, default=10, help="عمق به سال (پیش‌فرض ۱۰)")
    ap.add_argument("--from-date", type=int, default=None, help="شروع YYYYMMDD (به‌جای --years)")
    ap.add_argument("--to-date", type=int, default=None, help="پایان YYYYMMDD (پیش‌فرض دیروز)")
    ap.add_argument("--phase", choices=["all", "daily", "intraday"], default="all",
                    help="کدام فاز اجرا شود (پیش‌فرض all = مرحله‌ای)")
    ap.add_argument("--only", choices=["all", "ticks", "ob"], default="all",
                    help="در فاز درون‌روز کدام داده (پیش‌فرض all = تیک + اردربوک)")
    ap.add_argument("--symbols", type=str, default=None, help="فهرست نمادها با کاما (پیش‌فرض همه)")
    ap.add_argument("--active-only", action="store_true", help="فقط نمادهای فعال (پیش‌فرض: شامل سررسیدشده‌ها)")
    ap.add_argument("--no-rediscover", action="store_true", help="کشف دوباره انجام نشود؛ از رجیستری موجود استفاده شود")
    ap.add_argument("--workers", type=int, default=None, help="تعداد نخ‌ها (پیش‌فرض config.FETCH_WORKERS)")
    ap.add_argument("--force", action="store_true", help="حتی اگر داده موجود است دوباره بگیر")
    ap.add_argument("--reset-checkpoint", action="store_true", help="چک‌پوینت را پاک کن (از اول، ولی DB حفظ می‌شود)")
    ap.add_argument("--db", type=str, default=str(DB_PATH_DEFAULT), help="مسیر دیتابیس")
    ap.add_argument("--verbose", action="store_true", help="لاگ سطح DEBUG")
    args = ap.parse_args()

    Path(ROOT / "logs").mkdir(parents=True, exist_ok=True)
    logfile = ROOT / "logs" / f"backfill_akhza_{_today_int()}.log"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(logfile, encoding="utf-8")])
    # Quiet the per-request retry noise unless --verbose.
    if not args.verbose:
        logging.getLogger("data_fetcher").setLevel(logging.ERROR)

    from database import Database
    from data_fetcher import TSETMCFetcher
    try:
        from config import FETCH_WORKERS
    except Exception:
        FETCH_WORKERS = 5

    db_path = Path(args.db)
    db = Database(db_path)
    fetcher = TSETMCFetcher()
    workers = max(1, args.workers or FETCH_WORKERS)

    to_date = args.to_date or int((datetime.now().date() - timedelta(days=1)).strftime("%Y%m%d"))
    from_date = args.from_date or _years_ago_int(args.years)

    if args.reset_checkpoint:
        try:
            CKPT_PATH.unlink()
        except FileNotFoundError:
            pass
        logger.info("چک‌پوینت پاک شد (دیتابیس دست‌نخورده)")
    ckpt = _load_ckpt()

    logger.info("═" * 64)
    logger.info("بک‌فیل اخزا | پنجره %s → %s | فاز=%s | داده=%s | نخ=%d | DB=%s",
                _fmt(from_date), _fmt(to_date), args.phase, args.only, workers, db_path)
    logger.info("لاگ کامل: %s", logfile)
    logger.info("═" * 64)

    universe = discover_all_akhza(
        db, fetcher, active_only=args.active_only,
        rediscover=not args.no_rediscover)
    if args.symbols:
        want = {s.strip() for s in args.symbols.split(",") if s.strip()}
        universe = [s for s in universe if s["symbol"] in want]
        logger.info("محدود به %d نماد انتخابی", len(universe))
    if not universe:
        logger.error("هیچ نمادی برای جمع‌آوری یافت نشد.")
        return 1

    do_ticks = args.only in ("all", "ticks")
    do_ob = args.only in ("all", "ob")
    stop = threading.Event()
    t0 = time.time()
    try:
        if args.phase in ("all", "daily"):
            phase_daily(db, fetcher, universe, from_date=from_date,
                        to_date=to_date, years=args.years, force=args.force,
                        workers=workers, ckpt=ckpt, stop=stop)
        if args.phase in ("all", "intraday"):
            phase_intraday(db, fetcher, universe, db_path=db_path,
                           from_date=from_date, to_date=to_date,
                           do_ticks=do_ticks, do_ob=do_ob, force=args.force,
                           workers=workers, ckpt=ckpt, stop=stop)
    except KeyboardInterrupt:
        _save_ckpt(ckpt)
        logger.warning("⏸ متوقف شد (Ctrl-C). پیشرفت ذخیره شد — همین دستور را "
                       "دوباره بزنید تا از همین‌جا ادامه دهد.")
        return 130

    _save_ckpt(ckpt)
    mins = (time.time() - t0) / 60.0
    logger.info("═" * 64)
    logger.info("✅ پایان (%.1f دقیقه). برای ادامه/به‌روزرسانی بعدی، همین دستور "
                "را دوباره بزنید — کارهای انجام‌شده رد می‌شوند.", mins)
    return 0


if __name__ == "__main__":
    sys.exit(main())
