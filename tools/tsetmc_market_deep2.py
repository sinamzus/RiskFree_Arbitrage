#!/usr/bin/env python3
"""
تحلیل عمیق ساختار TSETMC: صفحه سوابق + داده‌های درون‌روزی + دیتای دقیقه‌ای.

روی ماشینی با دسترسی به اینترنت ایران اجرا کنید:
    python tools/tsetmc_market_deep2.py

خروجی: tools/tsetmc_market_deep2_report.txt
"""
import sys, json, re, time, math
from datetime import datetime, timedelta
from pathlib import Path
import requests

# ── Windows UTF-8 ──────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).parent / "tsetmc_market_deep2_report.txt"
lines = []
def log(msg=""): print(msg); lines.append(str(msg))
def sep(t=""): log(); log("─"*72); log(f"  {t}") if t else None; log("─"*72)

CDN  = "https://cdn.tsetmc.com/api"
MAIN = "https://www.tsetmc.com"
IR   = "https://www.tsetmc.ir"

INS_CODE = "34718633636164421"   # کمند — صندوق درآمد ثابت کمند

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.tsetmc.com/",
}
s = requests.Session()
s.headers.update(HEADERS)

def get(url, silent=False, **kwargs):
    try:
        r = s.get(url, timeout=20, **kwargs)
        if not silent:
            log(f"  GET {url}")
            log(f"      → {r.status_code}  {len(r.content):,}B  CT:{r.headers.get('Content-Type','?')[:40]}")
        return r
    except Exception as e:
        if not silent:
            log(f"  GET {url}  → ERROR: {e}")
        return None

def try_json(r):
    if not r: return None
    try: return r.json()
    except Exception: return None

# تاریخ‌های معاملاتی اخیر (شنبه–چهارشنبه ایران)
today = datetime.now()
trading_dates = []
for delta in range(0, 14):
    d = today - timedelta(days=delta)
    if d.weekday() not in (3, 4):   # پنجشنبه=3، جمعه=4 تعطیل
        trading_dates.append(d.strftime("%Y%m%d"))
    if len(trading_dates) >= 5: break

log(f"تاریخ‌های معاملاتی اخیر: {trading_dates}")


# ════════════════════════════════════════════════════════════════════════
# A. صفحه سوابق (تب سابقه) در tsetmc.ir و tsetmc.com
# ════════════════════════════════════════════════════════════════════════
sep("A. صفحه سوابق (History tab) — bررسی endpoint ها")

history_urls = [
    # tsetmc.com
    f"{MAIN}/instrument/{INS_CODE}",
    f"{MAIN}/Symbol/Detail/{INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=15131F&i={INS_CODE}",  # TseClient history
    f"{MAIN}/tsev2/data/instinfofast.aspx?i={INS_CODE}&b=1",
    f"{MAIN}/tsev2/data/PaperHistory.aspx?i={INS_CODE}",
    # tsetmc.ir
    f"{IR}/Symbol/Detail/{INS_CODE}",
    f"{IR}/instrument/{INS_CODE}",
    # CDN endpoints
    f"{CDN}/ClosingPrice/GetClosingPriceHistory/{INS_CODE}/0",
    f"{CDN}/ClosingPrice/GetClosingPriceHistory/{INS_CODE}/1",
    f"{CDN}/ClosingPrice/GetClosingPriceHistory/{INS_CODE}/2",
    f"{CDN}/ClosingPrice/GetClosingPriceHistory/{INS_CODE}/3",
    f"{CDN}/ClosingPrice/GetClosingPriceHistory/{INS_CODE}/4",
    f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{INS_CODE}/30",  # این می‌دانیم کار می‌کند
    f"{CDN}/ClosingPrice/GetClosingPriceDailyList/{INS_CODE}/365",
]

for url in history_urls:
    r = get(url, silent=True)
    if not r: continue
    try:
        d = r.json()
        keys = list(d.keys()) if isinstance(d, dict) else f"list[{len(d)}]"
        log(f"  ✓ JSON  {url}")
        log(f"    keys: {keys}")
        # اگر داده‌ای دارد نشان بده
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, list) and v:
                    log(f"    {k}: list[{len(v)}]  first_keys={list(v[0].keys()) if isinstance(v[0],dict) else '?'}")
    except Exception:
        size = len(r.content)
        is_spa = size < 1500
        log(f"  {'SPA-shell' if is_spa else 'HTML'} ({size}B)  {url}")


# ════════════════════════════════════════════════════════════════════════
# B. بررسی endpoint های دیتای دقیقه‌ای (OHLCV per minute)
# ════════════════════════════════════════════════════════════════════════
sep("B. جستجوی endpoint دیتای دقیقه‌ای")

MINUTE_CANDIDATES = [
    # احتمالی‌ترین نام‌ها
    f"{CDN}/ClosingPrice/GetMinuteData/{INS_CODE}",
    f"{CDN}/ClosingPrice/GetMinuteData/{INS_CODE}/1",
    f"{CDN}/ClosingPrice/GetMinuteData/{INS_CODE}/5",
    f"{CDN}/ClosingPrice/GetMinuteClosingPrice/{INS_CODE}",
    f"{CDN}/ClosingPrice/GetMinuteClosingPriceList/{INS_CODE}/1",
    f"{CDN}/ClosingPrice/GetMinuteClosingPriceList/{INS_CODE}/5",
    f"{CDN}/ClosingPrice/GetIntradayOHLCV/{INS_CODE}",
    f"{CDN}/ClosingPrice/GetIntradayOHLCV/{INS_CODE}/1",
    f"{CDN}/ClosingPrice/GetOHLCV/{INS_CODE}",
    f"{CDN}/ClosingPrice/GetOHLCV/{INS_CODE}/{trading_dates[0]}",
    # مسیرهای Trade
    f"{CDN}/Trade/GetMinuteData/{INS_CODE}",
    f"{CDN}/Trade/GetMinuteData/{INS_CODE}/{trading_dates[0]}",
    f"{CDN}/Trade/GetMinuteChart/{INS_CODE}",
    f"{CDN}/Trade/GetMinuteChart/{INS_CODE}/{trading_dates[0]}",
    f"{CDN}/Trade/GetAggregatedTrade/{INS_CODE}/{trading_dates[0]}/1",
    f"{CDN}/Trade/GetAggregatedTrade/{INS_CODE}/{trading_dates[0]}/5",
    # Chart endpoint
    f"{CDN}/Chart/GetData/{INS_CODE}",
    f"{CDN}/Chart/GetData/{INS_CODE}/1",
    f"{CDN}/Chart/GetMinuteData/{INS_CODE}",
    f"{CDN}/Chart/GetMinuteData/{INS_CODE}/1",
    f"{CDN}/Chart/GetMinuteData/{INS_CODE}/{trading_dates[0]}",
    # Instrument endpoints
    f"{CDN}/Instrument/GetMinuteData/{INS_CODE}",
    f"{CDN}/Instrument/GetMinuteData/{INS_CODE}/{trading_dates[0]}/1",
    # tsetmc.com Loader.aspx with different ParTree values
    f"{MAIN}/Loader.aspx?ParTree=15131P&i={INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=15131M&i={INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=151318&i={INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=15131L&i={INS_CODE}",
    f"{MAIN}/Loader.aspx?ParTree=15131C&i={INS_CODE}",
]

minute_endpoints_found = []
for url in MINUTE_CANDIDATES:
    r = get(url, silent=True)
    if not r: continue
    d = try_json(r)
    if d is not None:
        log(f"  ✓ JSON!  {url}")
        if isinstance(d, dict):
            log(f"    keys: {list(d.keys())}")
        elif isinstance(d, list):
            log(f"    list[{len(d)}]")
            if d and isinstance(d[0], dict):
                log(f"    item keys: {list(d[0].keys())}")
        minute_endpoints_found.append(url)
    elif len(r.content) > 50 and len(r.content) < 1500:
        log(f"  ? small response ({len(r.content)}B)  {url}: {r.text[:100]}")
    time.sleep(0.1)

if not minute_endpoints_found:
    log("\n  ✗ هیچ endpoint دقیقه‌ای پیدا نشد")
    log("  → باید تیک‌ها را از GetTradeHistory خودمان به دقیقه تبدیل کنیم")


# ════════════════════════════════════════════════════════════════════════
# C. بررسی کامل GetTradeHistory — ساختار تیک‌ها و تبدیل به OHLCV دقیقه‌ای
# ════════════════════════════════════════════════════════════════════════
sep("C. GetTradeHistory — تیک‌ها + تبدیل به OHLCV دقیقه‌ای")

for date_str in trading_dates[:2]:
    url = f"{CDN}/Trade/GetTradeHistory/{INS_CODE}/{date_str}/false"
    r = get(url)
    if not r: continue
    d = try_json(r)
    if not d: continue

    trades = d.get("tradeHistory") or []
    log(f"\n  {date_str}: {len(trades)} تیک")
    if not trades: continue

    # همه کلیدهای یک تیک
    log(f"  کلیدهای تیک: {list(trades[0].keys())}")
    for k, v in sorted(trades[0].items()):
        log(f"    {k:35} = {v}")

    # فیلتر کنسل‌شده‌ها
    valid = [t for t in trades if not t.get("canceled")]
    log(f"\n  تیک‌های معتبر (canceled=false): {len(valid)}")

    # نمایش 10 تیک اول (قدیمی‌ترین)
    log(f"\n  ۱۰ تیک اول روز:")
    for t in sorted(valid, key=lambda x: x.get("nTran",0))[:10]:
        h = str(t.get("hEven",0)).zfill(6)
        log(f"    {h[:2]}:{h[2:4]}:{h[4:]}  قیمت={t.get('pTran',0):,.0f}  "
            f"حجم={t.get('qTitTran',0):,}  seq={t.get('nTran')}")

    # تبدیل به OHLCV دقیقه‌ای (aggregation)
    def to_minute_bars(ticks, interval_min=1):
        bars = {}
        for t in sorted(ticks, key=lambda x: x.get("nTran",0)):
            heven = t.get("hEven", 0)
            h = heven // 10000
            m = (heven % 10000) // 100
            bar_m = (m // interval_min) * interval_min
            key = h * 100 + bar_m
            p = t.get("pTran", 0)
            v = t.get("qTitTran", 0)
            if key not in bars:
                bars[key] = {"time": key, "open": p, "high": p, "low": p,
                             "close": p, "volume": 0, "count": 0}
            b = bars[key]
            b["high"]   = max(b["high"], p)
            b["low"]    = min(b["low"],  p)
            b["close"]  = p
            b["volume"] += v
            b["count"]  += 1
        return sorted(bars.values(), key=lambda x: x["time"])

    bars_1m  = to_minute_bars(valid, 1)
    bars_5m  = to_minute_bars(valid, 5)
    bars_15m = to_minute_bars(valid, 15)

    log(f"\n  OHLCV aggregation نتیجه:")
    log(f"    ۱ دقیقه: {len(bars_1m)} شمع")
    log(f"    ۵ دقیقه: {len(bars_5m)} شمع")
    log(f"    ۱۵ دقیقه: {len(bars_15m)} شمع")

    log(f"\n  نمونه شمع‌های ۵ دقیقه‌ای (اول ۸ شمع):")
    for b in bars_5m[:8]:
        t = str(b["time"]).zfill(4)
        log(f"    {t[:2]}:{t[2:]}  O={b['open']:,.0f}  H={b['high']:,.0f}  "
            f"L={b['low']:,.0f}  C={b['close']:,.0f}  V={b['volume']:,}  n={b['count']}")


# ════════════════════════════════════════════════════════════════════════
# D. بررسی GetTrade (live stream) — تیک‌های همین لحظه
# ════════════════════════════════════════════════════════════════════════
sep("D. GetTrade — stream تیک‌های لحظه‌ای")

url = f"{CDN}/Trade/GetTrade/{INS_CODE}"
r = get(url)
d = try_json(r)
if d:
    trades = d.get("trade") or []
    log(f"\n  تعداد تیک لحظه‌ای: {len(trades)}")
    if trades:
        log(f"  کلیدها: {list(trades[0].keys())}")
        for t in sorted(trades, key=lambda x: x.get("nTran",0))[:5]:
            h = str(t.get("hEven",0)).zfill(6)
            log(f"    {h[:2]}:{h[2:4]}:{h[4:]}  p={t.get('pTran',0):,}  "
                f"v={t.get('qTitTran',0):,}")


# ════════════════════════════════════════════════════════════════════════
# E. WebSocket — آیا TSETMC از WS استفاده می‌کند؟
# ════════════════════════════════════════════════════════════════════════
sep("E. بررسی WebSocket و SignalR endpoint ها")

WS_CANDIDATES = [
    f"{MAIN}/signalr/negotiate",
    f"{MAIN}/signalr/hubs/negotiate",
    "https://cdn.tsetmc.com/signalr/negotiate",
    "https://ws.tsetmc.com/",
    "https://push.tsetmc.com/",
    "https://stream.tsetmc.com/",
    f"{CDN}/Hub/negotiate",
    f"{CDN}/hub/negotiate",
    "https://push.tsetmc.com/signalr/negotiate",
]

for url in WS_CANDIDATES:
    r = get(url, silent=True)
    if r and r.status_code != 404:
        log(f"  {r.status_code}  {url}  ({len(r.content)}B)")
        if len(r.content) < 500:
            log(f"    preview: {r.text[:200]}")


# ════════════════════════════════════════════════════════════════════════
# F. صفحه نماد در tsetmc.ir — بررسی کامل
# ════════════════════════════════════════════════════════════════════════
sep("F. صفحه نماد tsetmc.ir — تحلیل کامل network requests")

log(f"  tsetmc.ir/Symbol/Detail/{INS_CODE}")
r = get(f"{IR}/Symbol/Detail/{INS_CODE}")
if r:
    log(f"  size={len(r.content)}B  redirect={r.url}")
    is_spa = len(r.content) < 1500
    log(f"  SPA shell: {is_spa}")
    if not is_spa:
        log(f"  Preview: {r.text[:500].replace(chr(10),' ')}")

# تست همه زیرمسیرهای احتمالی صفحه نماد
symbol_pages = [
    f"{CDN}/Instrument/GetInstrumentInfo/{INS_CODE}",
    f"{CDN}/Instrument/GetInstrumentStatistic/{INS_CODE}",
    f"{CDN}/Instrument/GetKeyStatistic/{INS_CODE}",
    f"{CDN}/Instrument/GetInstrumentIdentity/{INS_CODE}",
    f"{CDN}/Instrument/GetSupervisorMessage/{INS_CODE}",
    f"{CDN}/Instrument/GetInstrumentInfo/{INS_CODE}",
    f"{CDN}/BestLimits/{INS_CODE}",
    f"{CDN}/BestLimits/{INS_CODE}/0",
    f"{CDN}/ClientType/GetClientTypeHistory/{INS_CODE}/0",   # حقیقی/حقوقی تاریخی
    f"{CDN}/ClientType/GetClientTypeHistory/{INS_CODE}/1",
    f"{CDN}/ShareHolder/GetShareHolderList/{INS_CODE}/0",     # سهامداران
    f"{CDN}/OptionData/GetOptionData/{INS_CODE}",
    f"{CDN}/ClosingPrice/GetRelatedCompany/{INS_CODE}",
    f"{CDN}/ClosingPrice/GetIndexRelatedCompany/{INS_CODE}",
    f"{CDN}/Instrument/GetInstrumentSearch/%DA%A9%D9%85%D9%86%D8%AF",
]

log("\n  بررسی endpoint های اطلاعات صفحه نماد:")
for url in symbol_pages:
    r = get(url, silent=True)
    if not r: continue
    d = try_json(r)
    if d is not None:
        if isinstance(d, dict):
            keys = list(d.keys())
            log(f"  ✓ JSON  {url.replace(CDN,'')}  keys={keys}")
            # dump فیلدهای مهم InstrumentInfo
            for sub_key in keys:
                sub = d.get(sub_key)
                if isinstance(sub, dict):
                    log(f"    {sub_key}: {list(sub.keys())[:12]}")
                elif isinstance(sub, list) and sub:
                    log(f"    {sub_key}: list[{len(sub)}]")
        elif isinstance(d, list):
            log(f"  ✓ JSON list[{len(d)}]  {url.replace(CDN,'')}")
            if d and isinstance(d[0], dict):
                log(f"    item keys: {list(d[0].keys())[:12]}")


# ════════════════════════════════════════════════════════════════════════
# G. GetClientTypeHistory — خرید/فروش حقیقی حقوقی (مهم برای تحلیل)
# ════════════════════════════════════════════════════════════════════════
sep("G. GetClientTypeHistory — خرید/فروش حقیقی و حقوقی تاریخی")

url = f"{CDN}/ClientType/GetClientTypeHistory/{INS_CODE}/0"
r = get(url)
d = try_json(r)
if d:
    items = d.get("clientTypeHistory") or d.get("clientType") or []
    log(f"\n  {len(items)} روز سابقه خرید/فروش حقیقی-حقوقی")
    if items:
        log(f"  کلیدها: {list(items[0].keys())}")
        log(f"\n  ۳ روز اخیر:")
        for item in sorted(items, key=lambda x: x.get("dEven",0), reverse=True)[:3]:
            log(f"    dEven={item.get('dEven')}  "
                f"buy_real_vol={item.get('buy_I_Volume',item.get('buyIVolume','-'))}  "
                f"sell_real_vol={item.get('sell_I_Volume',item.get('sellIVolume','-'))}")
            log(f"    " + "  ".join(f"{k}={v}" for k,v in list(item.items())[:8]))


# ════════════════════════════════════════════════════════════════════════
# H. خلاصه نتایج
# ════════════════════════════════════════════════════════════════════════
sep("H. خلاصه — چه endpoint هایی برای چارت دقیقه‌ای در دسترس است")

log("""
  نتیجه‌گیری:
  ──────────────────────────────────────────────────────
  1. ENDPOINT دقیقه‌ای مستقیم:
     → اگر minute_endpoints_found خالی بود: وجود ندارد
     → باید از GetTradeHistory تیک‌ها را خودمان aggregate کنیم

  2. ساختار تبدیل تیک به دقیقه:
     GetTradeHistory/{insCode}/{YYYYMMDD}/false
     → hEven (HHMMSS int) را به HHMM رند می‌کنیم
     → OHLCV دقیقه‌ای/۵ دقیقه‌ای/۱۵ دقیقه‌ای می‌سازیم

  3. صفحه سوابق tsetmc.ir:
     → SPA shell است — داده از CDN endpoints می‌آید
     → GetClosingPriceDailyList کار می‌کند (روزانه)
     → GetTradeHistory کار می‌کند (تیک‌های روزانه)

  4. داده live:
     → GetTrade/{insCode}     (تیک‌های روز جاری)
     → GetClosingPriceInfo    (قیمت لحظه‌ای)
     → BestLimits/{insCode}   (عرضه/تقاضا)
  ──────────────────────────────────────────────────────
""")


# ════════════════════════════════════════════════════════════════════════
sep("DONE")
report = "\n".join(lines)
OUT.write_text(report, encoding="utf-8")
print(f"\nگزارش ذخیره شد: {OUT}")
