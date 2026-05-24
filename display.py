"""Display and reporting for arbitrage scan results."""

import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import jdatetime
from datetime import datetime
from tabulate import tabulate

from arbitrage import ArbitrageOpportunity


def format_number(n: float, decimals: int = 0) -> str:
    if decimals == 0:
        return f"{int(n):,}"
    return f"{n:,.{decimals}f}"


def get_signal_label(signal: str) -> str:
    labels = {
        "BUY": "🟢 خرید (تخفیف)",
        "SELL": "🔴 فروش (صرف)",
        "HOLD": "⚪ بدون سیگنال",
    }
    return labels.get(signal, signal)


def print_summary_table(opportunities: list[ArbitrageOpportunity]):
    """Print a concise summary table of all funds."""
    now = datetime.now()
    jalali_now = jdatetime.datetime.fromgregorian(datetime=now)

    print("\n" + "=" * 100)
    print(f"  اسکنر آربیتراژ صندوق‌های درآمد ثابت — {jalali_now.strftime('%Y/%m/%d %H:%M')}")
    print("=" * 100)

    headers = [
        "نماد",
        "قیمت بازار",
        "NAV ابطال",
        "NAV صدور",
        "صرف/تخفیف %",
        "سود خالص %",
        "حجم",
        "سیگنال",
    ]

    rows = []
    for o in opportunities:
        pd_str = f"{o.premium_discount_pct:+.2f}%"
        np_str = f"{o.net_profit_pct:+.2f}%"
        vol_str = format_number(o.volume)
        signal = get_signal_label(o.signal)

        rows.append([
            o.symbol,
            format_number(o.market_price),
            format_number(o.nav),
            format_number(o.issue_nav),
            pd_str,
            np_str,
            vol_str,
            signal,
        ])

    print(tabulate(rows, headers=headers, tablefmt="pretty", stralign="center"))
    print()


def print_detailed_report(opportunities: list[ArbitrageOpportunity]):
    """Print detailed analysis for actionable opportunities."""
    actionable = [o for o in opportunities if o.actionable]

    if not actionable:
        print("⚠️  هیچ فرصت آربیتراژ قابل اجرایی یافت نشد.")
        print("   تمامی صندوق‌ها در محدوده طبیعی NAV معامله می‌شوند.")
        print()
        return

    print(f"\n{'=' * 80}")
    print(f"  فرصت‌های آربیتراژ قابل اجرا: {len(actionable)} مورد")
    print(f"{'=' * 80}\n")

    for i, o in enumerate(actionable, 1):
        print(f"── فرصت {i}: {o.symbol} ({o.name}) ──")
        print()

        if o.signal == "BUY":
            print("  نوع: خرید از بازار ← ابطال (تخفیف آربیتراژ)")
            print(f"  قیمت خرید از بازار:  {format_number(o.market_price)} ریال")
            print(f"  NAV ابطال:           {format_number(o.cancel_nav)} ریال")
            print(f"  تخفیف:              {abs(o.premium_discount_pct):.2f}%")
            print(f"  سود خالص تخمینی:     {o.net_profit_pct:+.2f}%")
            print()
            print("  مراحل اجرا:")
            print("    ۱. خرید واحدهای صندوق از بازار بورس")
            print("    ۲. ارسال درخواست ابطال به صندوق")
            print("    ۳. دریافت وجه ابطال بر اساس NAV ابطال")
            print(f"    ⏱  زمان تسویه: معمولاً T+2 تا T+4")

        elif o.signal == "SELL":
            print("  نوع: صدور از صندوق ← فروش در بازار (صرف آربیتراژ)")
            print(f"  NAV صدور:           {format_number(o.issue_nav)} ریال")
            print(f"  قیمت فروش در بازار: {format_number(o.market_price)} ریال")
            print(f"  صرف:               {o.premium_discount_pct:.2f}%")
            print(f"  سود خالص تخمینی:    {o.net_profit_pct:+.2f}%")
            print()
            print("  مراحل اجرا:")
            print("    ۱. صدور واحدهای جدید از صندوق (واریز وجه)")
            print("    ۲. انتظار برای تخصیص واحدها")
            print("    ۳. فروش واحدها در بازار بورس")
            print(f"    ⏱  زمان تسویه: معمولاً T+1 تا T+3")

        print()
        print(f"  حجم معاملات:   {format_number(o.volume)}")
        print(f"  ارزش معاملات:  {format_number(o.value)} ریال")
        print(f"  تعداد معاملات: {format_number(o.trade_count)}")

        if o.best_bid > 0 or o.best_ask > 0:
            print(f"  بهترین خرید:  {format_number(o.best_bid)} ({format_number(o.bid_depth)} واحد)")
            print(f"  بهترین فروش:  {format_number(o.best_ask)} ({format_number(o.ask_depth)} واحد)")

        print()
        print("  ⚠️  ریسک‌ها و ملاحظات:")
        print("    • تغییر NAV در فاصله زمانی تسویه")
        print("    • کارمزد صدور/ابطال متفاوت بین صندوق‌ها")
        print("    • حداقل مبلغ صدور/ابطال")
        print("    • ریسک نقدشوندگی در صف خرید/فروش")
        print()
        print("-" * 80)
        print()


def print_market_overview(opportunities: list[ArbitrageOpportunity]):
    """Print a market overview with key statistics."""
    if not opportunities:
        print("داده‌ای برای نمایش وجود ندارد.")
        return

    total = len(opportunities)
    at_premium = sum(1 for o in opportunities if o.premium_discount_pct > 0.1)
    at_discount = sum(1 for o in opportunities if o.premium_discount_pct < -0.1)
    at_par = total - at_premium - at_discount
    actionable = sum(1 for o in opportunities if o.actionable)

    avg_pd = sum(o.premium_discount_pct for o in opportunities) / total if total else 0
    max_premium = max((o.premium_discount_pct for o in opportunities), default=0)
    max_discount = min((o.premium_discount_pct for o in opportunities), default=0)

    max_premium_fund = next(
        (o.symbol for o in opportunities if o.premium_discount_pct == max_premium), "—"
    )
    max_discount_fund = next(
        (o.symbol for o in opportunities if o.premium_discount_pct == max_discount), "—"
    )

    print(f"\n{'─' * 60}")
    print("  خلاصه وضعیت بازار صندوق‌های درآمد ثابت")
    print(f"{'─' * 60}")
    print(f"  تعداد صندوق‌های بررسی شده:     {total}")
    print(f"  در صرف (بالای NAV):            {at_premium}")
    print(f"  در تخفیف (زیر NAV):            {at_discount}")
    print(f"  نزدیک به NAV:                  {at_par}")
    print(f"  فرصت‌های قابل اجرا:            {actionable}")
    print(f"  میانگین صرف/تخفیف:             {avg_pd:+.2f}%")
    print(f"  بیشترین صرف:   {max_premium:+.2f}% ({max_premium_fund})")
    print(f"  بیشترین تخفیف: {max_discount:+.2f}% ({max_discount_fund})")
    print(f"{'─' * 60}\n")


def export_csv(opportunities: list[ArbitrageOpportunity], filepath: str = "arbitrage_results.csv"):
    """Export results to CSV file."""
    import csv

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "نماد", "نام صندوق", "قیمت بازار", "NAV ابطال", "NAV صدور",
            "NAV آماری", "صرف/تخفیف %", "سود خالص %", "حجم", "ارزش معاملات",
            "تعداد معاملات", "بهترین خرید", "بهترین فروش", "عمق خرید",
            "عمق فروش", "سیگنال", "قابل اجرا", "توضیحات",
        ])

        for o in opportunities:
            writer.writerow([
                o.symbol, o.name, o.market_price, o.cancel_nav, o.issue_nav,
                o.statistical_nav, o.premium_discount_pct, o.net_profit_pct,
                o.volume, o.value, o.trade_count, o.best_bid, o.best_ask,
                o.bid_depth, o.ask_depth, o.signal, o.actionable,
                " | ".join(o.reasons),
            ])

    print(f"✅ نتایج در فایل {filepath} ذخیره شد.")
