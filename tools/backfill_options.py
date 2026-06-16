#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
جمع‌آوریِ دادهٔ اختيار معامله (آپشن) برای بک‌تستِ آربیتراژ — قابل‌ازسرگیری
==========================================================================
سه کار:

۱) کشفِ آپشن‌ها (`--discover`): قراردادهای اختيار را از TSETMC می‌گیرد، نوع/قیمت
   اعمال/سررسید/دارایی پایه را از نام استخراج و در جدولِ `option_series` ذخیره
   می‌کند.  با `--underlyings` زنجیرهٔ کاملِ آن پایه‌ها را هم می‌کشد.

۲) انتخابِ پایه‌ها (`--underlyings فولاد,اهرم`): زنجیرهٔ آپشنِ این پایه‌ها را
   watch=1 می‌کند و ins_code خودِ پایه را هم resolve و ذخیره می‌کند (چون
   آربیتراژ به اردربوکِ پایه نیاز دارد).

۳) جمع‌آوری (`--collect`): برای هر پایه + همهٔ آپشن‌های watchِ آن، دادهٔ روزانه +
   تیک + اردربوک را جمع می‌کند — مرحله‌ای و قابل‌ازسرگیری (فازهای تست‌شدهٔ
   backfill_stocks بازاستفاده می‌شوند، با چک‌پوینتِ مستقلِ آپشن).

اجرا (روی ویندوزِ خودتان):
    python tools/backfill_options.py --underlyings اهرم,خساپا --discover --collect
    # ادامه پس از قطعی: همان دستور را دوباره بزنید.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

import backfill_stocks as bs           # reuse phased/resumable collection
from backfill_stocks import _today_int, _years_ago_int, _fmt

DB_PATH_DEFAULT = ROOT / "data" / "arbitrage.db"
logger = logging.getLogger("backfill_options")


def discover(db, fetcher, underlyings) -> int:
    rows = fetcher.discover_options(underlyings or None)
    if not rows:
        logger.warning("کشفِ آپشن داده‌ای نداد (شبکه/endpoint؟). "
                       "می‌توانید با --underlyings زنجیرهٔ نمادها را مستقیم بکشید.")
        return 0
    today = _today_int()
    for r in rows:
        r["updated"] = today
    n = db.upsert_option_series(rows)
    n_act = sum(1 for r in rows if r.get("active"))
    unds = {r["underlying"] for r in rows if r["underlying"]}
    logger.info("کشف: %d قرارداد (%d فعال) روی %d پایه ذخیره شد", n, n_act, len(unds))
    return n


def watch_underlyings(db, fetcher, underlyings) -> int:
    """زنجیرهٔ این پایه‌ها را watch می‌کند و ins_code پایه را resolve می‌کند."""
    from data_fetcher import _normalize, classify_instrument, parse_option_name
    targets = {_normalize(u) for u in underlyings}
    today = _today_int()

    # ۱) زنجیرهٔ هر پایه را بکش و watch کن
    rows = fetcher.discover_options(underlyings)
    keep = [r for r in rows if r["underlying"] in targets]
    for r in keep:
        r["watch"] = 1
        r["updated"] = today
    db.upsert_option_series(keep)

    # ۲) ins_code خودِ پایه را از جستجوی تک‌نمادی بگیر و در instruments ذخیره کن
    und_rows = []
    for u in underlyings:
        un = _normalize(u)
        try:
            hits = fetcher.search_instrument(u)
        except Exception as exc:
            logger.warning("  جستجوی پایهٔ «%s» ناموفق: %s", u, exc)
            continue
        match = next((h for h in hits if _normalize(h.get("symbol", "")) == un), None)
        if not match or not (match.get("ins_code") or "").strip():
            logger.warning("  پایهٔ «%s» یافت نشد", u)
            continue
        nm = match.get("full_name", "")
        und_rows.append({"symbol": un, "ins_code": match["ins_code"].strip(),
                         "name": nm, "type": classify_instrument(match["symbol"], nm),
                         "market": "", "watch": 1, "updated": today})
        logger.info("  ✓ پایه %s → %s", un, match["ins_code"].strip())
    if und_rows:
        db.upsert_instruments(und_rows)
    n_chain = len(keep)
    logger.info("watch: %d قرارداد روی %d پایه علامت‌گذاری شد", n_chain, len(und_rows))
    return n_chain


def build_targets(db) -> list[dict]:
    """پایه‌ها + همهٔ آپشن‌های watch‌شده‌شان → فهرستِ {symbol, ins_code}."""
    opts = [o for o in db.get_option_series(watch_only=True)
            if (o.get("ins_code") or "").strip()]
    unds = {o["underlying"] for o in opts if o.get("underlying")}
    # ins_code پایه‌ها از instruments
    instr = {r["symbol"]: (r.get("ins_code") or "").strip()
             for r in db.get_instruments()}
    targets, seen = [], set()
    for u in sorted(unds):
        ins = instr.get(u, "")
        if ins and u not in seen:
            targets.append({"symbol": u, "ins_code": ins}); seen.add(u)
    for o in opts:
        if o["symbol"] not in seen:
            targets.append({"symbol": o["symbol"], "ins_code": o["ins_code"]})
            seen.add(o["symbol"])
    return targets


def main() -> int:
    ap = argparse.ArgumentParser(
        description="جمع‌آوریِ دادهٔ آپشن برای بک‌تستِ آربیتراژ — قابل‌ازسرگیری",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="کشفِ قراردادهای آپشن")
    ap.add_argument("--underlyings", type=str, default=None,
                    help="پایه‌ها با کاما (مثل اهرم,خساپا) — watch + کشفِ زنجیره")
    ap.add_argument("--collect", action="store_true", help="جمع‌آوریِ روزانه+درون‌روز")
    ap.add_argument("--years", type=int, default=3, help="عمقِ روزانه (آپشن‌ها کوتاه‌عمرند)")
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
    logfile = ROOT / "logs" / f"backfill_options_{_today_int()}.log"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(logfile, encoding="utf-8")])
    if not args.verbose:
        logging.getLogger("data_fetcher").setLevel(logging.ERROR)

    # مسیرِ چک‌پوینتِ مستقل برای آپشن (فازها از backfill_stocks بازاستفاده می‌شوند)
    bs.CKPT_PATH = ROOT / "logs" / "backfill_options_checkpoint.json"

    from database import Database
    from data_fetcher import TSETMCFetcher
    try:
        from config import FETCH_WORKERS
    except Exception:
        FETCH_WORKERS = 5

    db = Database(Path(args.db))
    fetcher = TSETMCFetcher()
    workers = max(1, args.workers or FETCH_WORKERS)

    if args.reset_checkpoint:
        try:
            bs.CKPT_PATH.unlink()
        except FileNotFoundError:
            pass
        logger.info("چک‌پوینت پاک شد")

    unders = [s.strip() for s in (args.underlyings or "").split(",") if s.strip()]
    if args.discover:
        discover(db, fetcher, unders)
    if unders:
        watch_underlyings(db, fetcher, unders)

    if not args.collect:
        if not (args.discover or unders):
            ap.print_help()
        return 0

    targets = build_targets(db)
    if not targets:
        logger.error("هدفی برای جمع‌آوری نیست. اول --underlyings و در صورت نیاز "
                     "--discover را اجرا کنید.")
        return 1

    to_date = args.to_date or int((datetime.now().date() - timedelta(days=1)).strftime("%Y%m%d"))
    from_date = args.from_date or _years_ago_int(args.years)
    ckpt = bs._load_ckpt()

    logger.info("═" * 64)
    logger.info("جمع‌آوریِ آپشن | %d هدف (پایه+قرارداد) | پنجره %s→%s | فاز=%s",
                len(targets), _fmt(from_date), _fmt(to_date), args.phase)
    logger.info("لاگ: %s", logfile)
    logger.info("═" * 64)

    do_ticks = args.only in ("all", "ticks")
    do_ob = args.only in ("all", "ob")
    stop = threading.Event()
    t0 = time.time()
    try:
        if args.phase in ("all", "daily"):
            bs.phase_daily(db, fetcher, targets, from_date=from_date, to_date=to_date,
                           years=args.years, force=args.force, workers=workers,
                           ckpt=ckpt, stop=stop)
        if args.phase in ("all", "intraday"):
            bs.phase_intraday(db, fetcher, targets, db_path=Path(args.db),
                              from_date=from_date, to_date=to_date, do_ticks=do_ticks,
                              do_ob=do_ob, force=args.force, workers=workers,
                              ckpt=ckpt, stop=stop)
    except KeyboardInterrupt:
        bs._save_ckpt(ckpt)
        logger.warning("⏸ متوقف شد. پیشرفت ذخیره شد — همین دستور را دوباره بزنید.")
        return 130

    bs._save_ckpt(ckpt)
    logger.info("✅ پایان (%.1f دقیقه). برای ادامه همین دستور را دوباره بزنید.",
                (time.time() - t0) / 60.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
