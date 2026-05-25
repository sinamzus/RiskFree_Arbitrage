"""tsetmc_all_fixed_income.py — استخراج دقیق صندوق‌های درآمد ثابت ETF

فیلد کلیدی کشف‌شده از debug:
  cgrValCot == "H1"  →  «بازار صندوق های قابل معامله» (ETF)
  lVal30             →  نام کامل ۳۰ کاراکتری (نه lSoc30 که خالی بود)

استراتژی:
  - جستجوی کلمه‌کلیدی + فیلتر cgrValCot=="H1" → فقط صندوق ETF
  - فیلتر ثانویه: «درآمد» یا «ثابت» در lVal30 → فقط درآمد ثابت
  - تأیید با GetClosingPriceInfo

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tsetmc.com/",
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

# صندوق‌های غیر درآمد ثابت که باید حذف شوند
EXCLUDE = ["سهامي", "سهامی", "مختلط", "طلا", "جواهر", "كالايي", "کالایی",
           "اهرم", "مسكن", "مسکن", "بازارگردان", "ريتون", "ریتون",
           "فعال سهام", "خاص سهام"]

def is_fixed_income(name: str) -> bool:
    """آیا صندوق ETF از نوع درآمد ثابت است؟"""
    for ex in EXCLUDE:
        if ex in name:
            return False
    # تأیید درآمد ثابت:
    # - «درآمد» یا «ثابت» در نام (صراحتاً)
    # - پسوند «-ثابت» (صندوق‌های بورس)
    # - پسوند «-د» یا «- د» (مخفف درآمد ثابت برای فرابورس ETF)
    # - «پايدار» (مثل پارند پايدار سپهر)
    if any(x in name for x in ("درآمد", "ثابت", "پايدار", "-د", "- د")):
        return True
    return True  # اگر هیچ کلمه منفی ندارد نگه می‌داریم

# ══════════════════════════════════════════════════════════════════════════
#  بخش A — جستجو با فیلتر cgrValCot=="H1"
# ══════════════════════════════════════════════════════════════════════════
log("=" * 72)
log("  A. جستجوی صندوق‌های ETF با فیلتر cgrValCot=H1")
log("=" * 72)
log()

etf_pool = {}  # insCode → {"symbol", "name", "flow"}

# کلمات کلیدی هدفمند — هر کدام حداکثر ۴۱ نتیجه برمی‌گرداند
# با جستجوی «صندوق + حرف» می‌توان همه را پوشش داد
keywords = []

# عبارات مستقیم درآمد ثابت
keywords += [
    "درآمد ثابت", "با درآمد ثابت", "صندوق درآمد",
    "صندوق با درآمد", "صندوق ثابت", "درآمد ثابت قابل",
]

# اسامی صندوق‌های شناخته‌شده — بورس (H1)
keywords += [
    "امين يكم", "تصميم", "كمند", "كيان", "ماني", "پارند",
    "افران", "آفاق", "خاتم", "فردا", "ياقوت", "فيروزا", "هماي",
    "آوند", "ريكا", "آذين", "آرامش",
]
# اسامی صندوق‌های شناخته‌شده — فرابورس (1A)
keywords += [
    "اعتماد آفرين", "لبخند", "گنجين", "صايند", "كارآمد", "كارما",
    "پاسارگاد درآمد", "پايش", "آسود", "اطمينان", "اونيكس", "ترنج ثابت",
    "اكسيژن", "دامون", "سام درآمد", "اوصتا", "سخند", "شميم",
    "آسان درآمد", "نگين ثابت", "آرمان ثابت", "سپهر ثابت", "سينا درآمد",
    "ابزار نوين", "ابزارهاي نوين",
]

# جستجوی «صندوق + هر حرف الفبا» — برای پوشش همه نام‌ها
for letter in "ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی":
    keywords.append(f"صندوق {letter}")

log(f"  تعداد جستجوها: {len(keywords)}")
log()

for kw in keywords:
    url = f"{CDN}/Instrument/GetInstrumentSearch/{quote(kw)}"
    r = get(url)
    if not r or r.status_code != 200:
        continue
    try:
        results = r.json().get("instrumentSearch", [])
    except:
        continue

    new = 0
    for inst in results:
        code  = inst.get("insCode", "")
        sym   = inst.get("lVal18AFC", "")
        name  = inst.get("lVal30", "")          # ← فیلد صحیح کشف‌شده
        flow  = inst.get("flow", 0)
        cgr   = inst.get("cgrValCot", "")       # ← فیلتر ETF صندوق

        if not code:
            continue
        # ── فیلتر اصلی: ETF صندوق بورس (H1) یا فرابورس (1A) ──
        if cgr not in ("H1", "1A"):
            continue
        # فیلتر نوع صندوق
        if not is_fixed_income(name):
            continue

        if code not in etf_pool:
            etf_pool[code] = {"symbol": sym, "name": name, "flow": flow}
            new += 1

    if new > 0:
        log(f"  «{kw}»: {new} جدید  (جمع: {len(etf_pool)})")
    time.sleep(0.25)

log()
log(f"  ── جمع صندوق‌های ETF یافت‌شده: {len(etf_pool)} ──")


# ══════════════════════════════════════════════════════════════════════════
#  بخش B — تأیید با GetClosingPriceInfo
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  B. تأیید با قیمت زنده")
log("=" * 72)
log()

verified = []

for code, info in etf_pool.items():
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
        verified.append({
            "symbol":   info["symbol"],
            "name":     info["name"],
            "ins_code": code,
            "price":    price,
            "volume":   vol,
            "flow":     info["flow"],
        })
    except:
        pass
    time.sleep(0.2)

verified.sort(key=lambda x: x["volume"], reverse=True)

log(f"  ✓ صندوق‌های تأیید شده: {len(verified)}")
log()
log(f"  {'نماد':12s}  {'قیمت':>10s}  {'حجم روز':>16s}  fl  نام")
log("  " + "─" * 85)
for f in verified:
    fi_tag = "✓درآمدثابت" if ("درآمد" in f["name"] or "ثابت" in f["name"]) else "؟بررسی‌شود"
    log(f"  {f['symbol']:12s}  {f['price']:>10,.0f}  {f['volume']:>16,.0f}"
        f"  {f['flow']:>2}  {fi_tag}  {f['name']}")


# ══════════════════════════════════════════════════════════════════════════
#  بخش C — جداسازی درآمد ثابت از سایر صندوق‌های ETF
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  C. جداسازی درآمد ثابت از سایر ETF ها")
log("=" * 72)
log()

fi   = [f for f in verified if "درآمد" in f["name"] or "ثابت" in f["name"] or "پايدار" in f["name"]]
oth  = [f for f in verified if f not in fi]

log(f"  ✓ درآمد ثابت (قطعی): {len(fi)}")
log(f"  ؟ سایر ETF (نیاز به بررسی): {len(oth)}")
if oth:
    log()
    log("  ── سایر ETF ها (شاید درآمد ثابت باشند — بررسی کنید) ──")
    for f in oth:
        log(f"  {f['symbol']:12s}  {f['ins_code']}  {f['name']}")


# ══════════════════════════════════════════════════════════════════════════
#  بخش D — خروجی config.py
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  D. خروجی آماده برای config.py")
log("=" * 72)
log()
log("FIXED_INCOME_ETFS = [")
for f in fi:
    log(f'    {{"symbol": "{f["symbol"]}", "name": "{f["name"]}", '
        f'"ins_code": "{f["ins_code"]}"}},')
if oth:
    log("    # ── نیاز به بررسی دستی ──")
    for f in oth:
        log(f'    # {{"symbol": "{f["symbol"]}", "name": "{f["name"]}", '
            f'"ins_code": "{f["ins_code"]}"}},')
log("]")

log()
log("=" * 72)
log(f"  DONE — {len(fi)} درآمد ثابت قطعی + {len(oth)} نیاز به بررسی")
log("=" * 72)

with open(OUT, "w", encoding="utf-8") as fout:
    fout.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
