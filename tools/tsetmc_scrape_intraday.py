"""tsetmc_scrape_intraday.py
دریافت داده معاملات ثانیه‌به‌ثانیه درون‌روزی از صفحه سوابق TSETMC

این اسکریپت را از ماشین خودتان در ایران اجرا کنید.
ابتدا endpoint های مختلف را تست می‌کند، آنچه کار کرد را در DB ذخیره می‌کند.

اجرا:
    python tools/tsetmc_scrape_intraday.py
    python tools/tsetmc_scrape_intraday.py --date 20260524
    python tools/tsetmc_scrape_intraday.py --date 20260524 --symbol کمند
    python tools/tsetmc_scrape_intraday.py --days 5
"""

from __future__ import annotations
import argparse, sys, json, time, re, csv
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from config import FIXED_INCOME_ETFS
from database import Database

DB = Database()

# ─── Session ────────────────────────────────────────────────────────────────
S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         "https://www.tsetmc.com/",
    "Origin":          "https://www.tsetmc.com",
})


# ─── Helpers ─────────────────────────────────────────────────────────────────

def log(msg=""):
    print(msg, flush=True)

def get(url: str, timeout=20) -> requests.Response | None:
    try:
        r = S.get(url, timeout=timeout)
        return r
    except Exception as e:
        log(f"    ⚠ خطای اتصال: {e}")
        return None

def trading_dates(n: int) -> list[int]:
    TSE_WEEKEND = {3, 4}
    dates, d = [], datetime.now()
    while len(dates) < n:
        if d.weekday() not in TSE_WEEKEND:
            dates.append(int(d.strftime("%Y%m%d")))
        d -= timedelta(days=1)
    return dates

def hhmm(t: int) -> str:
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


# ─── Endpoint probes (run once at startup) ───────────────────────────────────

PROBE_INS  = "34718633636164421"   # کمند — always in list

def _last_trading_day() -> int:
    """Return yesterday (or last Thursday if today is Saturday/Sunday)."""
    TSE_WEEKEND = {3, 4}
    d = datetime.now() - timedelta(days=1)
    while d.weekday() in TSE_WEEKEND:
        d -= timedelta(days=1)
    return int(d.strftime("%Y%m%d"))

def warm_session():
    """Visit TSETMC main page to get session cookies."""
    log("  🌐 اتصال به www.tsetmc.com ...")
    r = get("https://www.tsetmc.com/", timeout=15)
    if r and r.status_code == 200:
        log(f"  ✓ صفحه اصلی: 200  کوکی‌ها: {dict(S.cookies)}")
    elif r:
        log(f"  ⚠ صفحه اصلی: {r.status_code}")
    else:
        log("  ✗ اتصال به tsetmc.com ناموفق")
    time.sleep(0.5)

    # Also visit the instrument page to mimic real browser behaviour
    r2 = get(f"https://www.tsetmc.com/instrument/{PROBE_INS}")
    if r2:
        log(f"  ✓ صفحه نماد: {r2.status_code}  (کوکی‌های جدید: {dict(S.cookies)})")
    time.sleep(0.5)

WORKING_STRATEGY: str | None = None   # filled by probe_endpoints()

def probe_endpoints(date_int: int) -> str | None:
    """Try all known endpoint patterns. Return name of first that works.

    Uses *yesterday* for the probe (today has no trades if market is closed).
    """
    global WORKING_STRATEGY
    if WORKING_STRATEGY:
        return WORKING_STRATEGY

    ins = PROBE_INS
    # Always probe with the last trading day (not today which may be empty)
    probe_date = _last_trading_day()
    d   = str(probe_date)
    log(f"  (probe تاریخ: {probe_date})")

    candidates = [
        # ── CDN JSON endpoints ─────────────────────────────────────────────
        ("cdn_false",
         f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{ins}/{d}/false",
         "json", "tradeHistory"),
        ("cdn_true",
         f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{ins}/{d}/true",
         "json", "tradeHistory"),
        # ── Old TSETMC tsev2 (semicolon-separated) ─────────────────────────
        ("tsev2_top",
         f"https://www.tsetmc.com/tsev2/data/TradeHistory.aspx?i={ins}&top=999999&A=0",
         "csv", None),
        ("tsev2_date",
         f"https://www.tsetmc.com/tsev2/data/TradeHistory.aspx?i={ins}&dEven={d}&top=999999&A=0",
         "csv", None),
        # ── Live trades (current session only) ────────────────────────────
        ("cdn_live",
         f"https://cdn.tsetmc.com/api/Trade/GetTrade/{ins}",
         "json", "trade"),
        # ── Closing price as sanity check ─────────────────────────────────
        ("cdn_closing",
         f"https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceInfo/{ins}",
         "json", "closingPriceInfo"),
    ]

    log()
    log("  ─── تست endpoint ها ───────────────────────────────────────────────")
    for name, url, fmt, key in candidates:
        r = get(url)
        if not r:
            log(f"  ✗ {name:20s} → اتصال ناموفق")
            continue
        log(f"  {'✓' if r.status_code==200 else '✗'} {name:20s} → {r.status_code}  ({len(r.content):,} بایت)", )
        if r.status_code == 200:
            if fmt == "json":
                try:
                    data = r.json()
                    if key and key in data:
                        items = data[key]
                        log(f"      JSON key='{key}'  count={len(items)}")
                        if items:
                            log(f"      sample: {json.dumps(items[0], ensure_ascii=False)[:120]}")
                            WORKING_STRATEGY = name
                            return name
                    else:
                        log(f"      JSON keys: {list(data.keys())[:8]}")
                        if name == "cdn_closing":
                            WORKING_STRATEGY = name   # CDN reachable even if no trades
                            return name
                except Exception as e:
                    log(f"      ⚠ JSON parse error: {e}  body={r.text[:80]}")
            elif fmt == "csv":
                # tsev2 returns semicolon-separated lines
                lines = r.text.strip().split("@")
                log(f"      CSV lines={len(lines)}  sample={lines[0][:80]}")
                if len(lines) > 1:
                    WORKING_STRATEGY = name
                    return name
        time.sleep(0.3)

    log("  ─────────────────────────────────────────────────────────────────────")
    return None


# ─── Fetch ticks using the working strategy ──────────────────────────────────

def fetch_ticks_cdn_json(ins_code: str, date_int: int, variant: str) -> list[dict]:
    """Fetch via CDN JSON API (cdn_false or cdn_true)."""
    suffix = "false" if variant == "cdn_false" else "true"
    url = f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{ins_code}/{date_int}/{suffix}"
    r = get(url)
    if not r or r.status_code != 200:
        return []
    raw = r.json().get("tradeHistory") or []
    result = []
    for t in raw:
        result.append({
            "seq":      int(t.get("nTran", 0)),
            "time":     int(t.get("hEven", 0)),
            "price":    float(t.get("pTran", 0)),
            "volume":   int(t.get("qTitTran", 0)),
            "canceled": 1 if t.get("canceled") else 0,
        })
    result.sort(key=lambda x: x["seq"])
    return result

def fetch_ticks_tsev2(ins_code: str, date_int: int, variant: str) -> list[dict]:
    """Fetch via old tsev2 CSV endpoint.

    Format: each record separated by '@', fields by ';'
    Typical fields: time;price;volume;...
    """
    if variant == "tsev2_top":
        url = f"https://www.tsetmc.com/tsev2/data/TradeHistory.aspx?i={ins_code}&top=999999&A=0"
    else:
        url = f"https://www.tsetmc.com/tsev2/data/TradeHistory.aspx?i={ins_code}&dEven={date_int}&top=999999&A=0"
    r = get(url)
    if not r or r.status_code != 200:
        return []

    result = []
    seq = 1
    for line in r.text.strip().split("@"):
        parts = line.split(";")
        if len(parts) < 3:
            continue
        try:
            # Typical tsev2 format: time(HHMM or HHMMSS), price, volume, ...
            t_raw = parts[0].strip()
            # Normalise: pad to 6 digits
            if len(t_raw) <= 4:
                t_raw = t_raw.zfill(4) + "00"
            else:
                t_raw = t_raw.zfill(6)
            result.append({
                "seq":      seq,
                "time":     int(t_raw),
                "price":    float(str(parts[1]).replace(",", "")),
                "volume":   int(str(parts[2]).replace(",", "")),
                "canceled": 0,
            })
            seq += 1
        except Exception:
            continue
    return result

def fetch_ticks(ins_code: str, date_int: int, strategy: str) -> list[dict]:
    """Dispatch to the correct fetcher."""
    if strategy in ("cdn_false", "cdn_true", "cdn_closing"):
        # Fall back to both variants
        for v in ("cdn_false", "cdn_true"):
            ticks = fetch_ticks_cdn_json(ins_code, date_int, v)
            if ticks:
                return ticks
        return []
    elif strategy in ("tsev2_top", "tsev2_date"):
        return fetch_ticks_tsev2(ins_code, date_int, strategy)
    return []


# ─── Save to CSV (for manual inspection) ─────────────────────────────────────

def save_csv(symbol: str, date_int: int, ticks: list[dict]):
    out = ROOT / "tools" / f"intraday_{symbol}_{date_int}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["seq","time","price","volume","canceled"])
        w.writeheader()
        w.writerows(ticks)
    return out


# ─── Main fetch loop ──────────────────────────────────────────────────────────

def run(dates: list[int], symbol_filter: str | None, force: bool):
    log()
    log("━" * 72)
    log("  اسکریپر درون‌روزی TSETMC — معاملات ثانیه‌به‌ثانیه")
    log("━" * 72)

    warm_session()

    strategy = probe_endpoints(dates[0])
    if strategy is None:
        log()
        log("  ✗ هیچ endpoint ای کار نکرد.")
        log("  ← مطمئن شوید VPN خاموش است و از داخل ایران اجرا می‌کنید.")
        return

    log()
    log(f"  ✓ استراتژی انتخاب‌شده: {strategy}")
    log()
    log("━" * 72)

    grand_new = 0

    for fund in FIXED_INCOME_ETFS:
        sym      = fund["symbol"]
        ins_code = fund.get("ins_code", "").strip()

        if symbol_filter and sym != symbol_filter:
            continue
        if not ins_code:
            log(f"  {sym:12s}  ← بدون ins_code، رد شد")
            continue

        have_dates = set() if force else set(DB.get_intraday_dates(sym))

        for date_int in dates:
            existing_count = len(DB.get_intraday_trades(sym, date_int)) if not force else 0
            today_int = int(datetime.now().strftime("%Y%m%d"))

            if date_int in have_dates and existing_count >= 10 and date_int != today_int:
                log(f"  {sym:12s}  {date_int}  ← {existing_count:,} تیک قبلی، رد شد")
                continue

            ticks = fetch_ticks(ins_code, date_int, strategy)
            time.sleep(0.3)

            if not ticks:
                log(f"  {sym:12s}  {date_int}  ← بدون داده (بازار بسته یا خطا)")
                continue

            valid = [t for t in ticks if not t["canceled"] and t["price"] > 0]
            prices = [t["price"] for t in valid]
            p_min  = min(prices) if prices else 0
            p_max  = max(prices) if prices else 0
            t0     = hhmm(valid[0]["time"])  if valid else "—"
            t1     = hhmm(valid[-1]["time"]) if valid else "—"

            new_rows = DB.save_intraday_trades(sym, ins_code, date_int, valid)
            grand_new += new_rows

            csv_path = save_csv(sym, date_int, valid)
            log(
                f"  {sym:12s}  {date_int}  "
                f"{len(valid):>6,} تیک  "
                f"قیمت {p_min:>10,.0f}–{p_max:>10,.0f}  "
                f"{t0}–{t1}  "
                f"({new_rows} جدید ذخیره)  → {csv_path.name}"
            )

    log()
    log("━" * 72)
    log(f"  ✓ مجموع تیک جدید: {grand_new:,}")
    log()

    # Status summary
    log("  وضعیت DB پس از دریافت:")
    total = 0
    for fund in FIXED_INCOME_ETFS:
        sym = fund["symbol"]
        if symbol_filter and sym != symbol_filter:
            continue
        dates_have = DB.get_intraday_dates(sym)
        if dates_have:
            last  = dates_have[-1]
            count = len(DB.get_intraday_trades(sym, last))
            total += count
            log(f"    {sym:12s}  آخرین روز: {last}  تیک: {count:,}")
    log(f"  مجموع تیک در DB: {total:,}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="اسکریپر داده درون‌روزی TSETMC — از ایران اجرا شود"
    )
    ap.add_argument("--date",   help="تاریخ YYYYMMDD (پیش‌فرض: امروز)")
    ap.add_argument("--days",   type=int, default=1,
                    help="چند روز (پیش‌فرض: ۱)")
    ap.add_argument("--symbol", help="فقط یک نماد")
    ap.add_argument("--force",  action="store_true",
                    help="دوباره دریافت حتی اگر در DB باشد")
    args = ap.parse_args()

    if args.date:
        try:
            dt    = datetime.strptime(args.date, "%Y%m%d")
            start = dt
        except ValueError:
            print("فرمت تاریخ: YYYYMMDD مثلاً 20260524")
            sys.exit(1)
        dates = []
        TSE_WEEKEND = {3, 4}
        d = dt
        while len(dates) < args.days:
            if d.weekday() not in TSE_WEEKEND:
                dates.append(int(d.strftime("%Y%m%d")))
            d -= timedelta(days=1)
    else:
        dates = trading_dates(args.days)

    run(dates, args.symbol, args.force)

if __name__ == "__main__":
    main()
