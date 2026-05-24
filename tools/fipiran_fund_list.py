#!/usr/bin/env python3
"""
اسکریپت بررسی کامل fipiran.ir برای دریافت لیست صندوق‌های درآمد ثابت.

روی ماشینی با دسترسی به اینترنت ایران اجرا کنید:
    python tools/fipiran_fund_list.py

خروجی: tools/fipiran_fund_list_report.txt
"""
import sys, json, re, time
from pathlib import Path
import requests
from urllib.parse import urlencode, quote

# ── Windows UTF-8 ──────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).parent / "fipiran_fund_list_report.txt"
lines = []
def log(msg=""): print(msg); lines.append(str(msg))
def sep(t=""): log(); log("─"*72); log(f"  {t}") if t else None; log("─"*72)

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

s = requests.Session()
s.headers.update(BASE_HEADERS)

def get(url, **kwargs):
    try:
        r = s.get(url, timeout=20, **kwargs)
        log(f"  GET {url}")
        log(f"      → {r.status_code}  {len(r.content):,} bytes  "
            f"Content-Type: {r.headers.get('Content-Type','?')}")
        return r
    except Exception as e:
        log(f"  GET {url}  → ERROR: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════
# A. صفحه اصلی fipiran.ir/mf/list  —  بررسی SPA shell + کوکی‌ها
# ════════════════════════════════════════════════════════════════════════
sep("A. صفحه fipiran.ir/mf/list")

r = get("https://fipiran.ir/mf/list")
if r:
    log(f"  Cookie jar: {dict(s.cookies)}")
    # اگر HTML واقعی است (نه SPA)، چند خط اول را نشان بده
    text = r.text[:2000].replace("\n"," ")
    log(f"  Preview: {text[:300]}")


# ════════════════════════════════════════════════════════════════════════
# B. تست endpoint های شناخته‌شده API فیپیران
# ════════════════════════════════════════════════════════════════════════
sep("B. تست API endpoint های فیپیران")

FIPIRAN_ENDPOINTS = [
    # v1 fund API
    "https://fund.fipiran.ir/api/v1/fund/fundcompare",
    "https://fund.fipiran.ir/api/v1/fund/fundcompare?TypeOfInvest=Fixed",
    "https://fund.fipiran.ir/api/v1/fund/fundcompare?typeOfFund=1",
    "https://fund.fipiran.ir/api/v1/fund/fundlist",
    "https://fund.fipiran.ir/api/v1/fund/fundlist?TypeOfFund=1",
    "https://fund.fipiran.ir/api/v1/fund/fundlist?TypeOfInvest=Fixed",
    # v2
    "https://fund.fipiran.ir/api/v2/fund/fundlist",
    "https://fund.fipiran.ir/api/v2/fund/fundlist?TypeOfFund=1",
    # فیپیران اصلی
    "https://fipiran.ir/api/v1/fund/fundlist",
    "https://fipiran.ir/api/v1/fund/fundcompare",
    "https://fipiran.ir/DataService/FundCompare",
    "https://fipiran.ir/DataService/Funds",
    # صفحه ETF های بورسی
    "https://fipiran.ir/MFBourse/FundList",
    "https://fipiran.ir/mf/FundList",
    # زیردامنه‌های احتمالی
    "https://api.fipiran.ir/v1/fund/fundlist",
    "https://api.fipiran.ir/fund/list",
]

json_endpoints = []
for url in FIPIRAN_ENDPOINTS:
    r = get(url, headers={**BASE_HEADERS, "Referer": "https://fipiran.ir/"})
    if not r:
        continue
    # آیا JSON است؟
    try:
        data = r.json()
        log(f"  ✓ JSON! type={type(data).__name__}  "
            f"len={len(data) if isinstance(data, list) else list(data.keys())[:6]}")
        if isinstance(data, list) and len(data) > 0:
            log(f"    First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
        json_endpoints.append((url, data))
        # چند مورد اول را نشان بده
        items = data if isinstance(data, list) else (
            data.get("items") or data.get("data") or data.get("result") or []
        )
        if isinstance(items, list):
            for item in items[:3]:
                if isinstance(item, dict):
                    log(f"    Sample: {json.dumps({k:v for k,v in list(item.items())[:8]}, ensure_ascii=False)}")
    except Exception:
        log(f"  — not JSON, HTML size={len(r.content)}")
    time.sleep(0.3)


# ════════════════════════════════════════════════════════════════════════
# C. تلاش با AJAX headers  (سایت SPA اغلب با XHR headers پاسخ می‌دهد)
# ════════════════════════════════════════════════════════════════════════
sep("C. تلاش با AJAX + referer headers")

AJAX_HEADERS = {
    **BASE_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://fipiran.ir/mf/list",
    "Origin":  "https://fipiran.ir",
    "Accept":  "application/json, text/javascript, */*; q=0.01",
}

ajax_targets = [
    "https://fipiran.ir/mf/MFundList",
    "https://fipiran.ir/mf/GetFundList",
    "https://fipiran.ir/mf/FundData",
    "https://fipiran.ir/DataService/FundCompare?fundType=1&isETF=true",
    "https://fipiran.ir/DataService/FundCompare?typeOfFund=%D8%AF%D8%B1%D8%A2%D9%85%D8%AF+%D8%AB%D8%A7%D8%A8%D8%AA",
]

for url in ajax_targets:
    r = get(url, headers=AJAX_HEADERS)
    if not r:
        continue
    try:
        data = r.json()
        log(f"  ✓ JSON from AJAX! keys={list(data.keys())[:8] if isinstance(data,dict) else len(data)}")
        json_endpoints.append((url, data))
    except Exception:
        log(f"  — not JSON")
    time.sleep(0.3)


# ════════════════════════════════════════════════════════════════════════
# D. بررسی صفحه با BeautifulSoup (شاید HTML با داده render شده باشد)
# ════════════════════════════════════════════════════════════════════════
sep("D. پارس HTML صفحه mf/list")

try:
    from bs4 import BeautifulSoup
    r = get("https://fipiran.ir/mf/list",
            headers={**BASE_HEADERS, "Accept": "text/html"})
    if r:
        soup = BeautifulSoup(r.text, "html.parser")

        # جستجو برای JSON در تگ‌های script
        scripts = soup.find_all("script")
        log(f"  تعداد تگ script: {len(scripts)}")
        for i, sc in enumerate(scripts):
            text = sc.string or ""
            if len(text) > 200 and ("{" in text or "[" in text):
                log(f"  Script[{i}] ({len(text)} chars): {text[:200].replace(chr(10),' ')}")

        # جستجو برای جدول
        tables = soup.find_all("table")
        log(f"\n  تعداد جدول: {len(tables)}")
        for i, tbl in enumerate(tables[:3]):
            headers = [th.get_text(strip=True) for th in tbl.find_all("th")]
            rows = tbl.find_all("tr")
            log(f"  Table[{i}]: headers={headers[:8]}  rows={len(rows)}")

        # جستجو برای div با کلاس‌های مرتبط
        divs = soup.find_all("div", {"id": True})
        log(f"\n  div‌های دارای id: {[d['id'] for d in divs[:20]]}")

        # Next.js یا React state
        next_data = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data:
            log("\n  ✓ __NEXT_DATA__ پیدا شد:")
            try:
                nd = json.loads(next_data.string)
                log(f"    keys: {list(nd.keys())}")
                # برگ های props/pageProps
                pp = nd.get("props", {}).get("pageProps", {})
                log(f"    pageProps keys: {list(pp.keys())[:10]}")
            except Exception as e:
                log(f"    parse error: {e}")
        else:
            log("\n  — __NEXT_DATA__ یافت نشد")

except ImportError:
    log("  BeautifulSoup نصب نیست: pip install beautifulsoup4")


# ════════════════════════════════════════════════════════════════════════
# E. تلاش با fund.fipiran.ir  (اغلب SPA ها از زیردامنه API می‌گیرند)
# ════════════════════════════════════════════════════════════════════════
sep("E. fund.fipiran.ir — تست endpoint های مختلف")

FUND_API = "https://fund.fipiran.ir"

# ابتدا صفحه اصلی را می‌گیریم تا کوکی و CSRF بگیریم
r0 = get(f"{FUND_API}/")
if r0:
    log(f"  Cookies after homepage: {dict(s.cookies)}")

fund_tests = [
    f"{FUND_API}/api/v1/fund/fundcompare",
    f"{FUND_API}/api/v1/fund/fundcompare?TypeOfInvest=Fixed&ReturnType=1",
    f"{FUND_API}/api/v1/fund/fundcompare?typeOfFund=1",
    f"{FUND_API}/api/v1/fund/fundlist",
    f"{FUND_API}/api/v1/fund/fundlist?pageNumber=1&pageSize=200",
    f"{FUND_API}/api/v1/fund/fundlist?pageNumber=1&pageSize=200&typeOfFund=1",
    f"{FUND_API}/api/v1/fund/fundlist?pageNumber=1&pageSize=200&isETF=true",
    f"{FUND_API}/api/v1/fund/chart/getfundnetasset",
    f"{FUND_API}/api/v1/fund/treemap",
    f"{FUND_API}/api/v1/fund/treemap?typeOfFund=1",
]

for url in fund_tests:
    r = get(url, headers={**AJAX_HEADERS, "Referer": f"{FUND_API}/"})
    if not r:
        continue
    try:
        data = r.json()
        log(f"  ✓ JSON!")
        if isinstance(data, list):
            log(f"    list len={len(data)}")
            if data and isinstance(data[0], dict):
                log(f"    keys: {list(data[0].keys())[:12]}")
                # نشان دادن صندوق‌های درآمد ثابت
                fi = [x for x in data if
                      "درآمد ثابت" in str(x.get("typeOfFund","")) or
                      "درآمد ثابت" in str(x.get("fundType","")) or
                      "Fixed" in str(x.get("typeOfFund","")) or
                      "1" == str(x.get("typeOfFund",""))]
                log(f"    صندوق‌های درآمد ثابت (تخمینی): {len(fi)}")
                for item in fi[:5]:
                    log(f"      {json.dumps({k:v for k,v in list(item.items())[:10]}, ensure_ascii=False)}")
        elif isinstance(data, dict):
            log(f"    dict keys: {list(data.keys())[:12]}")
            items = (data.get("items") or data.get("data") or
                     data.get("result") or data.get("funds") or [])
            if items:
                log(f"    items count: {len(items)}")
                if items and isinstance(items[0], dict):
                    log(f"    item keys: {list(items[0].keys())[:12]}")
        json_endpoints.append((url, data))
    except Exception:
        pass
    time.sleep(0.3)


# ════════════════════════════════════════════════════════════════════════
# F. تحلیل داده‌های JSON پیدا شده
# ════════════════════════════════════════════════════════════════════════
sep("F. تحلیل داده‌های JSON پیدا شده — صندوق‌های درآمد ثابت ETF")

all_fi_funds = []

for url, data in json_endpoints:
    items = data if isinstance(data, list) else (
        data.get("items") or data.get("data") or data.get("result") or
        data.get("funds") or data.get("Items") or []
    )
    if not isinstance(items, list) or not items:
        continue

    log(f"\n  از {url}: {len(items)} آیتم")
    log(f"  کلیدهای آیتم: {list(items[0].keys()) if isinstance(items[0],dict) else '?'}")

    for item in items:
        if not isinstance(item, dict):
            continue

        # تشخیص نوع صندوق
        fund_type = str(
            item.get("typeOfFund") or item.get("fundType") or
            item.get("TypeOfFund") or item.get("FundType") or ""
        )
        is_etf_raw = (
            item.get("isETF") or item.get("isEtf") or
            item.get("IsETF") or item.get("IsEtf") or ""
        )
        name = str(
            item.get("name") or item.get("Name") or
            item.get("fundName") or item.get("FundName") or ""
        )

        # فیلتر: درآمد ثابت + ETF
        is_fi  = "درآمد ثابت" in fund_type or "Fixed" in fund_type or fund_type in ("1","2")
        is_etf = is_etf_raw in (True, 1, "1", "true", "True") or "ETF" in str(is_etf_raw).upper()

        if is_fi and is_etf:
            all_fi_funds.append({
                "url":       url,
                "name":      name,
                "symbol":    str(item.get("symbol") or item.get("ticker") or ""),
                "insCode":   str(item.get("insCode") or item.get("ins_code") or ""),
                "fundType":  fund_type,
                "cancelNAV": item.get("cancelNAV") or item.get("cancelNav") or "",
                "raw":       item,
            })

log(f"\n{'═'*72}")
log(f"  مجموع صندوق‌های درآمد ثابت ETF پیدا شده: {len(all_fi_funds)}")
log(f"{'═'*72}")
for f in all_fi_funds:
    log(f"  {f['symbol']:15}  {f['insCode']:22}  {f['name'][:45]}")

if not all_fi_funds:
    log("\n  هیچ صندوقی یافت نشد — احتمالاً باید داده را از HTML بگیریم.")
    log("  خروجی HTML صفحه mf/list را در فایل زیر ببینید.")


# ════════════════════════════════════════════════════════════════════════
# G. کامل‌ترین لیست — نشان دادن همه کلیدهای موجود برای اولین آیتم
# ════════════════════════════════════════════════════════════════════════
sep("G. dump کامل اولین آیتم هر endpoint موفق")

for url, data in json_endpoints[:3]:
    items = data if isinstance(data, list) else (
        data.get("items") or data.get("data") or data.get("result") or
        data.get("funds") or []
    )
    if isinstance(items, list) and items and isinstance(items[0], dict):
        log(f"\n  [{url}] — اولین آیتم (همه کلیدها):")
        for k, v in sorted(items[0].items()):
            log(f"    {k:40} = {str(v)[:80]}")


# ════════════════════════════════════════════════════════════════════════
# H. تست TSETMC برای یافتن ins_code صندوق‌های پیدا شده
# ════════════════════════════════════════════════════════════════════════
sep("H. تست TSETMC GetInstrumentSearch برای صندوق‌های یافت‌شده")

CDN = "https://cdn.tsetmc.com/api"
TSETMC_HEADERS = {**BASE_HEADERS, "Referer": "https://www.tsetmc.com/"}

if all_fi_funds:
    for fund in all_fi_funds[:10]:
        sym = fund["symbol"] or fund["name"][:10]
        url = f"{CDN}/Instrument/GetInstrumentSearch/{quote(sym)}"
        r = get(url, headers=TSETMC_HEADERS)
        if r:
            try:
                data = r.json()
                results = data.get("instrumentSearch", [])
                fund_results = [x for x in results if "صندوق" in x.get("lVal30","")]
                log(f"  {sym}: {len(fund_results)} صندوق در TSETMC")
                for fr in fund_results[:3]:
                    log(f"    {fr.get('lVal18AFC',''):15}  {fr.get('insCode',''):22}  {fr.get('lVal30','')[:40]}")
                    # ذخیره ins_code
                    if not fund["insCode"] and fund_results:
                        fund["insCode"] = fund_results[0].get("insCode","")
            except Exception:
                pass
        time.sleep(0.2)
else:
    log("  صندوقی یافت نشده بود — تست TSETMC انجام نمی‌شود")
    log("  برای تست دستی:")
    log("  python -c \"import requests; r=requests.get('https://cdn.tsetmc.com/api/Instrument/GetInstrumentSearch/درآمد ثابت'); print(r.text[:500])\"")


# ════════════════════════════════════════════════════════════════════════
# I. خروجی config.py آماده
# ════════════════════════════════════════════════════════════════════════
sep("I. خروجی آماده برای config.py")

if all_fi_funds:
    log("\nFIXED_INCOME_ETFS = [")
    for f in all_fi_funds:
        sym  = f["symbol"]  or "???"
        name = f["name"]    or "???"
        ic   = f["insCode"] or ""
        log(f'    {{"symbol": "{sym}", "name": "{name}", "ins_code": "{ic}"}},')
    log("]")
else:
    log("  داده‌ای برای تولید config.py یافت نشد.")
    log("\n  ── دستورالعمل دستی ──")
    log("  1. مرورگر را باز کنید: https://fipiran.ir/mf/list")
    log("  2. DevTools → Network → XHR/Fetch را فیلتر کنید")
    log("  3. درخواست‌های API را پیدا کنید")
    log("  4. URL آنها را در اینجا گزارش دهید")


# ────────────────────────────────────────────────────────────────────────
sep("DONE")
report = "\n".join(lines)
OUT.write_text(report, encoding="utf-8")
print(f"\nگزارش ذخیره شد: {OUT}")
