"""tsetmc_all_fixed_income.py — استخراج کامل صندوق‌های درآمد ثابت ETF از TSETMC

استراتژی:
  A. GetMarketWatch — دانلود کل بازار صندوق‌ها یکجا (بهترین روش)
  B. GetSectorPaperList — لیست نمادهای گروه صنعت صندوق
  C. جستجوی کلمه‌کلیدی گسترده (پشتیبان)
  D. تأیید نهایی با GetClosingPriceInfo

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
    kw.setdefault("timeout", 30)
    try:
        return S.get(url, **kw)
    except Exception as e:
        return None

def extract_list(data):
    """از هر ساختار JSON، لیست ابزارها را برگردان.
    TSETMC marketwatch می‌تواند list یا dict-of-dicts باشد.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ["marketwatch", "MarketWatch", "data", "Data", "items",
                  "instrumentInfo", "closingPriceInfo", "closingPrice"]:
            val = data.get(k)
            if isinstance(val, list) and val:
                return val
            # dict-of-dicts: {"marketwatch": {"insCode1": {...}, ...}}
            if isinstance(val, dict) and val:
                return list(val.values())
    return []

# ══════════════════════════════════════════════════════════════════════════
#  بخش A — GetMarketWatch (مهم‌ترین بخش)
# ══════════════════════════════════════════════════════════════════════════
log("=" * 72)
log("  A. GetMarketWatch — دانلود کل بازار")
log("=" * 72)

mw_candidates = [
    # ── ClosingPrice/GetMarketWatch (تأیید شده از گزارش قبلی) ──
    f"{CDN}/ClosingPrice/GetMarketWatch?market=6",          # صندوق ETF
    f"{CDN}/ClosingPrice/GetMarketWatch?market=6&paperType=6",
    f"{CDN}/ClosingPrice/GetMarketWatch?market=6&mop=0&sop=0",
    f"{CDN}/ClosingPrice/GetMarketWatch?market=1&paperType=6",
    f"{CDN}/ClosingPrice/GetMarketWatch?market=2&paperType=6",
    f"{CDN}/ClosingPrice/GetMarketWatch?market=1&mop=0&sop=0",
    f"{CDN}/ClosingPrice/GetMarketWatch?market=2&mop=0&sop=0",
    # market=4 = SME / بازار پایه
    f"{CDN}/ClosingPrice/GetMarketWatch?market=4&paperType=6",
    # ── MarketData (۴۰۴ روی این سرور ولی شاید فرمت درست باشد) ──
    f"{CDN}/MarketData/GetMarketWatch?market=6&EPS=false&mop=0&sop=0&sector=0&col=0",
    f"{CDN}/MarketData/GetMarketWatch?market=6&EPS=false",
    f"{CDN}/MarketData/GetMarketWatch?market=1&EPS=false&mop=0&sop=0&sector=0&col=0",
    f"{CDN}/MarketData/GetMarketWatch?market=2&EPS=false&mop=0&sop=0&sector=0&col=0",
    # ── EndPoints دیگر برای لیست ابزارها ──
    f"{CDN}/Instrument/GetInstrumentList?market=6",
    f"{CDN}/Instrument/GetInstrumentList?market=1&paperType=6",
    f"{CDN}/Instrument/GetInstrumentList?market=2&paperType=6",
    f"{CDN}/ClosingPrice/GetClosingPriceDailyList/0/365",   # همه (احتمال پایین)
]

mw_pool = {}  # insCode → info

for url in mw_candidates:
    r = get(url)
    if not r or r.status_code != 200:
        log(f"  ✗ {getattr(r,'status_code','ERR')}  ...{url[-60:]}")
        continue
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        log(f"  ✗ not-json ({len(r.content)}B)  ...{url[-60:]}")
        continue
    try:
        data = r.json()
    except:
        log(f"  ✗ parse-fail  ...{url[-60:]}")
        continue

    items = extract_list(data)
    if not items:
        log(f"  ? empty/dict  keys={list(data.keys())[:6] if isinstance(data, dict) else '?'}  ...{url[-60:]}")
        continue

    log(f"  ✓ {len(items)} items  ...{url[-60:]}")
    if isinstance(items[0], dict):
        log(f"    keys: {list(items[0].keys())[:12]}")

    new = 0
    for inst in items:
        if not isinstance(inst, dict):
            continue
        code = (inst.get("insCode") or inst.get("instrumentID") or
                inst.get("InstrumentID") or inst.get("isin") or "")
        sym  = (inst.get("lVal18AFC") or inst.get("symbol") or "")
        name = (inst.get("lSoc30") or inst.get("name") or "")
        flow = inst.get("flow", 0)
        if code and code not in mw_pool:
            mw_pool[code] = {"symbol": sym, "name": name, "flow": flow, "src": "mw"}
            new += 1
    log(f"    → {new} جدید، جمع: {len(mw_pool)}")
    time.sleep(0.5)

log(f"\n  جمع MarketWatch: {len(mw_pool)} نماد")


# ══════════════════════════════════════════════════════════════════════════
#  بخش B — GetSectorPaperList
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  B. GetSectorPaperList + GetIndustrySectorList")
log("=" * 72)

sector_pool = {}

# ابتدا لیست همه گروه‌های صنعتی را بگیر
sector_list_urls = [
    f"{CDN}/Sector/GetIndustrySectorList",
    f"{CDN}/Sector/GetSectorList",
    f"{CDN}/Sector/GetAllSectorList",
    f"{CDN}/StaticData/LoadStaticData",
    f"{CDN}/Instrument/GetPaperType",
]

fund_sector_codes = set()

for url in sector_list_urls:
    r = get(url)
    if not r or r.status_code != 200:
        continue
    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        continue
    try:
        data = r.json()
        items = extract_list(data) or (list(data.values())[0] if isinstance(data, dict) else [])
        if items:
            log(f"  ✓ {url}")
            log(f"    {len(items)} items  keys={list(items[0].keys())[:8] if isinstance(items[0], dict) else '?'}")
            # دنبال کد گروه «صندوق» بگرد
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("lSecVal", item.get("name", item.get("sector", ""))))
                code = str(item.get("cSecVal", item.get("code", item.get("id", ""))))
                if "صندوق" in name or "fund" in name.lower():
                    log(f"    → صندوق: code={code}  name={name}")
                    fund_sector_codes.add(code)
        else:
            log(f"  ? {url}  keys={list(data.keys())[:6] if isinstance(data, dict) else '?'}")
    except Exception as e:
        log(f"  ✗ {url}  {e}")
    time.sleep(0.4)

# تست کدهای صنعت صندوق شناخته‌شده
known_sector_codes = list(set(["68691", "57486", "69180", "68633", "69074",
                               "68949", "57010", "68948", "34"]) | fund_sector_codes)
for sc in known_sector_codes:
    for market in ["1", "2", "0"]:
        url = f"{CDN}/Sector/GetSectorPaperList/{sc}/{market}"
        r = get(url)
        if not r or r.status_code != 200:
            continue
        try:
            data = r.json()
            items = extract_list(data)
            if not items:
                continue
            log(f"  ✓ sector={sc} market={market}: {len(items)} items")
            new = 0
            for inst in items:
                if not isinstance(inst, dict):
                    continue
                code = inst.get("insCode", "")
                sym  = inst.get("lVal18AFC", "")
                name = inst.get("lSoc30", "")
                flow = inst.get("flow", market)
                if code and code not in sector_pool:
                    sector_pool[code] = {"symbol": sym, "name": name,
                                         "flow": flow, "src": f"sec{sc}"}
                    new += 1
            log(f"    → {new} جدید")
        except:
            pass
        time.sleep(0.3)

log(f"\n  جمع GetSectorPaperList: {len(sector_pool)} نماد")


# ══════════════════════════════════════════════════════════════════════════
#  بخش C — جستجوی کلمه‌کلیدی جامع
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  C. جستجوی کلمه‌کلیدی جامع")
log("=" * 72)

# لیست گسترده‌ای از کلمات کلیدی مرتبط با صندوق‌های درآمد ثابت
keywords = [
    # اسامی مستقیم
    "درآمد ثابت", "درآمد", "ثابت", "پایدار", "اطمینان", "آرامش",
    "امنیت", "اعتماد", "آسود", "آسان", "آسایش", "آمن",
    # اسامی خاص صندوق‌های شناخته‌شده
    "کمند", "کیان", "ماني", "پارند", "افران", "لبخند", "آفاق",
    "گنجین", "گنجینه", "خاتم", "فردا", "یاقوت", "فیروزا", "همای",
    "صایند", "امین", "تصمیم", "کارآمد", "کارما", "سام", "پاسارگاد",
    "پایش", "آسود", "اطمینان", "اونیکس", "ترنج", "اکسیژن", "دامون",
    # اسامی احتمالی
    "نقدینه", "نقد", "سپرده", "اوراق", "مانی", "آرمان", "نهال",
    "ثروت", "توسعه", "آتیه", "سرمایه", "مشترک", "بهادار", "صنعت",
    "آوند", "آذین", "آریا", "بهمن", "ارمغان", "بیدار", "جوان",
    "دانا", "رشد", "زرین", "سبز", "شادان", "عقیق", "فلاح",
    "قرآن", "کوثر", "گلدان", "لوتوس", "مروارید", "ناب", "ولایت",
    "هستی", "یاس", "آبان", "اردیبهشت", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
    # کلمات کوتاه — ممکن است نماد چند حرفی باشد
    "کاریس", "نوین", "ملت", "صبا", "حامی", "دماوند", "البرز",
    "کاسپین", "خلیج", "زاگرس", "الماس", "عقاب", "شاهین", "پرند",
    "نیلوفر", "سنبله", "مروا", "تابان", "روشن", "بانک", "بیمه",
    "حکیم", "دارا", "ملل", "ایران", "پارسه", "پارس", "پارسیان",
    "مفید", "تدبیر", "کاردان", "فارابی", "رشید", "مبین", "تمدن",
    "نگین", "ذوب", "فولاد", "پتروشیمی", "کاوه", "سیمان", "فجر",
]

kw_pool = {}

for kw in keywords:
    url = f"{CDN}/Instrument/GetInstrumentSearch/{quote(kw)}"
    r = get(url)
    if not r or r.status_code != 200:
        continue
    try:
        results = r.json().get("instrumentSearch", [])
        new = 0
        for inst in results:
            code = inst.get("insCode", "")
            sym  = inst.get("lVal18AFC", "")
            name = inst.get("lSoc30", "")
            flow = inst.get("flow", 0)
            # صافی: حذف اوراق اختیار، حق تقدم، و موارد غیرصندوق
            if not code:
                continue
            if any(sym.startswith(p) for p in ("ض", "ط")):
                continue
            if code not in kw_pool:
                kw_pool[code] = {"symbol": sym, "name": name,
                                 "flow": flow, "src": f"kw:{kw}"}
                new += 1
        if new:
            log(f"  «{kw}»: {len(results)} نتیجه  {new} جدید")
    except:
        pass
    time.sleep(0.25)

log(f"\n  جمع جستجوی کلمه‌کلیدی: {len(kw_pool)} نماد")


# ══════════════════════════════════════════════════════════════════════════
#  بخش D — ترکیب و تأیید نهایی
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  D. ترکیب همه یافته‌ها + تأیید با قیمت زنده")
log("=" * 72)

# ترکیب
candidates = {}
for pool in [mw_pool, sector_pool, kw_pool]:
    for code, info in pool.items():
        if code not in candidates:
            candidates[code] = info

log(f"  کل کاندیدا قبل از تأیید: {len(candidates)}")

# نمادهایی که مشخصاً صندوق درآمد ثابت نیستند
NON_FI_SYMBOLS = {
    "گوهر", "آساس", "آگاس", "سپهر", "حکمت", "اهرم", "توان",
    "کاریس", "طلا", "فیروزه", "مس", "آهن", "پالایش",
}

# تأیید با GetClosingPriceInfo
log()
log("  تأیید صندوق‌های قابل معامله...")

verified = []
for code, info in candidates.items():
    sym = info.get("symbol", "")

    # صافی سریع
    if any(sym.startswith(p) for p in ("ض", "ط", "ح")):
        continue
    if sym in NON_FI_SYMBOLS:
        continue
    # اعداد در نام → احتمالاً اوراق بدهی (اجاره، مشارکت)
    if any(c.isdigit() for c in sym) and len(sym) > 4:
        continue

    url = f"{CDN}/ClosingPrice/GetClosingPriceInfo/{code}"
    r = get(url)
    if not r or r.status_code != 200:
        continue
    try:
        cp    = r.json().get("closingPriceInfo", {})
        price = cp.get("pClosing", 0)
        vol   = cp.get("qTotTran5J", 0)
        name  = cp.get("lSoc30", cp.get("lVal18AFC", info.get("name", "")))
        sym2  = cp.get("lVal18AFC", sym)
        flow  = cp.get("flow", info.get("flow", 0))
        if price <= 0:
            continue
        verified.append({
            "symbol":   sym2 or sym,
            "name":     name,
            "ins_code": code,
            "price":    price,
            "volume":   vol,
            "flow":     flow,
            "src":      info.get("src", "?"),
        })
    except:
        pass
    time.sleep(0.2)

# مرتب‌سازی بر اساس حجم
verified.sort(key=lambda x: x["volume"], reverse=True)

log(f"\n  ✓ صندوق‌های تأیید شده با قیمت مثبت: {len(verified)}")
log()
log(f"  {'نماد':15s}  {'قیمت':>10s}  {'حجم':>14s}  {'flow':>4s}  ins_code")
log("  " + "─" * 78)
for f in verified:
    log(f"  {f['symbol']:15s}  {f['price']:>10,.0f}  {f['volume']:>14,}  "
        f"{f['flow']:>4}  {f['ins_code']}")


# ══════════════════════════════════════════════════════════════════════════
#  بخش E — خروجی config.py
# ══════════════════════════════════════════════════════════════════════════
log()
log("=" * 72)
log("  E. خروجی آماده برای config.py")
log("=" * 72)
log()
log("FIXED_INCOME_ETFS = [")
for f in verified:
    log(f'    {{"symbol": "{f["symbol"]}", "name": "{f["name"]}", '
        f'"ins_code": "{f["ins_code"]}"}},')
log("]")

log()
log("=" * 72)
log(f"  DONE — {len(verified)} صندوق یافت شد")
log("=" * 72)

with open(OUT, "w", encoding="utf-8") as fout:
    fout.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
