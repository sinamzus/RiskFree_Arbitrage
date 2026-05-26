#!/usr/bin/env python3
"""
اسکنر آربیتراژ بین‌روزی صندوق‌های درآمد ثابت
Inter-Day Fixed-Income ETF Arbitrage Scanner

HOW THIS WORKS
--------------
Fixed-income ETF funds on the Tehran Stock Exchange (TSE) have two prices:
  • Market price  — fluctuates continuously during trading hours
  • NAV           — published once per day by the fund manager

When market price diverges from NAV a riskless profit is possible:
  DISCOUNT  (market < NAV):  Buy on exchange → redeem at cancel_nav (T+2..T+4)
  PREMIUM   (market > NAV):  Create at issue_nav → sell on exchange (T+1..T+3)

This is INTER-DAY arbitrage: you enter today, position closes over the next
several trading days via creation/redemption.  It is NOT intra-day HFT.

"High frequency" here means scanning the market price every ~15 minutes during
trading hours to catch the moment the discount/premium crosses the threshold.

Usage
-----
    # Single scan, terminal output
    python main.py

    # Monitor every 15 min, terminal output
    python main.py --watch 15

    # Web UI with live chart + history  (http://localhost:5000)
    python main.py --serve

    # Web UI + scan every 15 min
    python main.py --serve --watch 15

    # Discover new funds
    python main.py --discover
"""

import argparse
import logging
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, time as dtime, timedelta, date as date_type

# Force UTF-8 stdout/stderr on Windows so Persian text and emoji work
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

import jdatetime

from config import FIXED_INCOME_ETFS
from data_fetcher import DataAggregator
from arbitrage import scan_all, filter_actionable, ArbitrageOpportunity
from database import Database
from display import (
    print_summary_table,
    print_detailed_report,
    print_market_overview,
    export_csv,
)


LOG_DIR = Path("logs")

# Tehran Stock Exchange trading hours (local Iran time, UTC+3:30)
MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(12, 30)


# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = LOG_DIR / f"run_{timestamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    return log_file


# ─────────────────────────────────────────────────────────────────────────────
#  History bootstrap & incremental update
# ─────────────────────────────────────────────────────────────────────────────

def _days_since(date_int: int) -> int:
    """Return calendar days between *date_int* (YYYYMMDD) and today."""
    dt = datetime.strptime(str(date_int), "%Y%m%d")
    return max(1, (datetime.now() - dt).days)


def update_history(aggregator: DataAggregator, db: Database,
                   force_full: bool = False,
                   fetch_intraday: bool = True,
                   intraday_days: int = 7,
                   delay: float = 0.3) -> None:
    """Bootstrap or incrementally update daily OHLCV + intraday trade history.

    **Daily OHLCV (GetClosingPriceDailyList)**
    - First run (no data in DB):  fetches 365 days for every fund.
    - Subsequent runs:            fetches only the missing days since the last
                                  stored date (plus a 3-day overlap buffer to
                                  catch late-published corrections).
    - *force_full=True*:          always fetches 365 days (full refresh).

    **Intraday ticks (GetTradeHistory)**
    - Fetches the last *intraday_days* trading days for each fund.
    - Only new ticks (dates not yet in DB) are inserted.
    - Set *fetch_intraday=False* to skip tick data (faster on slow connections).

    Parameters
    ----------
    aggregator   : DataAggregator — provides tsetmc fetcher
    db           : Database       — where to persist the data
    force_full   : bool           — force full 365-day re-download
    fetch_intraday: bool          — also download intraday ticks
    intraday_days: int            — how many recent dates to fetch intraday for
    delay        : float          — seconds to sleep between API calls
    """
    logger = logging.getLogger(__name__)
    fetcher = aggregator.tsetmc
    today_int = int(datetime.now().strftime("%Y%m%d"))

    # Collect last N non-weekend dates for intraday use
    def _recent_trading_dates(n: int) -> list[int]:
        dates, d = [], datetime.now()
        while len(dates) < n:
            if d.weekday() not in (3, 4):   # Thu=3, Fri=4 are Iran weekend
                dates.append(int(d.strftime("%Y%m%d")))
            d -= timedelta(days=1)
        return dates

    recent_dates = _recent_trading_dates(intraday_days) if fetch_intraday else []

    total_daily_new   = 0
    total_intraday_new = 0

    for i, fund in enumerate(FIXED_INCOME_ETFS):
        symbol   = fund["symbol"]
        ins_code = fund.get("ins_code", "").strip()
        if not ins_code:
            logger.debug("update_history: skipping %s (no ins_code)", symbol)
            continue

        # ── 1. Daily OHLCV ──────────────────────────────────────────────
        last_date = None if force_full else db.get_last_daily_date(symbol)

        if last_date is None:
            days_n = 365
            reason = "bootstrap"
        else:
            days_since = _days_since(last_date)
            days_n     = min(days_since + 3, 365)  # +3 day overlap buffer
            reason     = f"incremental ({days_since}d since {last_date})"

        logger.info("[%d/%d] %s — daily history %s (n=%d)",
                    i + 1, len(FIXED_INCOME_ETFS), symbol, reason, days_n)

        entries = fetcher.get_historical_daily(ins_code, days=days_n)
        if entries:
            # Only insert entries newer than what we already have
            if last_date:
                entries = [e for e in entries if e["date"] > last_date]
            new_rows = db.save_daily_history(symbol, ins_code, entries)
            total_daily_new += new_rows
            logger.info("  %d new daily rows saved for %s", new_rows, symbol)
        else:
            logger.warning("  No daily history returned for %s", symbol)

        time.sleep(delay)

        # ── 2. Intraday ticks ────────────────────────────────────────────
        if not fetch_intraday:
            continue

        # Build a map of date → tick count already in DB
        # Today is ALWAYS re-fetched (new ticks arrive every minute during session)
        # Past days are skipped only when they already have a meaningful number of ticks
        MIN_TICKS_COMPLETE = 10   # a day with < this is considered incomplete
        intraday_have_map: dict[int, int] = {}
        for d in db.get_intraday_dates(symbol):
            rows = db.get_intraday_trades(symbol, d)
            intraday_have_map[d] = len(rows)

        for date_int in recent_dates:
            if date_int > today_int:
                continue

            existing = intraday_have_map.get(date_int, 0)
            is_today = (date_int == today_int)

            # Skip past days that already have a full set of ticks
            if not is_today and existing >= MIN_TICKS_COMPLETE:
                logger.debug("  Intraday %s: already have %d ticks for %d",
                             symbol, existing, date_int)
                continue

            logger.info("  Fetching intraday ticks for %s on %d (have=%d) ...",
                        symbol, date_int, existing)
            trades = fetcher.get_intraday_trades(ins_code, date_int)
            if trades:
                new_rows = db.save_intraday_trades(
                    symbol, ins_code, date_int, trades
                )
                total_intraday_new += new_rows
                logger.info("  %d new tick rows for %s on %d (total in DB: %d)",
                            new_rows, symbol, date_int, existing + new_rows)
            else:
                logger.debug("  No intraday data for %s on %d", symbol, date_int)

            time.sleep(delay)

    logger.info(
        "update_history complete: %d new daily rows, %d new tick rows",
        total_daily_new, total_intraday_new,
    )
    print(f"\n✅ تاریخچه به‌روز شد — "
          f"{total_daily_new} سطر روزانه جدید، "
          f"{total_intraday_new} سطر درون‌روزی جدید")


# ─────────────────────────────────────────────────────────────────────────────
#  Core scan
# ─────────────────────────────────────────────────────────────────────────────

def run_scan(aggregator: DataAggregator,
             db: Database,
             use_nav: bool = True,
             enrich_intraday: bool = True) -> list[ArbitrageOpportunity]:
    """Fetch data, analyse, save to DB, return opportunities.

    Parameters
    ----------
    enrich_intraday
        If True (default), fetch today's intraday tick data for each fund
        from TSETMC, store in the DB, and attach an
        :class:`~intraday_context.IntraydayContext` to each fund dict before
        signal analysis.  This adds ~0.3 s per fund but gives trend-qualified
        signals (BUY_WEAK / SELL_WEAK when the premium is reversing).
    """
    from intraday_context import compute_intraday_context

    logger = logging.getLogger(__name__)
    scanned_at = datetime.now()
    today = scanned_at.strftime("%Y-%m-%d")
    today_int = int(scanned_at.strftime("%Y%m%d"))

    try:
        fund_data = aggregator.fetch_all(
            use_fipiran_fallback=use_nav,
            nav_cache=db,              # DataAggregator checks DB for today's NAV
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error("Scan failed: %s", e)
        return []

    failed = [f for f in fund_data
              if not f.get("price_data") and not f.get("nav_data")]
    if failed:
        logger.warning("%d funds had no data: %s",
                       len(failed), ", ".join(f["symbol"] for f in failed))

    # ── Intraday enrichment ──────────────────────────────────────────────
    if enrich_intraday:
        from orderbook import compute_tradability
        tsetmc = aggregator.tsetmc
        enriched = 0

        for fd in fund_data:
            sym      = fd.get("symbol", "")
            ins_code = fd.get("ins_code") or tsetmc._ins_code_cache.get(sym, "")
            nav_data = fd.get("nav_data") or {}
            nav      = nav_data.get("cancel_nav", 0)

            if not ins_code:
                continue

            # ── Fetch & store intraday tick data ─────────────────────────
            if nav > 0:
                ticks = tsetmc.get_intraday_trades(ins_code, today_int)
                if ticks:
                    db.save_intraday_trades(sym, ins_code, today_int, ticks)
                    all_ticks = ticks
                else:
                    all_ticks = db.get_intraday_trades(sym, today_int)

                ctx = compute_intraday_context(all_ticks, nav)
                if ctx:
                    fd["intraday_context"] = ctx
                    enriched += 1
                    logger.debug(
                        "intraday %s: %d ticks  trend=%s slope=%+.4f",
                        sym, ctx.tick_count, ctx.trend_label, ctx.trend_slope,
                    )

            # ── Fetch live order book — save snapshot + compute tradability ──
            ob = tsetmc.get_best_limits(ins_code)
            if ob:
                fd["order_book"] = ob

                # Persist to intraday_orderbook so the UI can chart OB history
                time_int = (scanned_at.hour * 10000
                            + scanned_at.minute * 100
                            + scanned_at.second)
                db.save_orderbook_snapshot(
                    sym, ins_code, today_int, time_int, ob, nav=nav,
                )

                # Compute tradability and attach to fund data
                price_data = fd.get("price_data") or {}
                prem_pct = 0.0
                if nav > 0:
                    mkt = price_data.get("last_price") or price_data.get("close_price") or 0
                    if mkt > 0:
                        prem_pct = (mkt - nav) / nav * 100

                direction = "SELL" if prem_pct >= 0 else "BUY"
                nav_for_arb = (
                    nav_data.get("issue_nav", nav)
                    if direction == "SELL"
                    else nav_data.get("cancel_nav", nav)
                )
                td = compute_tradability(direction, nav_for_arb, ob)
                fd["tradability"] = td

        logger.info(
            "Intraday context enriched %d / %d funds",
            enriched, len(fund_data),
        )
    # ────────────────────────────────────────────────────────────────────

    opps = scan_all(fund_data)

    # Save to DB
    if opps:
        db.save_scan(opps, scanned_at)
        # Cache any newly fetched NAVs
        for fd in fund_data:
            if fd.get("nav_data") and fd["nav_data"].get("nav_per_unit", 0) > 0:
                db.cache_nav(fd["symbol"], fd["nav_data"], today)

    return opps


# ─────────────────────────────────────────────────────────────────────────────
#  Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def print_scan_header(scan_number: int | None = None):
    now = datetime.now()
    jalali_now = jdatetime.datetime.fromgregorian(datetime=now)
    ts    = jalali_now.strftime("%Y/%m/%d %H:%M:%S")
    label = f"اسکن شماره {scan_number}" if scan_number else "اسکن"
    print(f"\n{'═' * 100}")
    print(f"  {label} — {ts}")
    print(f"{'═' * 100}")


def print_delta(prev: list[ArbitrageOpportunity],
                curr: list[ArbitrageOpportunity]) -> None:
    if not prev:
        return
    prev_map = {o.symbol: o for o in prev if o.actionable}
    curr_map = {o.symbol: o for o in curr if o.actionable}

    new_syms  = set(curr_map) - set(prev_map)
    gone_syms = set(prev_map) - set(curr_map)
    changed   = []
    for sym in set(curr_map) & set(prev_map):
        delta = curr_map[sym].premium_discount_pct - prev_map[sym].premium_discount_pct
        if abs(delta) >= 0.10:
            changed.append((curr_map[sym], prev_map[sym].premium_discount_pct))

    if not new_syms and not gone_syms and not changed:
        print("  ↔  بدون تغییر قابل توجه نسبت به اسکن قبلی")
        return
    for sym in new_syms:
        o = curr_map[sym]
        print(f"  🆕 فرصت جدید: {o.symbol}  {o.signal}  "
              f"({o.premium_discount_pct:+.2f}%  سود {o.net_profit_pct:+.2f}%)")
    for sym in gone_syms:
        print(f"  ❌ فرصت بسته شد: {sym}")
    for o, old_pd in changed:
        arrow = "▲" if o.premium_discount_pct > old_pd else "▼"
        print(f"  {arrow} تغییر: {o.symbol}  {old_pd:+.2f}% → {o.premium_discount_pct:+.2f}%")


def is_market_open() -> bool:
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def seconds_until_open() -> int:
    now    = datetime.now()
    target = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                         second=0, microsecond=0)
    if now.time() > MARKET_CLOSE:
        target += timedelta(days=1)
    return max(0, int((target - now).total_seconds()))


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "اسکنر آربیتراژ بین‌روزی صندوق‌های درآمد ثابت\n"
            "Inter-day: compares live market price to daily NAV "
            "to find creation/redemption opportunities."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="داشبورد وب را راه‌اندازی کن (http://localhost:PORT)",
    )
    parser.add_argument(
        "--port", type=int, default=5000,
        help="پورت وب سرور (پیش‌فرض: 5000)",
    )
    parser.add_argument(
        "--watch", metavar="MINUTES", type=int, default=0,
        help="حالت مانیتور: هر MINUTES دقیقه اسکن کن (0 = یک‌بار)",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="فقط جدول خلاصه (بدون گزارش تفصیلی)",
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="خروجی CSV ذخیره شود",
    )
    parser.add_argument(
        "--csv-path", default="arbitrage_results.csv",
        help="مسیر فایل CSV",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="جستجو برای صندوق‌های جدید",
    )
    parser.add_argument(
        "--no-fipiran", action="store_true",
        help="NAV از منبع خارجی دریافت نشود",
    )
    parser.add_argument(
        "--no-market-check", action="store_true",
        help="بدون توجه به ساعت بازار اسکن شود",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="نمایش لاگ DEBUG",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="تاخیر بین درخواست‌های HTTP (ثانیه)",
    )
    parser.add_argument(
        "--bootstrap", action="store_true",
        help=(
            "دانلود کامل ۳۶۵ روز تاریخچه روزانه + داده درون‌روزی هفت روز اخیر "
            "(در اولین اجرا به‌صورت خودکار انجام می‌شود)"
        ),
    )
    parser.add_argument(
        "--no-intraday", action="store_true",
        help="در bootstrap/update تیک‌های درون‌روزی دانلود نشوند",
    )
    parser.add_argument(
        "--intraday-days", type=int, default=7,
        help="تعداد روزهای اخیر برای دریافت داده تیک‌به‌تیک (پیش‌فرض: 7)",
    )
    parser.add_argument(
        "--intraday-status", action="store_true",
        help="نمایش وضعیت داده تیک‌به‌تیک در DB و خروج",
    )

    args = parser.parse_args()
    log_file = setup_logging(args.verbose)
    logger   = logging.getLogger(__name__)
    logger.info("Log file: %s", log_file.resolve())

    db         = Database()
    aggregator = DataAggregator()
    use_nav    = not args.no_fipiran

    # ── intraday status report ──────────────────────────────────────────────
    if args.intraday_status:
        from config import FIXED_INCOME_ETFS as FUNDS
        today_int = int(datetime.now().strftime("%Y%m%d"))
        print()
        print("=" * 72)
        print("  وضعیت داده درون‌روزی در DB")
        print("=" * 72)
        print(f"  {'نماد':12s}  {'آخرین تاریخ':12s}  {'تیک آن روز':>12s}  {'کل تاریخ':>8s}")
        print("  " + "─" * 55)
        for fund in FUNDS:
            sym   = fund["symbol"]
            dates = db.get_intraday_dates(sym)
            if dates:
                last  = dates[-1]
                ticks = db.get_intraday_trades(sym, last)
                mark  = " ← امروز" if last == today_int else ""
                print(f"  {sym:12s}  {last:12d}  {len(ticks):>12,}  {len(dates):>8d}{mark}")
            else:
                print(f"  {sym:12s}  {'—':12s}  {'—':>12s}  {'—':>8s}")
        print()
        total = sum(
            len(db.get_intraday_trades(f["symbol"], d))
            for f in FUNDS
            for d in db.get_intraday_dates(f["symbol"])
        )
        print(f"  مجموع تیک در DB: {total:,}")
        print()
        return

    # ── bootstrap / incremental history update ─────────────────────────────
    fetch_intraday = not args.no_intraday

    if args.bootstrap:
        # Explicit --bootstrap: force full 365-day re-download
        print("\n📥 در حال دانلود کامل تاریخچه (۳۶۵ روز) ...")
        update_history(
            aggregator, db,
            force_full=True,
            fetch_intraday=fetch_intraday,
            intraday_days=args.intraday_days,
            delay=args.delay,
        )
        return
    else:
        # Auto-bootstrap: run incremental update on every startup.
        # This is cheap when data is fresh (fetches only the missing days).
        logger.info("Running incremental history update ...")
        update_history(
            aggregator, db,
            force_full=False,
            fetch_intraday=fetch_intraday,
            intraday_days=args.intraday_days,
            delay=args.delay * 0.5,   # lighter delay for background update
        )

    # ── discover mode ──────────────────────────────────────────────────────
    if args.discover:
        new_funds = aggregator.discover_new_funds()
        if new_funds:
            print(f"\n{len(new_funds)} صندوق جدید یافت شد:")
            for f in new_funds:
                print(f"  {f['symbol']:>10}  |  {f['full_name']}  |  {f['ins_code']}")
        else:
            print("صندوق جدیدی یافت نشد.")
        return

    # ── web server ─────────────────────────────────────────────────────────
    flask_app = None
    if args.serve:
        try:
            from web_server import run_server
        except ImportError:
            print("❌ Flask نصب نشده. دستور: pip install flask")
            sys.exit(1)

        def _scan_callback():
            """Called by POST /api/scan from the browser."""
            opps = run_scan(aggregator, db, use_nav)
            if flask_app and opps:
                _push_scan(flask_app, opps)

        flask_app = run_server(db, host="0.0.0.0", port=args.port,
                               scan_callback=_scan_callback)
        print(f"\n🌐 داشبورد وب:  http://localhost:{args.port}")
        print(f"   Ctrl+C برای توقف\n")

    # ── single scan (no watch) ─────────────────────────────────────────────
    if args.watch == 0 and not args.serve:
        print("\n⏳ در حال اسکن ...")
        try:
            opps = run_scan(aggregator, db, use_nav)
        except KeyboardInterrupt:
            print("\n❌ لغو شد.")
            sys.exit(1)

        if not opps:
            print("\n❌ داده‌ای دریافت نشد.")
            sys.exit(1)

        print_scan_header()
        print_summary_table(opps)
        print_market_overview(opps)
        if not args.summary:
            print_detailed_report(opps)
        if args.csv:
            export_csv(opps, args.csv_path)

        actionable = filter_actionable(opps)
        if actionable:
            print(f"\n✅ {len(actionable)} فرصت آربیتراژ بین‌روزی قابل اجرا شناسایی شد.")
        else:
            print("\n📊 در حال حاضر فرصت قابل توجهی وجود ندارد.")
        print()
        return

    # ── watch / serve loop ─────────────────────────────────────────────────
    interval_sec = (args.watch or 15) * 60
    if args.serve and args.watch == 0:
        # serve-only: scan once at startup then wait for POST /api/scan
        print("⏳ اسکن اولیه ...")
        opps = run_scan(aggregator, db, use_nav)
        if flask_app and opps:
            _push_scan(flask_app, opps)
        print("✅ اسکن اولیه تکمیل شد — منتظر اسکن بعدی از طریق UI یا --watch")
        # Keep main thread alive
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n👋 متوقف شد.")
        return

    print(f"\n🔭 حالت مانیتور آربیتراژ بین‌روزی فعال شد")
    if args.serve:
        print(f"   داشبورد وب: http://localhost:{args.port}")
    print(f"   هر {args.watch} دقیقه اسکن")
    print(f"   بازار: {MARKET_OPEN.strftime('%H:%M')}–{MARKET_CLOSE.strftime('%H:%M')}  "
          f"{'(بدون بررسی ساعت)' if args.no_market_check else ''}")
    print(f"   Ctrl+C برای توقف\n")

    prev_opps: list[ArbitrageOpportunity] = []
    scan_count = 0

    try:
        while True:
            if not args.no_market_check and not is_market_open():
                wait_sec = seconds_until_open()
                h, m = divmod(wait_sec // 60, 60)
                print(f"\n🕐 بازار بسته — تا باز شدن {h}h {m}m ...")
                try:
                    time.sleep(min(wait_sec, 300))
                except KeyboardInterrupt:
                    break
                continue

            scan_count += 1
            print_scan_header(scan_count)

            opps = run_scan(aggregator, db, use_nav)

            if not opps:
                print("  ⚠️  داده‌ای دریافت نشد")
            else:
                print_summary_table(opps)
                actionable = filter_actionable(opps)
                if actionable:
                    print(f"\n  ✅ {len(actionable)} فرصت قابل اجرا:")
                    for o in actionable:
                        arrow = "🟢" if o.signal == "BUY" else "🔴"
                        print(f"     {arrow} {o.symbol:>8}  "
                              f"صرف/تخفیف: {o.premium_discount_pct:+.2f}%  "
                              f"سود: {o.net_profit_pct:+.2f}%  "
                              f"حجم: {o.volume:,}")
                else:
                    print("\n  📊 هیچ فرصتی بالاتر از آستانه نیست.")

                if scan_count > 1:
                    print("\n  📈 تغییرات:")
                    print_delta(prev_opps, opps)

                if not args.summary and actionable:
                    print_detailed_report(opps)

                if args.csv:
                    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
                    csv = args.csv_path.replace(".csv", f"_{ts}.csv")
                    export_csv(opps, csv)

                # Push to SSE (web UI)
                if flask_app:
                    _push_scan(flask_app, opps)

                prev_opps = opps

            next_at = datetime.now() + timedelta(seconds=interval_sec)
            print(f"\n  ⏱  اسکن بعدی: {next_at.strftime('%H:%M:%S')}")
            time.sleep(interval_sec)

    except KeyboardInterrupt:
        print("\n\n👋 مانیتور متوقف شد.")
        if prev_opps:
            n = len(filter_actionable(prev_opps))
            print(f"   آخرین: {len(prev_opps)} صندوق، {n} فرصت قابل اجرا")
        print()


def _push_scan(flask_app, opps: list[ArbitrageOpportunity]):
    """Push a scan_complete event to all SSE clients."""
    try:
        payload = {
            "type":      "scan_complete",
            "timestamp": datetime.utcnow().isoformat(),
            "funds": [
                {
                    "symbol":               o.symbol,
                    "name":                 o.name,
                    "market_price":         o.market_price,
                    "nav":                  o.nav,
                    "cancel_nav":           o.cancel_nav,
                    "premium_discount_pct": o.premium_discount_pct,
                    "net_profit_pct":       o.net_profit_pct,
                    "volume":               o.volume,
                    "signal":               o.signal,
                    "actionable":           o.actionable,
                    "best_bid":             o.best_bid,
                    "best_ask":             o.best_ask,
                    "spread_pct":           o.spread_pct,
                    "ob_score":             o.ob_score,
                    "tradable":             o.tradable,
                    "tradable_volume":      o.tradable_volume,
                    "tradability_reason":   o.tradability_reason,
                    "intraday_trend":       getattr(o.intraday, "trend_label", "") if o.intraday else "",
                    "trend_slope":          getattr(o.intraday, "trend_slope", 0) if o.intraday else 0,
                    "tick_count_today":     getattr(o.intraday, "tick_count", 0) if o.intraday else 0,
                    "vwap":                 getattr(o.intraday, "vwap", 0) if o.intraday else 0,
                    "vwap_premium_pct":     getattr(o.intraday, "vwap_premium_pct", 0) if o.intraday else 0,
                }
                for o in opps
            ],
        }
        flask_app.push_to_sse(payload)
    except Exception as e:
        logging.getLogger(__name__).debug("SSE push failed: %s", e)


if __name__ == "__main__":
    main()
