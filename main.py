#!/usr/bin/env python3
"""
اسکنر آربیتراژ بین‌روزی صندوق‌های درآمد ثابت
Inter-Day Fixed-Income ETF Arbitrage Scanner

HOW THIS WORKS
--------------
Fixed-income ETF funds on the Tehran Stock Exchange (TSE) have two prices:
  • Market price  — fluctuates continuously during trading hours
  • NAV           — calculated once per day by the fund manager and published
                    after market close (or early the next morning)

When market price diverges from NAV a riskless profit is possible:

  DISCOUNT  (market < NAV):
    Buy units on the exchange → submit redemption request to fund
    → receive cash at cancel_nav.  Settlement: T+2 to T+4 days.

  PREMIUM   (market > NAV):
    Submit creation request at issue_nav → receive new units
    → sell on the exchange.  Settlement: T+1 to T+3 days.

This is **inter-day** arbitrage: you enter today, the position closes over the
next several trading days.  It is *not* intraday momentum/HFT.

"High frequency" here means scanning the market price multiple times during
the trading session (e.g. every 15 minutes) to catch the moment the
discount/premium crosses the minimum threshold.

Usage:
    python main.py                    # Single scan, full report
    python main.py --summary          # Single scan, table only
    python main.py --watch 15         # Monitor every 15 minutes
    python main.py --watch 15 --csv   # Monitor + export CSV on each scan
    python main.py --discover         # Search for new funds not in config
    python main.py --csv              # Single scan + export CSV
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, time as dtime

import jdatetime

from data_fetcher import DataAggregator
from arbitrage import scan_all, filter_actionable, ArbitrageOpportunity
from display import (
    print_summary_table,
    print_detailed_report,
    print_market_overview,
    export_csv,
)


LOG_DIR = Path("logs")

# Tehran Stock Exchange trading hours (local time, UTC+3:30)
MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(12, 30)


def setup_logging(verbose: bool = False) -> Path:
    """Configure logging to both console and a timestamped log file."""
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"run_{timestamp}.log"

    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    return log_file


# ─────────────────────────────────────────────────────────────────────────────
#  Single-scan helpers
# ─────────────────────────────────────────────────────────────────────────────

def run_scan(aggregator: DataAggregator,
             use_fipiran: bool = True) -> list[ArbitrageOpportunity]:
    """Fetch data and return opportunity list (empty list on failure)."""
    logger = logging.getLogger(__name__)
    try:
        fund_data = aggregator.fetch_all(use_fipiran_fallback=use_fipiran)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error("Scan failed: %s", e)
        return []

    failed = [f for f in fund_data if not f.get("price_data") and not f.get("nav_data")]
    if failed:
        logger.warning("%d funds had no data: %s",
                       len(failed), ", ".join(f["symbol"] for f in failed))

    return scan_all(fund_data)


def print_scan_header(scan_number: int | None = None):
    now = datetime.now()
    jalali_now = jdatetime.datetime.fromgregorian(datetime=now)
    ts = jalali_now.strftime("%Y/%m/%d %H:%M:%S")
    label = f"اسکن شماره {scan_number}" if scan_number else "اسکن"
    print(f"\n{'═' * 100}")
    print(f"  {label} — {ts}")
    print(f"{'═' * 100}")


# ─────────────────────────────────────────────────────────────────────────────
#  Watch-mode helpers
# ─────────────────────────────────────────────────────────────────────────────

def _opportunity_key(o: ArbitrageOpportunity) -> str:
    return f"{o.symbol}:{o.signal}"


def print_delta(prev: list[ArbitrageOpportunity],
                curr: list[ArbitrageOpportunity]) -> None:
    """Print a brief diff between two consecutive scans.

    Shows:
    • New opportunities that just became actionable
    • Opportunities that just disappeared
    • Large moves (>0.1 pp change in premium/discount)
    """
    if not prev:
        return

    prev_map = {_opportunity_key(o): o for o in prev if o.actionable}
    curr_map = {_opportunity_key(o): o for o in curr if o.actionable}

    new_keys  = set(curr_map) - set(prev_map)
    gone_keys = set(prev_map) - set(curr_map)

    # Opportunities with meaningful premium/discount change
    changed = []
    for key in set(curr_map) & set(prev_map):
        old_pd = prev_map[key].premium_discount_pct
        new_pd = curr_map[key].premium_discount_pct
        if abs(new_pd - old_pd) >= 0.10:
            changed.append((curr_map[key], old_pd))

    if not new_keys and not gone_keys and not changed:
        print("  ↔  وضعیت نسبت به اسکن قبلی بدون تغییر قابل توجه است.")
        return

    for key in new_keys:
        o = curr_map[key]
        print(f"  🆕 فرصت جدید: {o.symbol} — {o.signal}  "
              f"({o.premium_discount_pct:+.2f}%  سود خالص {o.net_profit_pct:+.2f}%)")

    for key in gone_keys:
        o = prev_map[key]
        print(f"  ❌ فرصت بسته شد: {o.symbol} — {o.signal}")

    for o, old_pd in changed:
        direction = "▲" if o.premium_discount_pct > old_pd else "▼"
        print(f"  {direction}  تغییر: {o.symbol}  "
              f"{old_pd:+.2f}% → {o.premium_discount_pct:+.2f}%")


def is_market_open() -> bool:
    """Return True if current local time is within TSE trading hours."""
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def seconds_until_open() -> int:
    """Seconds until MARKET_OPEN (same day or tomorrow if already past close)."""
    now = datetime.now()
    target = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                         second=0, microsecond=0)
    if now.time() > MARKET_CLOSE:
        # After today's close — next open is tomorrow
        from datetime import timedelta
        target += timedelta(days=1)
    delta = (target - now).total_seconds()
    return max(0, int(delta))


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "اسکنر آربیتراژ بین‌روزی صندوق‌های درآمد ثابت\n"
            "Inter-day arbitrage: compares live market price to the fund's "
            "published daily NAV to find creation/redemption opportunities."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--watch", metavar="MINUTES", type=int, default=0,
        help=(
            "حالت مانیتور: هر MINUTES دقیقه اسکن مجدد (پیش‌فرض: خاموش).\n"
            "مثال: --watch 15  →  هر ۱۵ دقیقه اسکن کن."
        ),
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="فقط جدول خلاصه نمایش داده شود (بدون گزارش تفصیلی)",
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="خروجی CSV ذخیره شود (در حالت watch پس از هر اسکن)",
    )
    parser.add_argument(
        "--csv-path", default="arbitrage_results.csv",
        help="مسیر فایل CSV (پیش‌فرض: arbitrage_results.csv)",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="جستجو برای صندوق‌های جدید درآمد ثابت",
    )
    parser.add_argument(
        "--no-fipiran", action="store_true",
        help="از FIPIRAN/Rahavard به عنوان منبع NAV استفاده نشود",
    )
    parser.add_argument(
        "--no-market-check", action="store_true",
        help="بدون توجه به ساعت بازار اسکن شود (برای تست)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="نمایش لاگ DEBUG در کنسول",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="تاخیر بین درخواست‌های HTTP (ثانیه، پیش‌فرض 0.5)",
    )

    args = parser.parse_args()
    log_file = setup_logging(args.verbose)

    logger = logging.getLogger(__name__)
    logger.info("Log file: %s", log_file.resolve())
    print(f"📄 لاگ کامل در: {log_file.resolve()}\n")

    aggregator = DataAggregator()

    # ── discover mode ──────────────────────────────────────────────────────
    if args.discover:
        logger.info("Searching for new fixed-income funds...")
        new_funds = aggregator.discover_new_funds()
        if new_funds:
            print(f"\n{len(new_funds)} صندوق جدید یافت شد:")
            for f in new_funds:
                print(f"  {f['symbol']:>10}  |  {f['full_name']}  |  ins_code: {f['ins_code']}")
        else:
            print("صندوق جدیدی یافت نشد.")
        return

    use_nav = not args.no_fipiran

    # ── single-scan mode ───────────────────────────────────────────────────
    if args.watch == 0:
        print("\n⏳ در حال دریافت داده‌ها ...")
        print("   (اطمینان حاصل کنید که به اینترنت ایران دسترسی دارید)\n")

        try:
            opportunities = run_scan(aggregator, use_nav)
        except KeyboardInterrupt:
            print("\n❌ عملیات لغو شد.")
            sys.exit(1)

        if not opportunities:
            print("\n❌ داده‌ای دریافت نشد یا هیچ صندوقی قابل تحلیل نبود.")
            sys.exit(1)

        print_summary_table(opportunities)
        print_market_overview(opportunities)

        if not args.summary:
            print_detailed_report(opportunities)

        if args.csv:
            export_csv(opportunities, args.csv_path)

        actionable = filter_actionable(opportunities)
        if actionable:
            print(f"\n✅ {len(actionable)} فرصت آربیتراژ بین‌روزی قابل اجرا شناسایی شد.")
        else:
            print("\n📊 در حال حاضر فرصت آربیتراژ قابل توجهی وجود ندارد.")
        print()
        return

    # ── watch mode ─────────────────────────────────────────────────────────
    interval_sec = args.watch * 60
    print(f"\n🔭 حالت مانیتور آربیتراژ بین‌روزی فعال شد")
    print(f"   هر {args.watch} دقیقه اسکن می‌شود")
    print(f"   ساعت بازار بورس: {MARKET_OPEN.strftime('%H:%M')} تا {MARKET_CLOSE.strftime('%H:%M')}")
    print(f"   برای توقف: Ctrl+C\n")

    prev_opportunities: list[ArbitrageOpportunity] = []
    scan_count = 0

    try:
        while True:
            # Wait for market hours unless --no-market-check
            if not args.no_market_check and not is_market_open():
                wait_sec = seconds_until_open()
                wait_min = wait_sec // 60
                print(f"\n🕐 بازار بسته است. "
                      f"تا باز شدن بازار {wait_min} دقیقه ({wait_sec//3600}h {(wait_sec%3600)//60}m) صبر می‌شود ...")
                try:
                    time.sleep(min(wait_sec, 300))   # sleep in 5-min chunks
                except KeyboardInterrupt:
                    break
                continue

            scan_count += 1
            print_scan_header(scan_count)

            opportunities = run_scan(aggregator, use_nav)

            if not opportunities:
                print("  ⚠️  داده‌ای دریافت نشد — اسکن بعدی در "
                      f"{args.watch} دقیقه دیگر.")
            else:
                print_summary_table(opportunities)

                actionable = filter_actionable(opportunities)
                if actionable:
                    print(f"\n  ✅ {len(actionable)} فرصت قابل اجرا:")
                    for o in actionable:
                        arrow = "🟢" if o.signal == "BUY" else "🔴"
                        print(f"     {arrow} {o.symbol:>8}  |  "
                              f"صرف/تخفیف: {o.premium_discount_pct:+.2f}%  |  "
                              f"سود خالص: {o.net_profit_pct:+.2f}%  |  "
                              f"حجم: {o.volume:,}")
                else:
                    print("\n  📊 هیچ فرصتی بالاتر از آستانه نیست.")

                if scan_count > 1:
                    print("\n  📈 تغییرات نسبت به اسکن قبلی:")
                    print_delta(prev_opportunities, opportunities)

                if not args.summary and actionable:
                    print()
                    print_detailed_report(opportunities)

                if args.csv:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    csv_path = args.csv_path.replace(
                        ".csv", f"_{ts}.csv"
                    ) if ".csv" in args.csv_path else f"{args.csv_path}_{ts}.csv"
                    export_csv(opportunities, csv_path)

                prev_opportunities = opportunities

            # Sleep until next scan
            next_at = datetime.now().replace(microsecond=0)
            from datetime import timedelta
            next_at = next_at + timedelta(seconds=interval_sec)
            print(f"\n  ⏱  اسکن بعدی: {next_at.strftime('%H:%M:%S')}")
            time.sleep(interval_sec)

    except KeyboardInterrupt:
        print("\n\n👋 مانیتور متوقف شد.")
        if prev_opportunities:
            actionable = filter_actionable(prev_opportunities)
            print(f"   آخرین وضعیت: {len(prev_opportunities)} صندوق بررسی شد، "
                  f"{len(actionable)} فرصت قابل اجرا")
        print()


if __name__ == "__main__":
    main()
