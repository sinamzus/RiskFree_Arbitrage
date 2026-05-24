#!/usr/bin/env python3
"""
Fixed-Income ETF Arbitrage Scanner
اسکنر آربیتراژ صندوق‌های سرمایه‌گذاری درآمد ثابت

Collects intraday price and NAV data from tsetmc.com and fipiran.ir,
then identifies arbitrage opportunities between market price and NAV
for fixed-income ETF funds listed on the Tehran Stock Exchange.

Usage:
    python main.py              # Full scan with detailed report
    python main.py --summary    # Summary table only
    python main.py --csv        # Export results to CSV
    python main.py --discover   # Search for new funds not in config
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

from data_fetcher import DataAggregator
from arbitrage import scan_all, filter_actionable
from display import (
    print_summary_table,
    print_detailed_report,
    print_market_overview,
    export_csv,
)


LOG_DIR = Path("logs")


def setup_logging(verbose: bool = False) -> Path:
    """Configure logging to both console and a timestamped log file.

    Returns the path of the log file that was created.
    """
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"run_{timestamp}.log"

    level = logging.DEBUG if verbose else logging.INFO

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter by level

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler — INFO (or DEBUG if --verbose)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler — always DEBUG (full detail)
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


def main():
    parser = argparse.ArgumentParser(
        description="اسکنر آربیتراژ صندوق‌های درآمد ثابت",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="فقط جدول خلاصه نمایش داده شود",
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="خروجی CSV ذخیره شود",
    )
    parser.add_argument(
        "--csv-path", default="arbitrage_results.csv",
        help="مسیر فایل CSV (پیش‌فرض: arbitrage_results.csv)",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="جستجو برای صندوق‌های جدید",
    )
    parser.add_argument(
        "--no-fipiran", action="store_true",
        help="از FIPIRAN به عنوان منبع جایگزین استفاده نشود",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="نمایش جزئیات بیشتر",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="تاخیر بین درخواست‌ها (ثانیه)",
    )

    args = parser.parse_args()
    log_file = setup_logging(args.verbose)

    logger = logging.getLogger(__name__)
    logger.info("Log file: %s", log_file.resolve())
    print(f"📄 لاگ کامل در: {log_file.resolve()}\n")

    aggregator = DataAggregator()

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

    print("\n⏳ در حال دریافت داده‌ها از tsetmc.com ...")
    print("   (اطمینان حاصل کنید که به اینترنت ایران دسترسی دارید)\n")

    try:
        fund_data = aggregator.fetch_all(use_fipiran_fallback=not args.no_fipiran)
    except KeyboardInterrupt:
        print("\n❌ عملیات لغو شد.")
        sys.exit(1)
    except Exception as e:
        logger.error("Failed to fetch data: %s", e)
        print(f"\n❌ خطا در دریافت داده‌ها: {e}")
        print("   لطفاً اتصال اینترنت و دسترسی به tsetmc.com را بررسی کنید.")
        sys.exit(1)

    successful = [f for f in fund_data if f.get("price_data") or f.get("nav_data")]
    failed = [f for f in fund_data if not f.get("price_data") and not f.get("nav_data")]

    if failed:
        logger.warning(
            "%d funds had no data: %s",
            len(failed),
            ", ".join(f["symbol"] for f in failed),
        )

    if not successful:
        print("\n❌ داده‌ای دریافت نشد. لطفاً اتصال اینترنت خود را بررسی کنید.")
        print("   این برنامه نیاز به دسترسی به سایت‌های tsetmc.com و fipiran.ir دارد.")
        sys.exit(1)

    opportunities = scan_all(fund_data)

    print_summary_table(opportunities)
    print_market_overview(opportunities)

    if not args.summary:
        print_detailed_report(opportunities)

    if args.csv:
        export_csv(opportunities, args.csv_path)

    actionable = filter_actionable(opportunities)
    if actionable:
        print(f"\n✅ {len(actionable)} فرصت آربیتراژ قابل اجرا شناسایی شد.")
    else:
        print("\n📊 در حال حاضر فرصت آربیتراژ قابل توجهی وجود ندارد.")

    print()


if __name__ == "__main__":
    main()
