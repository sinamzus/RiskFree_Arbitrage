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

# کمند — flow=1 بورس ETF
log("\n── «كمند» (ETF بورس، flow=1) ──")
for item in search("كمند"):
    log(json.dumps(item, ensure_ascii=False, indent=2))

# سام — flow=2 فرابورس ETF
log("\n── «سام درآمد» (ETF فرابورس، flow=2) ──")
for item in search("سام درآمد"):
    log(json.dumps(item, ensure_ascii=False, indent=2))

# اعتماد آفرین — flow=2
log("\n── «اعتماد آفرين» (ETF فرابورس، flow=2) ──")
for item in search("اعتماد آفرين"):
    log(json.dumps(item, ensure_ascii=False, indent=2))

# لبخند — flow=2
log("\n── «لبخند» ──")
for item in search("لبخند"):
    log(json.dumps(item, ensure_ascii=False, indent=2))

# وبملت — سهام بورس
log("\n── «وبملت» (سهام بانک) ──")
for item in search("وبملت")[:1]:
    log(json.dumps(item, ensure_ascii=False, indent=2))

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\n  گزارش در {OUT} ذخیره شد")
