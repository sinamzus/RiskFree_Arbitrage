#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تأیید مسیر کامل دیتای زنده‌ی درون‌روز (همان کدی که داشبورد استفاده می‌کند)
=====================================================================
این اسکریپت دقیقاً کد backend پروژه را اجرا می‌کند تا ببینیم کجا (اگر جایی)
زنجیره می‌شکند:

  مرحله ۱  data_fetcher.get_today_trades(INS)          → تیک‌های خام امروز
  مرحله ۲  web_server._aggregate_ticks(...)            → کندل‌های ۱ دقیقه‌ای
  مرحله ۳  GET /api/live_intraday?symbol=...           → دقیقاً همان JSON
           که مرورگر دریافت می‌کند (از طریق Flask test client)

از داخل ایران اجرا کن. خروجی در verify_live_output.txt ذخیره می‌شود.

اجرا:
    python tools/verify_live.py
    python tools/verify_live.py 3846143218462419
"""

import sys
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INS = sys.argv[1] if len(sys.argv) > 1 else "3846143218462419"

_buf = io.StringIO()
def out(*a):
    line = " ".join(str(x) for x in a)
    print(line)
    _buf.write(line + "\n")


def hhmmss(t):
    t = int(t)
    return f"{t//10000:02d}:{(t//100)%100:02d}:{t%100:02d}"


def main():
    from datetime import datetime
    out("╔" + "═" * 64 + "╗")
    out("  تأیید مسیر دیتای زنده‌ی درون‌روز")
    out(f"  insCode = {INS}")
    out(f"  زمان    = {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out("╚" + "═" * 64 + "╝")

    # ── مرحله ۱: fetch خام تیک‌های امروز ─────────────────────────────────
    out("\n── مرحله ۱: get_today_trades (Trade/GetTrade) ─────────────────")
    try:
        from data_fetcher import TSETMCFetcher
        f = TSETMCFetcher()
        ticks = f.get_today_trades(INS)
        out(f"  تعداد تیک: {len(ticks)}")
        if ticks:
            out("  تیک اول:", json.dumps(ticks[0], ensure_ascii=False))
            out("  تیک آخر:", json.dumps(ticks[-1], ensure_ascii=False))
            out(f"  بازه‌ی زمانی: {hhmmss(ticks[0]['time'])} → {hhmmss(ticks[-1]['time'])}")
        else:
            out("  ⚠ هیچ تیکی برنگشت — یا بازار بسته است یا هدر/شبکه مشکل دارد.")
    except Exception as e:
        import traceback
        out("  ✗ خطا:", repr(e))
        out(traceback.format_exc())
        ticks = []

    # ── مرحله ۲: تجمیع به کندل ۱ دقیقه‌ای ────────────────────────────────
    out("\n── مرحله ۲: _aggregate_ticks → کندل ۱ دقیقه‌ای ────────────────")
    try:
        from web_server import _aggregate_ticks
        today_int = int(datetime.now().strftime("%Y%m%d"))
        bars = _aggregate_ticks(ticks, 1, today_int)
        out(f"  تعداد کندل ۱ دقیقه‌ای: {len(bars)}")
        if bars:
            out("  کندل اول:", json.dumps(bars[0], ensure_ascii=False))
            out("  کندل آخر:", json.dumps(bars[-1], ensure_ascii=False))
    except Exception as e:
        import traceback
        out("  ✗ خطا:", repr(e))
        out(traceback.format_exc())

    # ── مرحله ۳: خود endpoint از طریق Flask test client ──────────────────
    out("\n── مرحله ۳: GET /api/live_intraday (همان چیزی که مرورگر می‌گیرد) ──")
    try:
        from database import Database
        from web_server import create_app
        from config import FIXED_INCOME_ETFS

        # یک نماد از config پیدا کن که ins_code‌اش = INS باشد؛
        # وگرنه از اولین نماد config استفاده کن.
        symbol = None
        for fund in FIXED_INCOME_ETFS:
            if str(fund.get("ins_code")) == str(INS):
                symbol = fund["symbol"]
                break
        used_real = symbol is not None
        if not symbol:
            symbol = FIXED_INCOME_ETFS[0]["symbol"]

        db = Database(ROOT / "data" / "arbitrage.db")
        app = create_app(db)
        client = app.test_client()
        out(f"  نماد آزمایش: {symbol}"
            + ("" if used_real else "  (insCode داده‌شده در config نبود؛ نماد پیش‌فرض)"))

        from urllib.parse import quote
        r = client.get(f"/api/live_intraday?symbol={quote(symbol)}")
        out("  وضعیت HTTP:", r.status_code)
        j = r.get_json()
        if j is None:
            out("  پاسخ JSON نبود:", r.data[:200])
        else:
            summary = {k: j.get(k) for k in
                       ("symbol", "date", "source", "bar_count", "spot", "nav", "interval")}
            out("  خلاصه:", json.dumps(summary, ensure_ascii=False))
            bars = j.get("bars") or []
            if bars:
                out("  کندل اول:", json.dumps(bars[0], ensure_ascii=False))
                out("  کندل آخر:", json.dumps(bars[-1], ensure_ascii=False))
            else:
                out("  ⚠ bars خالی است.")
    except Exception as e:
        import traceback
        out("  ✗ خطا:", repr(e))
        out(traceback.format_exc())

    out("\n" + "═" * 66)
    out("نتیجه‌گیری:")
    out("  • اگر مرحله ۱ و ۲ تیک/کندل دارند ولی مرورگرت چیزی نشان نمی‌دهد →")
    out("    سرور را restart نکرده‌ای یا مرورگر کش است (Ctrl+Shift+R).")
    out("  • اگر مرحله ۳ bar_count>0 دارد → backend کاملاً سالم است.")
    out("  • اگر مرحله ۱ خالی است → مشکل شبکه/هدر یا بسته بودن بازار.")

    try:
        with open(ROOT / "verify_live_output.txt", "w", encoding="utf-8") as fh:
            fh.write(_buf.getvalue())
        print("\n✅ خروجی در verify_live_output.txt ذخیره شد.")
    except Exception as e:
        print("خطا در ذخیره:", e)


if __name__ == "__main__":
    main()
