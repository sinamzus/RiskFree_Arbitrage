"""debug_search_fields.py — dump کامل فیلدهای GetInstrumentSearch

اجرا:
  python tools/debug_search_fields.py
"""
import sys, json, requests
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CDN = "https://cdn.tsetmc.com/api"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
     "Referer": "https://www.tsetmc.com/"}
OUT = "tools/debug_search_fields_report.txt"
lines = []

def log(s=""):
    print(s)
    lines.append(str(s))

def search(kw):
    from urllib.parse import quote
    r = requests.get(f"{CDN}/Instrument/GetInstrumentSearch/{quote(kw)}", headers=H, timeout=15)
    return r.json().get("instrumentSearch", [])

log("=" * 60)
log("  فیلدهای GetInstrumentSearch — dump کامل")
log("=" * 60)

# کمند (صندوق ETF درآمد ثابت)
log("\n── جستجو «كمند» (صندوق ETF درآمد ثابت) ──")
for item in search("كمند"):
    log(json.dumps(item, ensure_ascii=False, indent=2))

# وبملت (سهام بانک)
log("\n── جستجو «وبملت» (سهام بانک) ──")
for item in search("وبملت")[:2]:
    log(json.dumps(item, ensure_ascii=False, indent=2))

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
