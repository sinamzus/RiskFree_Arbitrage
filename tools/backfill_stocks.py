#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
جمع‌آوریِ دادهٔ سهامِ بورس/فرابورس برای بک‌تستِ بازارگردانی — قابل‌ازسرگیری
==========================================================================
دو کار انجام می‌دهد:

۱) کشفِ کاملِ نمادها (MarketWatch): فهرستِ همهٔ ابزارهای بورس+فرابورس را از
   TSETMC می‌گیرد و در جدولِ `instruments` ذخیره می‌کند (با بازار/نوع/حجم مبنا).
   این متادیتا سبک است و سریع.

۲) جمع‌آوریِ درون‌روزِ واچ‌لیست: برای نمادهای علامت‌خوردهٔ واچ‌لیست (watch=1) —
   که خودتان انتخاب می‌کنید — دادهٔ روزانه + تیک‌به‌تیک + اردربوک را جمع می‌کند.
   چون «همهٔ سهام تیک‌به‌تیک» صدها گیگ و چند روز است، فقط نمادهای هدفِ
   بازارگردانی جمع می‌شوند (دقیقاً همان چیزی که بک‌تست لازم دارد).

قابلیت‌ها (مثلِ backfill_akhza_10y)
-----------------------------------
• مرحله‌ای: اول روزانهٔ همهٔ واچ‌لیست، بعد درون‌روز.
• پنجرهٔ هر نماد: تاریخ‌های درون‌روز از تقویمِ واقعیِ معاملاتیِ خودِ نماد.
• قابل‌ازسرگیری: رد-شدن در سطح (نماد، تاریخ) از روی DB + چک‌پوینت. قطع شد؟
  همان دستور را دوباره بزنید.
• تاب‌آور: هر درخواست ۳ بار با backoff؛ خطای هر (نماد،تاریخ) لاگ و رد می‌شود.

اجرا (روی ویندوزِ خودتان — TSETMC از IP دیتاسنتر 403 می‌دهد)
-------------------------------------------------------------
    # ۱) کشفِ همهٔ نمادها و ذخیره در instruments
    python tools/backfill_stocks.py --discover

    # ۲) افزودنِ نمادها به واچ‌لیست
    python tools/backfill_stocks.py --watch فولاد,وبملت,شستا

    # ۳) جمع‌آوریِ ۱۰ سالهٔ واچ‌لیست (مرحله‌ای، قابل‌ازسرگیری)
    python tools/backfill_stocks.py --collect

    # همه با هم
    python tools/backfill_stocks.py --discover --watch فولاد,خساپا --collect --years 10
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

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

DB_PATH_DEFAULT = ROOT / "data" / "arbitrage.db"
CKPT_PATH = ROOT / "logs" / "backfill_stocks_checkpoint.json"

logger = logging.getLogger("backfill_stocks")


# ── date / checkpoint helpers (same approach as backfill_akhza_10y) ──────────

def _today_int() -> int:
    return int(datetime.now().strftime("%Y%m%d"))


def _years_ago_int(years: int) -> int:
    t = datetime.now().date()
    try:
        return int(t.replace(year=t.year - years).strftime("%Y%m%d"))
    except ValueError:
        return int((t - timedelta(days=365 * years)).strftime("%Y%m%d"))


def _fmt(d: int) -> str:
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else s


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


# ── discovery ────────────────────────────────────────────────────────────────

def resolve_and_watch(db, fetcher, symbols: list[str]) -> int:
    """Resolve each symbol's ins_code via per-symbol TSETMC search and add it to
    the watchlist — independent of the bulk MarketWatch call.

    Collection needs an ins_code, so a bare watch flag on a non-existent row is
    useless. This searches each نماد, upserts an instruments row with its real
    ins_code, and sets watch=1. Returns the count resolved.
    """
    from data_fetcher import _normalize, classify_instrument
    rows, missing = [], []
    for sym in symbols:
        target = _normalize(sym)
        try:
            hits = fetcher.search_instrument(sym)
        except Exception as exc:
            logger.warning("  جستجوی «%s» ناموفق: %s", sym, exc)
            missing.append(sym)
            continue
        match = next((h for h in hits if _normalize(h.get("symbol", "")) == target), None)
        if not match:
            match = next((h for h in hits
                          if _normalize(h.get("symbol", "")).startswith(target)), None)
        if not match or not (match.get("ins_code") or "").strip():
            logger.warning("  نماد یافت نشد یا بدونِ ins_code: «%s»", sym)
            missing.append(sym)
            continue
        nm = match.get("full_name", "")
        rows.append({"symbol": _normalize(match["symbol"]),
                     "ins_code": match["ins_code"].strip(), "name": nm,
                     "type": classify_instrument(match["symbol"], nm),
                     "market": "", "watch": 1, "updated": _today_int()})
        logger.info("  ✓ %s → %s", _normalize(match["symbol"]), match["ins_code"].strip())
    if rows:
        db.upsert_instruments(rows)
        db.set_instrument_watch([r["symbol"] for r in rows], True)
    logger.info("واچ‌لیست: %d از %d نماد resolve و علامت‌گذاری شد%s",
                len(rows), len(symbols),
                (" — ناموفق: " + ", ".join(missing)) if missing else "")
    return len(rows)


def discover(db, fetcher) -> int:
    """MarketWatch → instruments table. Returns count stored."""
    rows = fetcher.get_market_watch()
    if not rows:
        logger.warning("کشف ناموفق — MarketWatch داده‌ای نداد (endpoint/شبکه؟)")
        return 0
    today = _today_int()
    for r in rows:
        r["updated"] = today
    n = db.upsert_instruments(rows)
    by_mkt: dict = {}
    by_type: dict = {}
    for r in rows:
        by_mkt[r["market"]] = by_mkt.get(r["market"], 0) + 1
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    logger.info("کشف: %d ابزار ذخیره شد | بازار=%s | نوع=%s", n, by_mkt, by_type)
    return n


# ── collection (mirrors backfill_akhza_10y) ──────────────────────────────────

def _symbol_trading_dates(db_path, symbol, from_date, to_date) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT date FROM daily_history WHERE symbol=? AND date BETWEEN ? AND ? "
            "ORDER BY date", (symbol, from_date, to_date)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def phase_daily(db, fetcher, targets, *, from_date, to_date, years, force,
                workers, ckpt, stop) -> dict:
    done = set(ckpt.get("daily_done", []))
    todo = [t for t in targets if force or t["symbol"] not in done]
    logger.info("── فاز ۱: روزانه ── %d نماد (%d رد شد)", len(todo),
                len(targets) - len(todo))
    n_req = max(400, years * 300 + 200)
    totals = {"rows": 0, "syms": 0, "fail": 0}
    lock = threading.Lock()

    def _one(t):
        if stop.is_set():
            return
        sym, ins = t["symbol"], (t.get("ins_code") or "").strip()
        try:
            entries = fetcher.get_historical_daily(ins, days=n_req)
            entries = [e for e in entries if from_date <= e["date"] <= to_date]
            rows = db.save_daily_history(sym, ins, entries) if entries else 0
            with lock:
                totals["rows"] += rows
                totals["syms"] += 1
            logger.info("  %-14s روزانه: +%d (پنجره %d)", sym, rows, len(entries))
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
        stop.set(); raise
    logger.info("فاز ۱ تمام: %d نماد، +%d ردیف، %d خطا",
                totals["syms"], totals["rows"], totals["fail"])
    return totals


def phase_intraday(db, fetcher, targets, *, db_path, from_date, to_date,
                   do_ticks, do_ob, force, workers, ckpt, stop) -> dict:
    done = set(ckpt.get("intraday_done", []))
    todo = [t for t in targets if force or t["symbol"] not in done]
    logger.info("── فاز ۲: درون‌روز ── %d نماد (%d رد شد)", len(todo),
                len(targets) - len(todo))
    today_int = _today_int()
    totals = {"ticks": 0, "ob": 0, "dates": 0, "empty": 0, "fail": 0}
    lock = threading.Lock()

    def _one(t):
        if stop.is_set():
            return
        sym, ins = t["symbol"], (t.get("ins_code") or "").strip()
        trading = _symbol_trading_dates(db_path, sym, from_date, to_date)
        if not trading:
            logger.info("  %-14s درون‌روز: تقویم روزانه خالی — رد شد", sym)
            _mark_done(ckpt, "intraday_done", sym)
            return
        have_tk = set(db.get_intraday_dates(sym)) if do_ticks else set()
        have_ob = set(db.get_ob_dates(sym)) if do_ob else set()
        t_new = o_new = n_dates = n_empty = 0
        try:
            for d in trading:
                if stop.is_set():
                    return
                if d >= today_int:
                    continue
                got = False
                if do_ticks and (force or d not in have_tk):
                    try:
                        trd = fetcher.get_intraday_trades(ins, d)
                        if trd:
                            t_new += db.save_intraday_trades(sym, ins, d, trd)
                            got = True
                    except Exception as exc:
                        with lock:
                            totals["fail"] += 1
                        logger.debug("    %s %s تیک ناموفق: %s", sym, d, exc)
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
                            got = True
                    except Exception as exc:
                        with lock:
                            totals["fail"] += 1
                        logger.debug("    %s %s اردربوک ناموفق: %s", sym, d, exc)
                n_dates += 1
                if not got:
                    n_empty += 1
        except KeyboardInterrupt:
            stop.set(); raise
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
        stop.set(); raise
    logger.info("فاز ۲ تمام: %d تاریخ | تیک+%d | اردربوک+%d | %d خالی | %d خطا",
                totals["dates"], totals["ticks"], totals["ob"],
                totals["empty"], totals["fail"])
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(
        description="جمع‌آوریِ دادهٔ سهام برای بک‌تستِ بازارگردانی — قابل‌ازسرگیری",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="کشفِ MarketWatch و ذخیره در instruments")
    ap.add_argument("--watch", type=str, default=None, help="افزودنِ این نمادها (با کاما) به واچ‌لیست")
    ap.add_argument("--unwatch", type=str, default=None, help="حذفِ این نمادها از واچ‌لیست")
    ap.add_argument("--collect", action="store_true", help="جمع‌آوریِ روزانه+درون‌روزِ واچ‌لیست")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--from-date", type=int, default=None)
    ap.add_argument("--to-date", type=int, default=None)
    ap.add_argument("--phase", choices=["all", "daily", "intraday"], default="all")
    ap.add_argument("--only", choices=["all", "ticks", "ob"], default="all")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reset-checkpoint", action="store_true")
    ap.add_argument("--db", type=str, default=str(DB_PATH_DEFAULT))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    logfile = ROOT / "logs" / f"backfill_stocks_{_today_int()}.log"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(logfile, encoding="utf-8")])
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

    if args.reset_checkpoint:
        try:
            CKPT_PATH.unlink()
        except FileNotFoundError:
            pass
        logger.info("چک‌پوینت پاک شد (DB دست‌نخورده)")

    if args.discover:
        discover(db, fetcher)
    if args.watch:
        syms = [s.strip() for s in args.watch.split(",") if s.strip()]
        # Resolve each symbol's ins_code (per-symbol search) so the watchlist is
        # usable even if the bulk MarketWatch discovery returned nothing.
        resolve_and_watch(db, fetcher, syms)
    if args.unwatch:
        syms = [s.strip() for s in args.unwatch.split(",") if s.strip()]
        db.set_instrument_watch(syms, False)
        logger.info("از واچ‌لیست حذف شد: %s", syms)

    if not args.collect:
        if not (args.discover or args.watch or args.unwatch):
            ap.print_help()
        return 0

    targets = db.get_instruments(watch_only=True)
    targets = [t for t in targets if (t.get("ins_code") or "").strip()]
    if not targets:
        logger.error("واچ‌لیست خالی است. اول با --watch نماد اضافه کنید "
                     "(و در صورت نیاز --discover).")
        return 1

    to_date = args.to_date or int((datetime.now().date() - timedelta(days=1)).strftime("%Y%m%d"))
    from_date = args.from_date or _years_ago_int(args.years)
    ckpt = _load_ckpt()

    logger.info("═" * 64)
    logger.info("جمع‌آوریِ سهام | واچ‌لیست=%d | پنجره %s→%s | فاز=%s | داده=%s | نخ=%d",
                len(targets), _fmt(from_date), _fmt(to_date), args.phase, args.only, workers)
    logger.info("لاگ: %s", logfile)
    logger.info("═" * 64)

    do_ticks = args.only in ("all", "ticks")
    do_ob = args.only in ("all", "ob")
    stop = threading.Event()
    t0 = time.time()
    try:
        if args.phase in ("all", "daily"):
            phase_daily(db, fetcher, targets, from_date=from_date, to_date=to_date,
                        years=args.years, force=args.force, workers=workers,
                        ckpt=ckpt, stop=stop)
        if args.phase in ("all", "intraday"):
            phase_intraday(db, fetcher, targets, db_path=db_path, from_date=from_date,
                           to_date=to_date, do_ticks=do_ticks, do_ob=do_ob,
                           force=args.force, workers=workers, ckpt=ckpt, stop=stop)
    except KeyboardInterrupt:
        _save_ckpt(ckpt)
        logger.warning("⏸ متوقف شد. پیشرفت ذخیره شد — همین دستور را دوباره بزنید.")
        return 130

    _save_ckpt(ckpt)
    logger.info("✅ پایان (%.1f دقیقه). برای ادامه همین دستور را دوباره بزنید.",
                (time.time() - t0) / 60.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
