"""tsetmc_all_fixed_income.py — استخراج دقیق صندوق‌های درآمد ثابت ETF

استراتژی اصلاح‌شده:
  - فقط نتایجی که lSoc30 آنها شامل «صندوق» است نگه می‌داریم
  - جستجوهای هدفمند با ترکیب «صندوق + حرف/کلمه» برای bypass کردن لیمیت ۴۱ نتیجه
  - تأیید نهایی با GetClosingPriceInfo (قیمت مثبت)

اجرا:
  python tools/tsetmc_all_fixed_income.py
"""

import sys, json, time, requests
from urllib.parse import quote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDN = "https://cdn.tsetmc.com/api"
OUT = "tools/tsetmc_all_fixed_income_report.txt"
lines = []

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tsetmc.com/",
    "Accept-Language": "fa-IR,fa;q=0.9",
}
S = requests.Session()
S.headers.update(H)

def log(s=""):
    print(s)
    lines.append(str(s))

def get(url, **kw):
    kw.setdefault("timeout", 25)
    try:
        return S.get(url, **kw)
    except Exception as e:
        return None

# ══════════════════════════════════════════════════════════════════════════
#  تابع جستجو — فقط نتایجی که «صندوق» در نام کامل دارند
# ══════════════════════════════════════════════════════════════════════════
fund_pool = {}  # insCode → {"symbol", "name", "flow"}

# صندوق‌های درآمد ثابت در نامشان معمولاً این عبارات را دارند
FI_KEYWORDS = ["درآمد ثابت", "درآمد", "ثابت", "پايدار", "پایدار",
               "اطمينان", "اطمینان", "آرامش", "امنيت", "امنیت"]
# صندوق‌هایی که باید حذف شوند (سهامی، طلا، کالا)
EXCLUDE_KEYWORDS = ["سهامي", "سهامی", "مختلط", "طلا", "جواهر", "كالايي",
                    "کالایی", "اهرم", "مشترك", "مشترک سهام", "فعال سهام",
                    "مسكن", "مسکن", "ريتون", "ریتون", "بازارگردان"]

def is_fixed_income(name: str) -> bool:
    """آیا نام صندوق نشان‌دهنده درآمد ثابت است؟"""
    if not name:
        return False
    # باید «صندوق» داشته باشد
    if "صندوق" not in name and "fund" not in name.lower():
        return False
    # بررسی حذف
    for ex in EXCLUDE_KEYWORDS:
        if ex in name:
            return False
    # اگر صراحتاً درآمد ثابت است
    if "درآمد ثابت" in name or "با درآمد" in name:
        return True
    # اگر «صندوق» دارد و کلمه منفی ندارد → احتمالاً قابل بررسی است
    return True

def search_and_filter(keyword: str) -> int:
    """جستجو در TSETMC و فیلتر صندوق‌ها. تعداد جدید برمی‌گرداند."""
    url = f"{CDN}/Instrument/GetInstrumentSearch/{quote(keyword)}"
    r = get(url)
    if not r or r.status_code != 200:
        return 0
    try:
        results = r.json().get("instrumentSearch", [])
    except:
        return 0

    new = 0
    for inst in results:
        code = inst.get("insCode", "")
        sym  = inst.get("lVal18AFC", "")
        name = inst.get("lSoc30", "")  # نام کامل
        flow = inst.get("flow", 0)

        if not code or not sym:
            continue
        # فیلتر اوراق اختیار و حق تقدم
        if any(sym.startswith(p) for p in ("ض", "ط", "ح")):
            continue
        # باید «صندوق» در نام کامل باشد
        if "صندوق" not in name:
            continue
        # حذف صندوق‌های غیر درآمد ثابت
        skip = False
        for ex in EXCLUDE_KEYWORDS:
            if ex in name:
                skip = True
                break
        if skip:
            continue

        if code not in fund_pool:
            fund_pool[code] = {"symbol": sym, "name": name, "flow": flow}
            new += 1
    return new


# ══════════════════════════════════════════════════════════════════════════
#  بخش A — جستجوهای هدفمند «صندوق + X»
# ══════════════════════════════════════════════════════════════════════════
log("=" * 72)
log("  A. جستجوی هدفمند صندوق‌ها در TSETMC")
log("=" * 72)
log()

# حروف فارسی برای جستجوی «صندوق سرمایه‌گذاری X»
# هر صندوق درآمد ثابت معمولاً نام‌هایی مثل «صندوق ... کمند»، «صندوق ... پارند» دارد
# جستجوی «صندوق» + پیشوند نام به تقسیم بیش از ۴۱ نتیجه کمک می‌کند

direct_terms = [
    # عبارات مستقیم صندوق درآمد ثابت
    "درآمد ثابت",
    "با درآمد ثابت",
    "صندوق درآمد",
    "صندوق با درآمد",
    "صندوق ثابت",
    "درآمد ثابت قابل",
    # اسامی خاص صندوق‌های شناخته‌شده
    "امين يكم", "تصميم", "كمند", "كيان", "ماني", "پارند", "اعتماد",
    "افران", "لبخند", "آفاق", "گنجين", "خاتم", "فردا", "ياقوت",
    "فيروزا", "هماي", "صايند", "سام درآمد", "كارآمد", "كارما",
    "پاسارگاد درآمد", "پايش", "آسود", "اطمينان درآمد", "اونيكس",
    "ترنج ثابت", "اكسيژن", "دامون",
    # اسامی بیشتر صندوق‌های احتمالی درآمد ثابت
    "نگين ثابت", "نهال درآمد", "آرزو ثابت", "آرمان درآمد",
    "توسعه درآمد", "رشد درآمد", "سپهر ثابت", "سينا درآمد",
    "آتيه درآمد", "سامان درآمد", "نوين ثابت", "حافظ ثابت",
    "آسا درآمد", "آسان درآمد", "شميم درآمد", "آوند درآمد",
    "ريكا درآمد", "آذين ثابت", "قابل معامله درآمد",
]

# جستجوی «صندوق + هر حرف الفبا» برای پوشش کامل
persian_letters = list("ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی")
for letter in persian_letters:
    direct_terms.append(f"صندوق {letter}")

log(f"  تعداد کل جستجوها: {len(direct_terms)}")
log()

for term in direct_terms:
    new = search_and_filter(term)
    if new > 0:
        log(f"  «{term}»: {new} جدید  (جمع: {len(fund_pool)})")
    time.sleep(0.3)

log()
log(f"  ── جمع صندوق‌های یافت‌شده با فیلتر «صندوق»: {len(fund_pool)} ──")


# ══════════════════════════════════════════════════════════════════════════
#  بخش B — تأیید با GetClosingPriceInfo
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  B. تأیید با قیمت زنده (GetClosingPriceInfo)")
log("=" * 72)
log()

verified = []

for code, info in fund_pool.items():
    url = f"{CDN}/ClosingPrice/GetClosingPriceInfo/{code}"
    r = get(url)
    if not r or r.status_code != 200:
        continue
    try:
        cp    = r.json().get("closingPriceInfo", {})
        price = cp.get("pClosing", 0)
        vol   = cp.get("qTotTran5J", 0)
        if price <= 0:
            continue
        # نام از search نتیجه بهتر از CP است
        name = info.get("name", "") or cp.get("lSoc30", "")
        sym  = info.get("symbol", "") or cp.get("lVal18AFC", "")
        flow = cp.get("flow", info.get("flow", 0))
        # فیلتر نهایی: باید «صندوق» در نام داشته باشد (double-check)
        if "صندوق" not in name:
            continue
        # حذف صندوق‌های سهامی/طلا/کالا
        skip = any(ex in name for ex in EXCLUDE_KEYWORDS)
        if skip:
            continue
        verified.append({
            "symbol":   sym,
            "name":     name,
            "ins_code": code,
            "price":    price,
            "volume":   vol,
            "flow":     flow,
        })
    except:
        pass
    time.sleep(0.2)

verified.sort(key=lambda x: x["volume"], reverse=True)

log(f"  ✓ صندوق‌های تأیید شده: {len(verified)}")
log()
log(f"  {'نماد':12s}  {'قیمت':>10s}  {'حجم روز':>16s}  fl  نام کامل")
log("  " + "─" * 90)
for f in verified:
    log(f"  {f['symbol']:12s}  {f['price']:>10,.0f}  {f['volume']:>16,.0f}  "
        f"{f['flow']:>2}  {f['name'][:50]}")


# ══════════════════════════════════════════════════════════════════════════
#  بخش C — فقط صندوق‌های احتمالی درآمد ثابت (فیلتر «درآمد» در نام)
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  C. صندوق‌های با احتمال بالای درآمد ثابت (نام شامل «درآمد»)")
log("=" * 72)
log()

fi_confirmed = [f for f in verified if "درآمد" in f["name"]]
fi_possible  = [f for f in verified if "درآمد" not in f["name"]]

log(f"  صندوق با «درآمد» در نام: {len(fi_confirmed)}")
log(f"  صندوق بدون «درآمد» (نیاز به بررسی): {len(fi_possible)}")
log()
log("  ── صندوق‌های تأیید‌شده درآمد ثابت ──")
for f in fi_confirmed:
    log(f"  {f['symbol']:12s}  {f['ins_code']}  {f['name'][:60]}")

log()
log("  ── صندوق‌های بدون «درآمد» در نام (بررسی کنید) ──")
for f in fi_possible:
    log(f"  {f['symbol']:12s}  {f['ins_code']}  {f['name'][:60]}")


# ══════════════════════════════════════════════════════════════════════════
#  بخش D — خروجی config.py
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  D. خروجی آماده برای config.py (فقط درآمد ثابت)")
log("=" * 72)
log()
log("FIXED_INCOME_ETFS = [")
for f in fi_confirmed:
    log(f'    {{"symbol": "{f["symbol"]}", "name": "{f["name"]}", '
        f'"ins_code": "{f["ins_code"]}"}},')
log("    # ── نیاز به بررسی دستی ──")
for f in fi_possible:
    log(f'    # {{"symbol": "{f["symbol"]}", "name": "{f["name"]}", '
        f'"ins_code": "{f["ins_code"]}"}},')
log("]")

log()
log("=" * 72)
log(f"  DONE — {len(fi_confirmed)} صندوق درآمد ثابت تأیید‌شده"
    f" + {len(fi_possible)} نیاز به بررسی")
log("=" * 72)

with open(OUT, "w", encoding="utf-8") as fout:
    fout.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
