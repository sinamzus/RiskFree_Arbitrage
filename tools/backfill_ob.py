#!/usr/bin/env python3
"""
بک‌فیل اردربوک تاریخی — Historical OB Backfill
==================================================
داده اردربوک لحظه‌ای (per-minute) را از API تابلوی TSETMC می‌گیرد
و در جدول intraday_orderbook ذخیره می‌کند.

این ابزار برای پر کردن روزهایی است که اسکنر در حال اجرا نبوده
یا جدول intraday_orderbook هنوز وجود نداشته.

اجرا:
    # بک‌فیل ۱۴ روز اخیر (پیش‌فرض)
    python tools/backfill_ob.py

    # بک‌فیل ۳۰ روز اخیر
    python tools/backfill_ob.py --days 30

    # تنها یک تاریخ مشخص
    python tools/backfill_ob.py --date 20260520

    # بک‌فیل و نادیده گرفتن داده‌های موجود
    python tools/backfill_ob.py --force
"""

import sys
import argparse
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_ob")


def _recent_gregorian_dates(n: int) -> list[int]:
    """Return the last n calendar dates as YYYYMMDD integers (excluding today)."""
    today = datetime.now().date()
    dates = []
    d = today - timedelta(days=1)
    while len(dates) < n:
        # Skip Fridays (weekday=4 in Python) — TSE is closed
        if d.weekday() != 4:
            dates.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return dates


def backfill(days: int = 14, specific_date: int = None, force: bool = False):
    from database import Database
    from data_fetcher import TSETMCFetcher, DataAggregator

    db = Database(ROOT / "data" / "arbitrage.db")
    tsetmc = TSETMCFetcher()

    # Determine target dates
    if specific_date:
        target_dates = [specific_date]
    else:
        target_dates = _recent_gregorian_dates(days)

    # Get all known (symbol, ins_code) pairs from daily_history
    import sqlite3
    conn = sqlite3.connect(ROOT / "data" / "arbitrage.db")
    conn.row_factory = sqlite3.Row
    funds = conn.execute(
        "SELECT DISTINCT symbol, ins_code FROM daily_history WHERE ins_code != '' ORDER BY symbol"
    ).fetchall()
    conn.close()

    if not funds:
        # fallback: load from config
        try:
            from config import FUNDS
            funds = [{"symbol": f["symbol"], "ins_code": f.get("ins_code", "")}
                     for f in FUNDS if f.get("ins_code")]
        except Exception:
            logger.error("No funds found in daily_history or config. Run --bootstrap first.")
            return

    logger.info("Backfilling OB for %d funds × %d dates", len(funds), len(target_dates))

    total_inserted = 0
    total_skipped  = 0
    total_failed   = 0

    for date_int in sorted(target_dates):
        date_str = str(date_int)
        date_label = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        logger.info("── Date %s ──────────────────────────────────────", date_label)

        for fund in funds:
            sym      = fund["symbol"] if hasattr(fund, "__getitem__") else fund[0]
            ins_code = fund["ins_code"] if hasattr(fund, "__getitem__") else fund[1]
            if not ins_code:
                continue

            # Skip if already have data for this (symbol, date) and not forced
            if not force:
                import sqlite3 as _sq
                _conn = _sq.connect(ROOT / "data" / "arbitrage.db")
                existing = _conn.execute(
                    "SELECT COUNT(*) FROM intraday_orderbook WHERE symbol=? AND date=?",
                    (sym, date_int)
                ).fetchone()[0]
                _conn.close()
                if existing > 0:
                    logger.debug("  %s %s — %d snaps already, skip", sym, date_label, existing)
                    total_skipped += existing
                    continue

            # Fetch historical OB deltas and reconstruct per-minute snapshots
            try:
                snapshots = tsetmc.get_best_limits_history(ins_code, date_int)
            except Exception as e:
                logger.warning("  %s %s — fetch failed: %s", sym, date_label, e)
                total_failed += 1
                time.sleep(0.5)
                continue

            if not snapshots:
                logger.debug("  %s %s — no OB history available", sym, date_label)
                total_failed += 1
                time.sleep(0.2)
                continue

            inserted = 0
            for snap in snapshots:
                t = snap.get("time", 0)
                bids = snap.get("bids", [])
                asks = snap.get("asks", [])
                ob = {"bids": bids, "asks": asks}
                ok = db.save_orderbook_snapshot(sym, ins_code, date_int, t, ob, nav=0.0)
                if ok:
                    inserted += 1

            total_inserted += inserted
            logger.info("  %-14s  %s  fetched=%d  new=%d",
                        sym, date_label, len(snapshots), inserted)
            time.sleep(0.15)   # polite delay between funds

    logger.info("═" * 60)
    logger.info("Done. inserted=%d  skipped=%d  failed=%d",
                total_inserted, total_skipped, total_failed)


def main():
    ap = argparse.ArgumentParser(description="Backfill historical OB snapshots from TSETMC")
    ap.add_argument("--days",  type=int,  default=14,
                    help="Number of recent days to backfill (default 14)")
    ap.add_argument("--date",  type=int,  default=None,
                    help="Single date to backfill YYYYMMDD (overrides --days)")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if snapshots already exist for that date")
    args = ap.parse_args()

    backfill(days=args.days, specific_date=args.date, force=args.force)


if __name__ == "__main__":
    main()
