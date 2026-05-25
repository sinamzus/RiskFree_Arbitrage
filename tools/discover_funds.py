"""discover_funds.py — کشف جامع همه صندوق‌های درآمد ثابت قابل معامله

روش کار:
  ۱. TSETMC CDN: جستجوی سیستماتیک با ترکیب‌های دو-حرفی فارسی
     (همه ETF با cgrValCot=H1 بورس یا 1A فرابورس)
  ۲. TSETMC tsev2: تلاش برای دریافت لیست کامل با پارامترهای مختلف
  ۳. فیپیران: تلاش برای دریافت لیست صندوق‌های درآمد ثابت

  فیلتر نهایی: نام حاوی ثابت | درآمد | صندوق‌های با cgrValCot∈{H1,1A}
  + تأیید با GetClosingPriceInfo (قیمت و حجم)

اجرا:
  python tools/discover_funds.py [--verbose] [--output funds_full.py]
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from config import REQUEST_HEADERS

S = requests.Session()
S.headers.update(REQUEST_HEADERS)
S.headers["Referer"] = "https://www.tsetmc.com/"
S.headers["Origin"]  = "https://www.tsetmc.com"

CDN   = "https://cdn.tsetmc.com/api"
TSETMC = "https://www.tsetmc.com"
FIPIRAN = "https://fund.fipiran.ir"

# ── ETF market codes that contain fixed-income funds ──────────────────────────
# H1 = بازار صندوق‌های قابل معامله بورس
# 1A = بازار ابزارهای نوین مالی فرابورس (OTC ETFs)
FIXED_INCOME_MARKET_CODES = {"H1", "1A"}

# Keywords that identify fixed-income funds (in lVal30 / full name)
FI_KEYWORDS = [
    "درآمد ثابت", "درآمدثابت", "با درآمد",
    "پايدار", "پایدار",
]

# ── helper ────────────────────────────────────────────────────────────────────

def is_fixed_income(name: str) -> bool:
    """آیا صندوق از نوع درآمد ثابت است؟"""
    if any(kw in name for kw in FI_KEYWORDS):
        return True
    # TSE fixed-income ETFs always end with -ثابت
    if name.rstrip().endswith("-ثابت") or name.rstrip().endswith("- ثابت"):
        return True
    # OTC fixed-income ETFs end with -د  AND contain صندوق/ص.س
    if (name.rstrip().endswith("-د") or name.rstrip().endswith("- د")):
        if "صندوق" in name or "ص.س" in name:
            return True
    return False


def get_json(url: str, timeout: int = 15, delay: float = 0.3) -> dict | list | None:
    try:
        r = S.get(url, timeout=timeout)
        time.sleep(delay)
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                return r.json()
        return None
    except Exception as e:
        return None


# ── METHOD 1: Systematic keyword search ──────────────────────────────────────
# TSETMC search returns at most ~50 hits per term; we sweep many short terms.

# All Persian letters (for systematic 2-char sweep)
PERSIAN_CHARS = list("آاابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی")

# Generate all 2-char combinations to catch any fund name (TSETMC returns fast for no-match)
_TWO_CHAR_SWEEP = [a+b for a in PERSIAN_CHARS for b in PERSIAN_CHARS]

# Common 2-char prefixes in fund names (will be deduplicated with sweep)
TWO_CHAR_PREFIXES = [
    "صن", "صا", "ص.", "آف", "آو", "آر", "آس", "آم", "آک", "آی",
    "ام", "اع", "اف", "اک", "او", "اط", "اص", "اس", "اب", "اي",
    "ار", "از", "ان", "اه", "اي",
    "پا", "پی", "پار", "پاي", "پاس",
    "تص", "تد", "تر",
    "ثم", "ثا",
    "خا", "خت",
    "دا", "دي",
    "رای", "رايب",
    "زم",
    "سا", "سپ", "سخ",
    "شم",
    "فر", "فی", "فيروز",
    "قا",
    "کا", "کم", "کی", "کار", "کارم",
    "گن",
    "لب",
    "ما", "مان",
    "نی", "نيك",
    "وی",
    "هم",
    "یا",
    "بل",
]
# Explicit keywords most likely to match
EXPLICIT_TERMS = [
    "درآمد ثابت", "درآمدثابت", "صندوق ثابت", "صندوق با درآمد",
    "صندوق س", "ص.س.درآمد", "پايدار", "اطمينان",
    "صندوق درآمد", "ص.س.ص", "نوع دوم",
    # Known fund name fragments
    "كمند", "كيان", "امين", "افران", "ياقوت", "فيروزا", "هماي",
    "تداوم", "ثمر", "اركيده", "كارين", "سپر", "آسا", "ماهور", "آرامش",
    "رايبد", "بلوط", "پارند", "خاتم", "فردا", "لبخند", "آفاق",
    "گنجين", "صايند", "اوصتا", "سخند", "آسود", "اطمينان", "اونيكس",
    "ترنج", "اكسيژن", "دامون", "آسان", "شميم", "اصيل", "نيك",
    "آوند", "گنجينه", "تصميم", "پاسارگاد", "كارآمد", "كارما",
    "پايش", "كارين", "ابوذر", "كوثر", "زمرد", "الماس",
    "مبين", "سرو", "بهار", "باران", "سيب", "ياس", "بنفشه",
    "نسيم", "بادران", "الوند", "كهكشان", "قطره", "سهيل",
    "شقايق", "اقاقيا", "گل", "نيلوفر", "سوسن", "صبح",
    "فجر", "سحر", "طلوع", "بام", "صنم", "صبا",
    "پندار", "پيمان", "پيوند", "پرتو",
    "نهال", "نور", "نشاط", "ندا",
    "مرجان", "مروارید", "مشعل",
    "كوروش", "كيميا", "كوثر",
    "هستي", "همدان",
    "وفا", "وفادار",
    "رشد", "رفاه", "رضوي",
    "ايمان", "ايران",
    "حكمت", "حافظ",
    "مولانا", "حكيم", "سينا", "بوعلي", "بيهق",
    "آذر", "خوارزم", "البرز", "دماوند", "زاگرس",
    "سهند", "سبلان", "توس",
    "صدف", "لؤلؤ", "زمرد",
]

def search_all_terms(verbose: bool) -> dict[str, dict]:
    """Search TSETMC for all fixed-income ETFs using many terms."""
    found: dict[str, dict] = {}   # insCode → fund info

    all_terms = list(set(
        EXPLICIT_TERMS + TWO_CHAR_PREFIXES + _TWO_CHAR_SWEEP
    ))

    print(f"\n  ── جستجوی TSETMC با {len(all_terms)} عبارت ──")

    for i, term in enumerate(sorted(all_terms)):
        url = f"{CDN}/Instrument/GetInstrumentSearch/{requests.utils.quote(term)}"
        data = get_json(url, delay=0.2)
        if not data:
            continue
        instruments = data if isinstance(data, list) else []
        for inst in instruments:
            code    = inst.get("insCode", "")
            name    = inst.get("lVal30", "")
            sym     = inst.get("lVal18AFC", "")
            mkt     = inst.get("cgrValCot", "")
            flow    = inst.get("flow", 0)

            if code not in found:
                # H1 = TSE ETF market (needs name check — H1 includes equity ETFs too)
                # 1A = OTC fixed-income instrument market (more specific)
                if mkt in FIXED_INCOME_MARKET_CODES and is_fixed_income(name):
                    found[code] = {
                        "symbol": sym.strip(),
                        "name": name.strip(),
                        "ins_code": code,
                        "cgrValCot": mkt,
                        "flow": flow,
                    }
                    if verbose:
                        print(f"    [{len(found):3d}] {sym:15s} {mkt} fl={flow}  {name[:50]}")

        if (i+1) % 20 == 0:
            print(f"    ... {i+1}/{len(all_terms)} عبارت بررسی‌شده، {len(found)} صندوق یافت‌شده")

    return found


# ── METHOD 2: TSETMC tsev2 / market-group endpoints ──────────────────────────

def try_bulk_endpoints() -> dict[str, dict]:
    """Try TSETMC endpoints that might return all ETFs at once."""
    found: dict[str, dict] = {}

    print("\n  ── تلاش برای endpoint های bulk TSETMC ──")

    # Try various bulk/list endpoints
    bulk_urls = [
        # CDN endpoints
        f"{CDN}/ClosingPrice/GetMarketClose/H1/1",
        f"{CDN}/ClosingPrice/GetMarketClose/1A/1",
        f"{CDN}/ClosingPrice/GetMarketClose/H1/0",
        f"{CDN}/ClosingPrice/GetMarketClose/1A/0",
        f"{CDN}/Instrument/GetInstrumentGroupByMarket/H1",
        f"{CDN}/Instrument/GetInstrumentGroupByMarket/1A",
        f"{CDN}/Instrument/GetInstrumentsByBeta/400",   # 400 = ETF insBeta?
        f"{CDN}/Instrument/GetInstrumentsByType/I",
        f"{CDN}/MarketData/GetTotalMarket/H1",
        f"{CDN}/MarketData/GetTotalMarket/1A",
        f"{CDN}/MarketData/GetSectorsPE/H1",
        # tsev2 endpoints (old API)
        f"https://www.tsetmc.com/tsev2/data/instinfofast.aspx?d=i&g=H1&t=1",
        f"https://www.tsetmc.com/tsev2/data/instinfofast.aspx?d=i&g=1A&t=1",
        f"https://www.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0&group=H1",
        f"https://www.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0&group=1A",
        # TSETMC ETF pages
        f"https://www.tsetmc.com/Loader.aspx?ParTree=15131P&i=H1",
        f"https://www.tsetmc.com/Loader.aspx?ParTree=151318&i=H1",
    ]

    for url in bulk_urls:
        try:
            r = S.get(url, timeout=15)
            time.sleep(0.3)
            ct = r.headers.get("content-type", "")
            size = len(r.content)
            if r.status_code == 200 and "json" in ct and size > 100:
                data = r.json()
                print(f"  ✓ {url.split('com')[-1][:60]}  → {size} B  JSON keys={list(data.keys())[:5] if isinstance(data, dict) else type(data).__name__}")
                # Try to extract instruments
                items = []
                if isinstance(data, list): items = data
                elif isinstance(data, dict):
                    for k in ["instruments", "data", "result", "closingPrice", "marketWatch"]:
                        if k in data: items = data[k]; break
                for item in (items if isinstance(items, list) else []):
                    code = (item.get("insCode") or item.get("inscode") or
                            item.get("InstrumentCode") or "")
                    name = (item.get("lVal30") or item.get("name") or "")
                    sym  = (item.get("lVal18AFC") or item.get("symbol") or "")
                    mkt  = (item.get("cgrValCot") or item.get("market") or "")
                    flow = item.get("flow", 0)
                    if code and mkt in FIXED_INCOME_MARKET_CODES:
                        found[code] = {
                            "symbol": sym.strip(), "name": name.strip(),
                            "ins_code": code, "cgrValCot": mkt, "flow": flow,
                        }
            elif r.status_code != 404:
                print(f"  ? {url.split('com')[-1][:60]}  → {r.status_code}  {size} B  {'JSON?' if 'json' in ct else ct[:20]}")
        except Exception as e:
            pass

    return found


# ── METHOD 3: Fipiran fund list ───────────────────────────────────────────────

def try_fipiran() -> list[dict]:
    """Try Fipiran public API for fixed-income fund list."""
    print("\n  ── تلاش برای API فیپیران ──")

    results = []
    # Fipiran uses /api/v1/fund/fundlistissuebyfundtype?fundType=6 (6=fixed-income)
    # But fund.fipiran.ir domain is IP-blocked outside Iran
    urls = [
        f"{FIPIRAN}/api/v1/fund/fundlistissuebyfundtype?fundType=6&pageSize=200",
        f"{FIPIRAN}/api/v1/fund/fundlistissuebyfundtype?fundType=6",
        f"{FIPIRAN}/api/v1/fund/fundlist?typeOfFund=Fixed&pageSize=500",
        "https://fipiran.ir/api/v1/fund/fundlistissuebyfundtype?fundType=6",
        "https://fipiran.com/api/v1/fund/fundlistissuebyfundtype?fundType=6",
    ]

    for url in urls:
        try:
            r = S.get(url, timeout=20)
            time.sleep(0.3)
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" in ct and len(r.content) > 500:
                data = r.json()
                items = data if isinstance(data, list) else (
                    data.get("items") or data.get("fund") or
                    data.get("funds") or data.get("result") or []
                )
                if items:
                    print(f"  ✓ {url}  → {len(items)} صندوق")
                    for f in items:
                        results.append({
                            "name":    f.get("name") or f.get("fundName") or "",
                            "symbol":  f.get("symbol") or f.get("regNo") or "",
                            "navCode": f.get("insCode") or f.get("id") or "",
                        })
                    break
            else:
                st = r.status_code
                print(f"  ✗ {url}  → {st}  {len(r.content)} B")
        except Exception as e:
            print(f"  ✗ {url}  → خطا: {e}")

    return results


# ── METHOD 4: Verify with GetClosingPriceInfo ─────────────────────────────────

def verify_funds(funds: dict[str, dict], verbose: bool) -> list[dict]:
    """Verify each fund by fetching live price data."""
    print(f"\n  ── تأیید {len(funds)} صندوق با GetClosingPriceInfo ──")
    verified = []

    for i, (code, fd) in enumerate(funds.items()):
        url = f"{CDN}/ClosingPrice/GetClosingPriceInfo/{code}"
        data = get_json(url, delay=0.15)
        if not data:
            continue

        cp = None
        if isinstance(data, dict):
            cp = data.get("closingPriceInfo") or data.get("closingPrice")
        if not cp:
            continue

        price  = cp.get("pClosing") or cp.get("pDrCotVal") or 0
        volume = cp.get("qTotTran5J") or 0
        name   = cp.get("lVal30") or fd["name"]
        sym    = cp.get("lVal18AFC") or fd["symbol"]

        if price > 0:
            fd_copy = dict(fd)
            fd_copy["market_price"] = price
            fd_copy["volume"]       = volume
            fd_copy["name"]         = name
            fd_copy["symbol"]       = sym.strip()
            verified.append(fd_copy)
            if verbose:
                print(f"  [{i+1:3d}] {sym:15s} قیمت={price:,.0f}  حجم={volume:,.0f}  {name[:40]}")

        if (i+1) % 20 == 0:
            print(f"    ... {i+1}/{len(funds)} تأیید شدند: {len(verified)}")

    return verified


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def print_config_block(funds: list[dict]):
    """Print the config.py FIXED_INCOME_ETFS block."""
    print("\n" + "═"*72)
    print("  خروجی آماده برای config.py")
    print("═"*72)
    print("\nFIXED_INCOME_ETFS = [")

    tse_funds = [f for f in funds if f.get("flow") == 1 or f.get("cgrValCot") == "H1"]
    otc_funds = [f for f in funds if f.get("flow") == 2 or f.get("cgrValCot") == "1A"]

    # Sort by volume descending
    tse_funds.sort(key=lambda f: -(f.get("volume") or 0))
    otc_funds.sort(key=lambda f: -(f.get("volume") or 0))

    print("    # ══ flow=1  بورس اوراق بهادار تهران ══")
    for f in tse_funds:
        sym  = f["symbol"].replace('"', '\\"')
        name = f["name"].replace('"', '\\"')
        code = f["ins_code"]
        print(f'    {{"symbol": "{sym}", "name": "{name}",')
        print(f'     "ins_code": "{code}"}},')

    print("    # ══ flow=2  فرابورس ══")
    for f in otc_funds:
        sym  = f["symbol"].replace('"', '\\"')
        name = f["name"].replace('"', '\\"')
        code = f["ins_code"]
        print(f'    {{"symbol": "{sym}", "name": "{name}",')
        print(f'     "ins_code": "{code}"}},')

    print("]")


def save_report(funds: list[dict], all_found: dict[str, dict], path: Path):
    """Save full discovery report."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("━"*72 + "\n")
        f.write("  گزارش کشف صندوق‌های درآمد ثابت — جستجوی جامع\n")
        f.write("━"*72 + "\n\n")

        f.write(f"  یافت‌شده (raw): {len(all_found)}\n")
        f.write(f"  تأیید‌شده با قیمت: {len(funds)}\n\n")

        f.write("  نماد           قیمت          حجم        fl  mkt  نام\n")
        f.write("  " + "─"*80 + "\n")

        for fd in sorted(funds, key=lambda x: -(x.get("volume") or 0)):
            sym   = fd.get("symbol", "")
            name  = fd.get("name", "")
            price = fd.get("market_price", 0)
            vol   = fd.get("volume", 0)
            flow  = fd.get("flow", 0)
            mkt   = fd.get("cgrValCot", "")
            f.write(f"  {sym:15s} {price:12,.0f} {vol:15,.0f}  {flow}  {mkt:3s}  {name}\n")

        f.write("\n\n" + "═"*72 + "\n")
        f.write("  خروجی config.py\n")
        f.write("═"*72 + "\n\nFIXED_INCOME_ETFS = [\n")

        for fd in sorted(funds, key=lambda x: -(x.get("volume") or 0)):
            sym  = fd["symbol"].replace('"', '\\"')
            name = fd["name"].replace('"', '\\"')
            code = fd["ins_code"]
            flow = fd.get("flow", 0)
            mkt  = fd.get("cgrValCot", "")
            f.write(f'    # fl={flow} {mkt}\n')
            f.write(f'    {{"symbol": "{sym}", "name": "{name}", "ins_code": "{code}"}},\n')

        f.write("]\n")

    print(f"\n  ✓ گزارش ذخیره شد: {path}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="کشف جامع صندوق‌های درآمد ثابت قابل معامله")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--output",  default="tools/discover_funds_report.txt",
                    help="فایل خروجی گزارش (پیش‌فرض: tools/discover_funds_report.txt)")
    ap.add_argument("--skip-verify", action="store_true",
                    help="تأیید با GetClosingPriceInfo را رد کن (سریع‌تر)")
    args = ap.parse_args()

    print()
    print("━"*72)
    print("  کشف جامع صندوق‌های درآمد ثابت قابل معامله")
    print("  فیلتر: cgrValCot ∈ {H1, 1A} + نام حاوی کلیدواژه‌های درآمد ثابت")
    print("━"*72)

    # Warm session
    try:
        r = S.get("https://www.tsetmc.com/", timeout=10)
        print(f"\n  ✓ اتصال به TSETMC: {r.status_code}")
    except Exception as e:
        print(f"\n  ✗ خطا در اتصال: {e}")
        return

    # --- Collect from all methods ---
    all_found: dict[str, dict] = {}

    # Method 1: systematic keyword search
    found1 = search_all_terms(args.verbose)
    all_found.update(found1)
    print(f"  → جستجوی keyword: {len(found1)} صندوق")

    # Method 2: bulk endpoints
    found2 = try_bulk_endpoints()
    all_found.update(found2)
    if found2:
        print(f"  → bulk endpoint: {len(found2)} صندوق")

    # Method 3: Fipiran (optional, might not work outside Iran)
    fipiran_funds = try_fipiran()
    if fipiran_funds:
        print(f"  → فیپیران: {len(fipiran_funds)} صندوق")

    print(f"\n  مجموع یافت‌شده (raw، بدون تأیید): {len(all_found)}")

    # --- Verify with live prices ---
    if args.skip_verify:
        verified = list(all_found.values())
    else:
        verified = verify_funds(all_found, args.verbose)

    # --- Sort by volume ---
    verified.sort(key=lambda f: -(f.get("volume") or 0))

    # --- Print summary ---
    print()
    print("━"*72)
    print(f"  ✓ صندوق‌های تأیید‌شده: {len(verified)}")
    print()
    print(f"  {'نماد':15s} {'قیمت':>12s} {'حجم':>18s}  fl  نام")
    print("  " + "─"*75)
    for fd in verified:
        print(
            f"  {fd.get('symbol',''):15s}"
            f" {fd.get('market_price',0):12,.0f}"
            f" {fd.get('volume',0):18,.0f}"
            f"  {fd.get('flow',0)}"
            f"  {fd.get('name','')[:50]}"
        )

    # --- Print config block ---
    print_config_block(verified)

    # --- Save report ---
    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    save_report(verified, all_found, out)


if __name__ == "__main__":
    main()
