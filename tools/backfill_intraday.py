#!/usr/bin/env python3
"""
بک‌فیل دیتای درون‌روز TSETMC
=================================
داده snapshot قیمت (≈ پر-ثانیه) و حقیقی/حقوقی روزانه را برای
N روز اخیر از TSETMC می‌گیرد و در DB ذخیره می‌کند.

جدول‌ها:
  intraday_price_history   ← snapshots قیمت/حجم تجمیعی
  client_type_daily        ← خرید/فروش حقیقی و حقوقی روزانه

اجرا:
    # ۱۴ روز اخیر (پیش‌فرض)
    python tools/backfill_intraday.py

    # ۳۰ روز اخیر، فقط برخی نمادها
    python tools/backfill_intraday.py --days 30 --symbol کیان,آکورد

    # تنها یک تاریخ مشخص
    python tools/backfill_intraday.py --date 20260526

    # فقط یکی از دو نوع داده
    python tools/backfill_intraday.py --only price
    python tools/backfill_intraday.py --only ct
"""

import sys, argparse, logging, time, sqlite3
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_intraday")


def _recent_trading_dates(n: int) -> list[int]:
    """Return last n weekdays as YYYYMMDD ints, skipping Thu+Fri (Iran weekend)."""
    today = datetime.now().date()
    dates = []
    d = today - timedelta(days=1)
    while len(dates) < n:
        # Iran weekend: Thursday(3) + Friday(4) closed
        if d.weekday() not in (3, 4):
            dates.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return sorted(dates)


def backfill(days: int = 14, specific_date: int = None,
             symbol_filter: list[str] = None,
             only: str = "both", force: bool = False):
    from database import Database
    from data_fetcher import TSETMCFetcher

    db_path = ROOT / "data" / "arbitrage.db"
    db = Database(db_path)
    tsetmc = TSETMCFetcher()

    # Load funds from daily_history
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    funds = conn.execute(
        "SELECT DISTINCT symbol, ins_code FROM daily_history "
        "WHERE ins_code != '' ORDER BY symbol"
    ).fetchall()
    conn.close()

    if symbol_filter:
        funds = [f for f in funds if f["symbol"] in symbol_filter]

    if not funds:
        logger.error("No funds found. Run bootstrap first.")
        return

    target_dates = [specific_date] if specific_date else _recent_trading_dates(days)
    logger.info("Backfilling intraday %s for %d funds × %d dates",
                only.upper(), len(funds), len(target_dates))

    stats = {"price_new": 0, "price_skip": 0, "ct_new": 0, "ct_skip": 0,
             "fail": 0}

    for date_int in target_dates:
        date_label = f"{str(date_int)[:4]}-{str(date_int)[4:6]}-{str(date_int)[6:]}"
        logger.info("── Date %s ──────────────────────────────────────", date_label)

        for fund in funds:
            sym, ins_code = fund["symbol"], fund["ins_code"]
            if not ins_code:
                continue

            # ── Intraday price snapshots ────────────────────────────────────
            if only in ("both", "price"):
                already = 0
                if not force:
                    c = sqlite3.connect(db_path)
                    already = c.execute(
                        "SELECT COUNT(*) FROM intraday_price_history "
                        "WHERE symbol=? AND date=?",
                        (sym, date_int)
                    ).fetchone()[0]
                    c.close()

                if already > 0 and not force:
                    stats["price_skip"] += already
                else:
                    try:
                        snaps = tsetmc.get_intraday_price_history(ins_code, date_int)
                        n = db.save_intraday_price_history(sym, ins_code, date_int, snaps)
                        stats["price_new"] += n
                        if n > 0:
                            logger.info("  %-14s  %s  PRICE  fetched=%d  new=%d",
                                        sym, date_label, len(snaps), n)
                    except Exception as e:
                        stats["fail"] += 1
                        logger.warning("  %s %s PRICE fail: %s", sym, date_label, e)

                time.sleep(0.12)

            # ── Client type (حقیقی/حقوقی) ───────────────────────────────────
            if only in ("both", "ct"):
                exists = 0
                if not force:
                    c = sqlite3.connect(db_path)
                    exists = c.execute(
                        "SELECT COUNT(*) FROM client_type_daily "
                        "WHERE symbol=? AND date=?",
                        (sym, date_int)
                    ).fetchone()[0]
                    c.close()

                if exists > 0 and not force:
                    stats["ct_skip"] += 1
                else:
                    try:
                        ct = tsetmc.get_client_type(ins_code, date_int)
                        if ct:
                            db.save_client_type(sym, ins_code, date_int, ct)
                            stats["ct_new"] += 1
                            logger.info("  %-14s  %s  CT  I_buy=%d  N_buy=%d",
                                        sym, date_label,
                                        ct["buy_i_vol"], ct["buy_n_vol"])
                    except Exception as e:
                        stats["fail"] += 1
                        logger.warning("  %s %s CT fail: %s", sym, date_label, e)

                time.sleep(0.12)

    logger.info("═" * 60)
    logger.info("Done. price_new=%d  price_skip=%d  ct_new=%d  ct_skip=%d  fail=%d",
                stats["price_new"], stats["price_skip"],
                stats["ct_new"], stats["ct_skip"], stats["fail"])


def main():
    ap = argparse.ArgumentParser(description="Backfill TSETMC intraday data")
    ap.add_argument("--days",   type=int, default=14)
    ap.add_argument("--date",   type=int, default=None,
                    help="Single date YYYYMMDD (overrides --days)")
    ap.add_argument("--symbol", type=str, default=None,
                    help="Comma-separated symbols to filter (default: all)")
    ap.add_argument("--only",   choices=["both", "price", "ct"], default="both",
                    help="Which dataset to backfill (default: both)")
    ap.add_argument("--force",  action="store_true",
                    help="Re-fetch even if data exists")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbol.split(",")] if args.symbol else None
    backfill(days=args.days, specific_date=args.date,
             symbol_filter=syms, only=args.only, force=args.force)


if __name__ == "__main__":
    main()
