"""Configuration for fixed-income fund arbitrage scanner."""

TSETMC_CDN = "https://cdn.tsetmc.com/api"
FIPIRAN_WEB = "https://www.fipiran.ir"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
}

REQUEST_TIMEOUT = 15

# Fixed-income ETF funds with TSETMC instrument codes.
# ins_code verified from live run on 2026-05-24.
# Alternative symbols: some funds appear under a different ticker on TSETMC
# so we provide alt_symbols for the search fallback.
FIXED_INCOME_ETFS = [
    # --- verified ins_codes (returned HTTP 200 on ClosingPriceInfo) ---
    {"symbol": "کیان",   "name": "صندوق درآمد ثابت کیان",       "ins_code": "46348559193224090"},
    {"symbol": "پارند",  "name": "صندوق درآمد ثابت پارند",      "ins_code": "28320293733348826"},
    {"symbol": "سپهر",   "name": "صندوق درآمد ثابت سپهر",       "ins_code": "65883838195688438"},
    {"symbol": "اعتماد", "name": "صندوق درآمد ثابت اعتماد",     "ins_code": "7745894403636165"},
    # --- auto-discovered ins_codes (from live TSETMC search 2026-05-24) ---
    {"symbol": "کمند",   "name": "صندوق درآمد ثابت کمند",       "ins_code": "34718633636164421"},
    {"symbol": "افران",  "name": "صندوق درآمد ثابت افران",      "ins_code": "3846143218462419"},
    {"symbol": "لبخند",  "name": "صندوق درآمد ثابت لبخند",      "ins_code": "31569200988534548"},
    {"symbol": "آساس",   "name": "صندوق درآمد ثابت آساس",       "ins_code": "66682662312253625"},
    {"symbol": "آفاق",   "name": "صندوق درآمد ثابت آفاق",       "ins_code": "37073830945037165"},
    {"symbol": "گنجین",  "name": "صندوق درآمد ثابت گنجینه",     "ins_code": "65640021232361587"},
    {"symbol": "خاتم",   "name": "صندوق درآمد ثابت خاتم",       "ins_code": "18865325633315847"},
    {"symbol": "اوصتا",  "name": "صندوق درآمد ثابت اوصتا",      "ins_code": "57761388729898548"},
    {"symbol": "فردا",   "name": "صندوق درآمد ثابت فردا",       "ins_code": "65249046611427924"},
    {"symbol": "گوهر",   "name": "صندوق درآمد ثابت گوهر",       "ins_code": "12390706505809150"},
    {"symbol": "سخند",   "name": "صندوق درآمد ثابت سخند",       "ins_code": "59598536122397373"},
    # --- ins_codes discovered via TSETMC search (live run 2026-05-24) ---
    {"symbol": "یاقوت",  "name": "صندوق درآمد ثابت یاقوت",      "ins_code": "1438514795814416"},
    {"symbol": "فیروزا", "name": "صندوق درآمد ثابت فیروزا",     "ins_code": "10795723506538053"},
    {"symbol": "صایند",  "name": "صندوق درآمد ثابت صایند",      "ins_code": "45205530868811305"},
    {"symbol": "همای",   "name": "صندوق درآمد ثابت همای",       "ins_code": "15494954332657697"},
    # حکمت: TSETMC search returns 12 hits; the ETF fund ins_code is not confirmed
    {"symbol": "حکمت",   "name": "صندوق درآمد ثابت حکمت",       "ins_code": "",
     "alt_symbols": ["ثحکمت", "حکمت1", "صحکمت"]},
]

# Trading cost parameters
BUYER_COMMISSION  = 0.00145
SELLER_COMMISSION = 0.00145
SELLER_TAX        = 0.0
CREATION_FEE      = 0.001
REDEMPTION_FEE    = 0.001

# Minimum thresholds
MIN_PREMIUM_THRESHOLD  = 0.3   # %
MIN_DISCOUNT_THRESHOLD = 0.3   # %
MIN_DAILY_VOLUME       = 500_000
