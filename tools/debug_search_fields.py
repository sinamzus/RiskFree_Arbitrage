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

def search(kw):
    from urllib.parse import quote
    r = requests.get(f"{CDN}/Instrument/GetInstrumentSearch/{quote(kw)}", headers=H, timeout=15)
    return r.json().get("instrumentSearch", [])

print("=" * 60)
print("  فیلدهای GetInstrumentSearch — dump کامل")
print("=" * 60)

# کمند (صندوق)
print("\n── جستجو «كمند» (صندوق ETF درآمد ثابت) ──")
for item in search("كمند"):
    print(json.dumps(item, ensure_ascii=False, indent=2))

# وبملت (سهام بانک)
print("\n── جستجو «وبملت» (سهام بانک) ──")
for item in search("وبملت")[:2]:
    print(json.dumps(item, ensure_ascii=False, indent=2))
