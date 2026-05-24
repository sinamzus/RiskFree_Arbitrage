"""fipiran_v2.py — پیدا کردن API فیپیران از طریق JS bundle

روش:
  1. فایل HTML صفحه mf/list را بگیر
  2. URL های JS bundle را استخراج کن
  3. هر bundle را دانلود کن و pattern های API را پیدا کن
  4. endpoint های پیدا شده را تست کن
  5. نتیجه را چاپ کن

اجرا:
  pip install requests beautifulsoup4
  python tools/fipiran_v2.py
"""

import re
import sys
import json
import time
import socket
import requests
from urllib.parse import urljoin, urlparse

# ── اطمینان از UTF-8 در ویندوز ──
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
    "Referer": "https://fipiran.ir/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

BASE = "https://fipiran.ir"
OUT  = "tools/fipiran_v2_report.txt"
lines = []

def log(s=""):
    print(s)
    lines.append(str(s))

def get(url, **kw):
    kw.setdefault("timeout", 20)
    kw.setdefault("verify", True)
    try:
        r = SESSION.get(url, **kw)
        return r
    except Exception as e:
        return None


# ══════════════════════════════════════════════════════════════════════════
#  بخش ۱ — پیدا کردن JS bundle
# ══════════════════════════════════════════════════════════════════════════
log("=" * 72)
log("  بخش ۱ — استخراج URL های JS bundle از صفحه fipiran.ir")
log("=" * 72)

r = get(f"{BASE}/mf/list")
if not r:
    log("  خطا: نمی‌توان به fipiran.ir وصل شد")
else:
    log(f"  GET {BASE}/mf/list → {r.status_code}  {len(r.content)}B")
    html = r.text

    # پیدا کردن تمام script src ها
    script_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I)
    log(f"  تعداد script src: {len(script_srcs)}")
    for s in script_srcs:
        log(f"    {s}")

    # پیدا کردن link href های CSS/JS
    link_hrefs = re.findall(r'<link[^>]+href=["\']([^"\']+\.(?:js|css))["\']', html, re.I)
    log(f"  تعداد link href: {len(link_hrefs)}")

    # همه JS URL ها
    all_js = [s for s in script_srcs if s.endswith(".js") or "/static/js/" in s or "/chunks/" in s]
    if not all_js:
        all_js = script_srcs  # اگر هیچ JS خاصی نبود همه را بگیر
    log(f"\n  JS bundle های یافت‌شده: {len(all_js)}")

    # ── بخش ۲: دانلود bundle و جستجوی pattern ──
    log()
    log("=" * 72)
    log("  بخش ۲ — دانلود JS bundle و جستجوی endpoint های API")
    log("=" * 72)

    found_patterns = []

    for src in all_js:
        bundle_url = urljoin(BASE, src)
        rb = get(bundle_url)
        if not rb or rb.status_code != 200:
            log(f"  ✗ {bundle_url}  → {getattr(rb, 'status_code', 'ERROR')}")
            continue
        content = rb.text
        log(f"  ✓ {bundle_url}  ({len(content)} chars)")

        # الگوهایی که دنبالشان هستیم
        patterns = [
            r'fund\.fipiran\.ir[^\s"\']*',
            r'fipiran\.ir/api[^\s"\']{0,100}',
            r'fipiran\.ir/DataService[^\s"\']{0,100}',
            r'fipiran\.ir/mf/[^\s"\']{0,100}',
            r'/api/v\d+/fund[^\s"\']{0,100}',
            r'fundcompare[^\s"\']{0,80}',
            r'fundlist[^\s"\']{0,80}',
            r'TypeOfInvest[^\s"\']{0,80}',
            r'typeOfFund[^\s"\']{0,80}',
            r'isETF[^\s"\']{0,80}',
        ]
        for pat in patterns:
            matches = re.findall(pat, content, re.I)
            for m in matches:
                if m not in found_patterns:
                    found_patterns.append(m)
                    log(f"    ✦ {m}")

    if not found_patterns:
        log("  — هیچ pattern API ای در bundle پیدا نشد")
    else:
        log(f"\n  جمع pattern های یافت‌شده: {len(found_patterns)}")

# ══════════════════════════════════════════════════════════════════════════
#  بخش ۳ — تست DNS برای fund.fipiran.ir
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۳ — بررسی DNS و دسترسی به fund.fipiran.ir")
log("=" * 72)

domains = ["fund.fipiran.ir", "api.fipiran.ir", "fipiran.ir", "www.fipiran.ir"]
for d in domains:
    try:
        ip = socket.gethostbyname(d)
        log(f"  ✓ {d} → {ip}")
    except Exception as e:
        log(f"  ✗ {d} → DNS FAIL: {e}")

# ══════════════════════════════════════════════════════════════════════════
#  بخش ۴ — تست endpoint های احتمالی با headers مختلف
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۴ — تست endpoint های احتمالی فیپیران")
log("=" * 72)

# endpoint های با احتمال بالا بر اساس ساختار React-SPA های ایرانی
candidates = [
    # fund.fipiran.ir
    "https://fund.fipiran.ir/api/v1/fund/fundcompare",
    "https://fund.fipiran.ir/api/v1/fund/fundcompare?TypeOfInvest=Fixed&ReturnType=1",
    "https://fund.fipiran.ir/api/v1/fund/fundcompare?typeOfFund=1",
    "https://fund.fipiran.ir/api/v1/fund/fundlist",
    "https://fund.fipiran.ir/api/v1/fund/fundlist?pageNumber=1&pageSize=200",
    "https://fund.fipiran.ir/api/v1/fund/fundlist?typeOfFund=1&pageNumber=1&pageSize=200",
    "https://fund.fipiran.ir/api/v1/fund/treemap",
    # fipiran.ir مستقیم
    "https://fipiran.ir/api/v1/fund/fundcompare",
    "https://fipiran.ir/api/v1/fund/fundlist",
    "https://fipiran.ir/DataService/FundCompare?typeOfFund=1&isETF=true",
    "https://fipiran.ir/DataService/FundList?typeOfFund=1",
    "https://fipiran.ir/DataService/FundList?fundType=1",
    # DataService با JSON accept
    "https://fipiran.ir/DataService/FundCompare",
    "https://fipiran.ir/DataService/FundList",
    # مسیرهای قدیمی‌تر
    "https://fipiran.ir/MFBourse/FundList?typeOfFund=1",
    "https://fipiran.ir/MFBourse/FundData?typeOfFund=1",
    "https://fipiran.ir/MFBourse/GetFunds",
    # AJAX-style
    "https://fipiran.ir/mf/GetFundList?typeOfFund=1&isETF=true",
    "https://fipiran.ir/mf/GetFundList?fundType=%D8%AF%D8%B1%D8%A2%D9%85%D8%AF%20%D8%AB%D8%A7%D8%A8%D8%AA",
]

json_headers = {
    **HEADERS,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://fipiran.ir/mf/list",
}

fund_data = []

for url in candidates:
    r = get(url, headers=json_headers, allow_redirects=True)
    if r is None:
        log(f"  ✗ ERROR  {url}")
        continue
    ct = r.headers.get("Content-Type", "?")
    size = len(r.content)
    if size < 100:
        log(f"  — tiny ({size}B)  {url}")
        continue
    if "application/json" in ct:
        log(f"  ✓ JSON ({size}B)  {url}")
        try:
            data = r.json()
            log(f"     keys: {list(data.keys()) if isinstance(data, dict) else f'list[{len(data)}]'}")
            # جستجو در داده برای صندوق‌های درآمد ثابت
            if isinstance(data, list) and len(data) > 0:
                log(f"     first item keys: {list(data[0].keys()) if isinstance(data[0], dict) else '?'}")
                fund_data = data
            elif isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list) and len(v) > 0:
                        log(f"     {k}: list[{len(v)}]  first_keys={list(v[0].keys()) if isinstance(v[0], dict) else '?'}")
                        if not fund_data:
                            fund_data = v
        except Exception as e:
            log(f"     JSON parse error: {e}")
            log(f"     preview: {r.text[:200]}")
    elif "text/html" in ct and size == 1532:
        pass  # SPA shell - skip
    elif "text/html" in ct and size > 1532:
        log(f"  ? HTML ({size}B)  {url}")
        log(f"     preview: {r.text[:100]}")
    else:
        log(f"  ? {ct} ({size}B)  {url}")
        log(f"     preview: {r.text[:200]}")
    time.sleep(0.3)

# ══════════════════════════════════════════════════════════════════════════
#  بخش ۵ — اگر داده‌ای پیدا شد، فیلتر صندوق‌های درآمد ثابت ETF
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۵ — لیست صندوق‌های درآمد ثابت ETF یافت‌شده")
log("=" * 72)

if fund_data:
    # کلیدهای ممکن برای نوع صندوق
    fixed_income_keywords = ["درآمد ثابت", "Fixed", "fixed", "1"]
    etf_funds = []
    for f in fund_data:
        fund_type = str(f.get("typeOfFund", f.get("TypeOfFund",
                       f.get("fundType", f.get("typeOfInvest", "")))))
        is_etf    = f.get("isETF", f.get("isTradedOnBourse", f.get("etf", False)))
        name      = f.get("name", f.get("fundName", f.get("persianName", "")))
        symbol    = f.get("symbol", f.get("ticker", f.get("regNo", "")))

        # فیلتر درآمد ثابت + ETF
        is_fi = any(kw in fund_type for kw in fixed_income_keywords)
        if is_fi and is_etf:
            etf_funds.append(f)

    log(f"  مجموع صندوق‌های پیدا شده: {len(fund_data)}")
    log(f"  صندوق‌های درآمد ثابت ETF: {len(etf_funds)}")
    log()
    log("  — dump اول ۳ صندوق —")
    for f in fund_data[:3]:
        log(f"  {json.dumps(f, ensure_ascii=False)}")

    if etf_funds:
        log()
        log("  ── خروجی آماده برای config.py ──")
        log("  FIXED_INCOME_ETFS = [")
        for f in etf_funds:
            name   = f.get("name", f.get("fundName", f.get("persianName", "?")))
            symbol = f.get("symbol", f.get("ticker", f.get("regNo", "?")))
            log(f'    {{"symbol": "{symbol}", "name": "{name}", "ins_code": "???"}},')
        log("  ]")
else:
    log("  هیچ داده‌ای از API پیدا نشد.")
    log()
    log("  ── راهنمای دستی (DevTools) ──")
    log("  1. مرورگر Chrome را باز کن")
    log("  2. برو به: https://fipiran.ir/mf/list")
    log("  3. F12 → Network → بالای صفحه فیلتر XHR یا Fetch را فعال کن")
    log("  4. صفحه را Reload کن (F5)")
    log("  5. در لیست Network ها دنبال درخواست‌هایی بگرد که:")
    log("     - Response آنها JSON است")
    log("     - در URL آنها 'fund' یا 'compare' یا 'list' باشد")
    log("  6. URL آن درخواست را اینجا کپی کن")
    log()
    log("  ── همچنین بررسی کن در Network tab ──")
    log("  مسیر: فیلتر All → به ترتیب Size مرتب کن → دنبال response بزرگ باش")

# ══════════════════════════════════════════════════════════════════════════
#  بخش ۶ — تست TSETMC search برای پیدا کردن ins_code نمادهای ناشناخته
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۶ — TSETMC GetInstrumentSearch برای صندوق‌های شناخته‌شده")
log("=" * 72)

# چند نماد که می‌دانیم صندوق درآمد ثابت هستند
known_symbols = [
    "امین یکم", "تصمیم", "سام", "کارآمد", "کارما", "کمند", "کیان",
    "ماني", "پاسارگاد", "پایش", "پارند", "اعتماد", "افران", "لبخند",
    "آفاق", "گنجین", "خاتم", "اوصتا", "فردا", "سخند", "یاقوت",
    "فیروزا", "صایند", "همای",
    # صندوق‌های احتمالی که ممکن است در لیست ما نباشند
    "کشتی", "نگین", "مشترک", "آسام", "نهال", "ثروتمند",
    "گنجینه زرین", "درسا", "توسعه", "آتیه", "فولاد", "صنعت",
]

tsetmc_cdn = "https://cdn.tsetmc.com/api"
tsetmc_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Referer": "https://www.tsetmc.com/",
}

# بهترین رویکرد: جستجو با کلمه «درآمد ثابت» تا ETF های موجود در TSETMC را پیدا کنیم
search_terms = ["درآمد ثابت", "fixed income", "ثابت اندیشه", "گنجینه", "پایدار"]
all_found = {}

for term in search_terms:
    url = f"{tsetmc_cdn}/Instrument/GetInstrumentSearch/{requests.utils.quote(term)}"
    r = get(url, headers=tsetmc_headers)
    if r and r.status_code == 200:
        try:
            data = r.json()
            instruments = data.get("instrumentSearch", [])
            log(f"  جستجو «{term}»: {len(instruments)} نتیجه")
            for inst in instruments:
                code  = inst.get("insCode", "")
                sym   = inst.get("lVal18AFC", "")
                name  = inst.get("lSoc30", "")
                flow  = inst.get("flow", 0)
                yVal  = inst.get("yVal", "")
                # فقط صندوق‌های ETF (flow=1 یا yVal=N→ بورسی)
                if code and sym:
                    all_found[code] = {"symbol": sym, "name": name, "flow": flow, "yVal": yVal}
                    log(f"    {sym:12s} {name:40s} flow={flow} yVal={yVal} code={code}")
        except Exception as e:
            log(f"  خطا در پارس: {e}")
    time.sleep(0.5)

# جستجو با اسامی خاص
log()
log("  جستجو با اسامی مستقیم نمادها:")
for sym in known_symbols[:15]:  # ۱۵ تا اول
    url = f"{tsetmc_cdn}/Instrument/GetInstrumentSearch/{requests.utils.quote(sym)}"
    r = get(url, headers=tsetmc_headers)
    if r and r.status_code == 200:
        try:
            data = r.json()
            instruments = data.get("instrumentSearch", [])
            for inst in instruments:
                code  = inst.get("insCode", "")
                s     = inst.get("lVal18AFC", "")
                name  = inst.get("lSoc30", "")
                flow  = inst.get("flow", 0)
                if code and s and code not in all_found:
                    all_found[code] = {"symbol": s, "name": name, "flow": flow}
        except:
            pass
    time.sleep(0.3)

if all_found:
    log()
    log(f"  ── جمع نمادهای یافت‌شده از TSETMC: {len(all_found)} ──")
    for code, info in all_found.items():
        log(f"    {info['symbol']:12s} {info['name']:40s} ins_code={code}")

# ──────────────────────────────────────────────────────────────────────────
log()
log("=" * 72)
log("  DONE")
log("=" * 72)

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
