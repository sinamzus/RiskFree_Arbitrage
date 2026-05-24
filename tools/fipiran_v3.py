"""fipiran_v3.py — تلاش برای دسترسی به fund.fipiran.ir از طریق IP مستقیم

یافته‌های قبلی:
  - JS bundle فیپیران: fundlistissuebyfundtype?fundType=6
  - fund.fipiran.ir → DNS FAIL
  - fipiran.ir → 46.102.143.223

راه‌حل‌های این اسکریپت:
  1. اضافه کردن hosts entry به صورت خودکار (نیاز به Admin)
  2. تست endpoint های پیدا شده در bundle
  3. تأیید ins_code نمادهای مشکوک در TSETMC

اجرا (با دسترسی Admin در ویندوز):
  python tools/fipiran_v3.py

اگر بدون Admin ران کنید، بخش hosts را skip می‌کند.
"""

import re
import sys
import json
import time
import socket
import subprocess
import requests
from urllib.parse import quote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FIPIRAN_IP   = "46.102.143.223"
FUND_HOST    = "fund.fipiran.ir"
OUT          = "tools/fipiran_v3_report.txt"
lines        = []

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8",
    "Referer": "https://fipiran.ir/mf/list",
    "X-Requested-With": "XMLHttpRequest",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def log(s=""):
    print(s)
    lines.append(str(s))

def get(url, **kw):
    kw.setdefault("timeout", 20)
    try:
        return SESSION.get(url, **kw)
    except Exception as e:
        return None


# ══════════════════════════════════════════════════════════════════════════
#  بخش ۱ — اضافه کردن hosts entry
# ══════════════════════════════════════════════════════════════════════════
log("=" * 72)
log("  بخش ۱ — اضافه کردن fund.fipiran.ir به hosts")
log("=" * 72)

import platform
is_windows = platform.system() == "Windows"
hosts_path = r"C:\Windows\System32\drivers\etc\hosts" if is_windows else "/etc/hosts"

# بررسی آیا hosts entry از قبل وجود دارد
hosts_has_entry = False
try:
    with open(hosts_path, "r", encoding="utf-8", errors="replace") as f:
        hosts_content = f.read()
    if FUND_HOST in hosts_content and FIPIRAN_IP in hosts_content:
        hosts_has_entry = True
        log(f"  ✓ hosts entry از قبل وجود دارد: {FIPIRAN_IP} {FUND_HOST}")
    else:
        log(f"  — hosts entry وجود ندارد، تلاش برای اضافه کردن...")
        try:
            entry = f"\n{FIPIRAN_IP} {FUND_HOST}\n"
            with open(hosts_path, "a", encoding="utf-8") as f:
                f.write(entry)
            hosts_has_entry = True
            log(f"  ✓ با موفقیت اضافه شد: {FIPIRAN_IP} {FUND_HOST}")
        except PermissionError:
            log(f"  ✗ خطای دسترسی — لازم است به صورت Admin ران کنید")
            log(f"    یا دستی این خط را به {hosts_path} اضافه کنید:")
            log(f"    {FIPIRAN_IP} {FUND_HOST}")
except Exception as e:
    log(f"  خطا در خواندن hosts: {e}")

# تست DNS بعد از تغییر
log()
for d in [FUND_HOST, "fipiran.ir"]:
    try:
        ip = socket.gethostbyname(d)
        log(f"  DNS ✓ {d} → {ip}")
    except Exception as e:
        log(f"  DNS ✗ {d} → {e}")


# ══════════════════════════════════════════════════════════════════════════
#  بخش ۲ — تست endpoint های پیدا شده از JS bundle
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۲ — تست endpoint های کشف‌شده از JS bundle فیپیران")
log("=" * 72)
log("  (از bundle: fundlistissuebyfundtype?fundType=6)")
log()

# fundType های احتمالی:
# 1=سهامی ETF, 2=مختلط, 3=سهامی معمولی, 4=مختلط معمولی
# 5=درآمدثابت معمولی, 6=درآمد ثابت ETF, 7=طلا ETF, 8=کالایی

endpoints = []
for base in [f"https://{FUND_HOST}", "https://fipiran.ir"]:
    for ft in [6, 5, 1, 2, 3, 4, 7, 8]:
        endpoints.append(
            f"{base}/api/v1/fund/fundlistissuebyfundtype?fundType={ft}"
        )
    # endpoint های دیگر از bundle
    endpoints += [
        f"{base}/api/v1/fund/fundlist",
        f"{base}/api/v1/fund/fundlist?pageNumber=1&pageSize=500",
        f"{base}/api/v1/fund/fundlist?pageNumber=1&pageSize=500&typeOfFund=Fixed",
        f"{base}/api/v1/fund/fundlistbrief",
        f"{base}/api/v1/fund/fundlistbrief?typeOfFund=Fixed",
        f"{base}/api/v1/fund/fundcompare",
        f"{base}/api/v1/fund/fundcompare?TypeOfInvest=Fixed&ReturnType=1",
        f"{base}/api/v1/fund/treemap",
        f"{base}/api/v1/fund/treemap?typeOfFund=Fixed",
    ]

all_fund_data = {}  # fundType → list of funds

for url in endpoints:
    r = get(url)
    if r is None:
        log(f"  ✗ ERROR  {url}")
        continue
    ct   = r.headers.get("Content-Type", "?")
    size = len(r.content)

    if size < 100:
        continue  # skip tiny responses silently

    if "application/json" in ct:
        log(f"  ✓ JSON ({size:,}B)  {url}")
        try:
            data = r.json()
            if isinstance(data, list):
                log(f"     list[{len(data)}]  first_keys={list(data[0].keys()) if data else '?'}")
                ft_key = url.split("fundType=")[-1].split("&")[0] if "fundType=" in url else "?"
                if data and ft_key not in all_fund_data:
                    all_fund_data[ft_key] = data
                    # dump اولین item
                    log(f"     نمونه اول: {json.dumps(data[0], ensure_ascii=False)[:200]}")
            elif isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list) and v:
                        log(f"     {k}: list[{len(v)}]  first_keys={list(v[0].keys()) if isinstance(v[0], dict) else '?'}")
                        all_fund_data[k] = v
                    elif isinstance(v, (int, str)):
                        log(f"     {k}: {v}")
        except Exception as e:
            log(f"     JSON error: {e}")
            log(f"     preview: {r.text[:150]}")
    elif "html" in ct and size == 1532:
        pass  # SPA shell
    elif "html" in ct and size != 1532:
        log(f"  ? HTML ({size}B)  {url}")
        log(f"     {r.text[:100]}")
    elif size > 100:
        log(f"  ? {ct} ({size}B)  {url}")
        log(f"     {r.text[:100]}")
    time.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════════
#  بخش ۳ — اگر داده پیدا شد، لیست درآمد ثابت ETF
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۳ — صندوق‌های درآمد ثابت ETF یافت‌شده")
log("=" * 72)

if all_fund_data:
    log(f"  کل fund_type هایی که داده دارند: {list(all_fund_data.keys())}")
    # fundType=6 معمولاً درآمد ثابت ETF است
    fi_etf = all_fund_data.get("6", [])
    if not fi_etf:
        # اگر ۶ نبود، در همه جستجو کن
        for key, items in all_fund_data.items():
            for f in items:
                ft = str(f.get("typeOfFund", f.get("fundType", "")))
                is_etf = f.get("isETF", f.get("isTradedOnBourse", True))
                if ("Fixed" in ft or "درآمد" in ft or "6" in ft) and is_etf:
                    fi_etf.append(f)

    log(f"  صندوق‌های درآمد ثابت ETF (fundType=6): {len(fi_etf)}")
    log()
    for f in fi_etf:
        name   = f.get("name", f.get("fundName", "?"))
        symbol = f.get("symbol", f.get("ticker", f.get("regNo", "?")))
        reg_no = f.get("regNo", "")
        log(f"    {symbol:15s} {name}")

    if fi_etf:
        log()
        log("  ── dump کامل اولین صندوق ──")
        log(json.dumps(fi_etf[0], ensure_ascii=False, indent=2))
else:
    log("  هیچ داده‌ای از API فیپیران پیدا نشد.")
    log()
    log("  ── راه‌حل دستی ──")
    log(f"  1. این خط را به {hosts_path} اضافه کن:")
    log(f"     {FIPIRAN_IP} {FUND_HOST}")
    log("  2. مرورگر Chrome را باز کن")
    log("  3. برو به: https://fipiran.ir/mf/list")
    log("  4. F12 → Network → فیلتر: Fetch/XHR")
    log("  5. صفحه را Reload کن")
    log("  6. دنبال درخواست‌هایی بگرد که URL آنها شامل:")
    log("     'fundlist' یا 'fundcompare' یا 'fundlistissuebyfundtype' باشد")
    log("  7. URL کامل آن درخواست را اینجا بنویس")


# ══════════════════════════════════════════════════════════════════════════
#  بخش ۴ — تأیید ins_code نمادهای مشکوک از TSETMC
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  بخش ۴ — تأیید ins_code نمادهای مشکوک")
log("=" * 72)

TSETMC_CDN = "https://cdn.tsetmc.com/api"
tsetmc_h = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Referer": "https://www.tsetmc.com/",
}

# نمادهایی که ins_code آنها از جستجو تأیید نشد
suspects = {
    "خاتم":   "18865325633315847",
    "اوصتا":  "57761388729898548",
    "فردا":   "65249046611427924",
    "سخند":   "59598536122397373",
    "یاقوت":  "1438514795814416",
    "فیروزا": "10795723506538053",
    "همای":   "15494954332657697",
}

# نمادهای جدید کشف‌شده برای تأیید
new_finds = {
    "آسود":   "16582961426722208",
    "اطمینان":"50243708970398750",
    "اونیکس": "23498719713662118",
    "ترنج":   "50264175787486822",
    "اکسیژن": "44558786393585356",
    "دامون":  "43009306066217458",
}

def verify_ins_code(symbol, ins_code):
    """تأیید یک ins_code از TSETMC — بررسی می‌کند که آیا داده وجود دارد."""
    # روش ۱: GetClosingPriceInfo
    url = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}"
    r = get(url, headers=tsetmc_h)
    if r and r.status_code == 200:
        try:
            data = r.json()
            cp = data.get("closingPriceInfo", {})
            price = cp.get("pClosing", 0)
            name  = cp.get("lVal18AFC", "")
            if price > 0:
                return True, f"قیمت={price:,.0f}  نام={name}"
        except:
            pass
    # روش ۲: GetInstrumentInfo
    url2 = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
    r2 = get(url2, headers=tsetmc_h)
    if r2 and r2.status_code == 200:
        try:
            data2 = r2.json()
            info  = data2.get("instrumentInfo", {})
            if info:
                return True, f"topInst={info.get('topInst','')}  dEven={info.get('dEven','')}"
        except:
            pass
    return False, "داده‌ای یافت نشد"

log()
log("  — نمادهای مشکوک (تأیید نشده از جستجو) —")
for sym, code in suspects.items():
    ok, detail = verify_ins_code(sym, code)
    status = "✓" if ok else "✗"
    log(f"  {status} {sym:10s} {code}  {detail}")
    time.sleep(0.4)

log()
log("  — نمادهای جدید (تأیید ins_code از TSETMC search 2026-05-24) —")
for sym, code in new_finds.items():
    ok, detail = verify_ins_code(sym, code)
    status = "✓" if ok else "✗"
    log(f"  {status} {sym:10s} {code}  {detail}")
    time.sleep(0.4)

# ── جستجوی مستقیم برای نمادهای مشکوک ──
log()
log("  — جستجوی مستقیم TSETMC برای نمادهای مشکوک —")
for sym in suspects.keys():
    url = f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{quote(sym)}"
    r = get(url, headers=tsetmc_h)
    if r and r.status_code == 200:
        try:
            results = r.json().get("instrumentSearch", [])
            if results:
                for inst in results[:3]:
                    code = inst.get("insCode", "")
                    name = inst.get("lSoc30", "")
                    s    = inst.get("lVal18AFC", "")
                    flow = inst.get("flow", 0)
                    log(f"    {sym}: {s:12s} {name:35s} flow={flow} code={code}")
            else:
                log(f"    {sym}: — نتیجه‌ای نیافت")
        except Exception as e:
            log(f"    {sym}: خطا: {e}")
    time.sleep(0.4)

# ──────────────────────────────────────────────────────────────────────────
log()
log("=" * 72)
log("  DONE")
log("=" * 72)

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
