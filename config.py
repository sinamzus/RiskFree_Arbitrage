"""Configuration for fixed-income fund arbitrage scanner."""

TSETMC_CDN = "https://cdn.tsetmc.com/api"
FIPIRAN_WEB = "https://www.fipiran.ir"  # NOT fund.fipiran.ir (DNS fails on some networks)

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

# Known fixed-income ETF funds.
# ins_code: TSETMC unique instrument ID — empty string "" means auto-discover via search.
# Verified working codes are marked ✓ (returned HTTP 200 on ClosingPriceInfo).
# All others are cleared to "" so they are auto-discovered at runtime.
FIXED_INCOME_ETFS = [
    {"symbol": "کیان",   "name": "صندوق درآمد ثابت کیان",       "ins_code": "46348559193224090"},  # ✓
    {"symbol": "پارند",  "name": "صندوق درآمد ثابت پارند",      "ins_code": "28320293733348826"},  # ✓
    {"symbol": "سپهر",   "name": "صندوق درآمد ثابت سپهر",       "ins_code": "65883838195688438"},  # ✓
    {"symbol": "اعتماد", "name": "صندوق درآمد ثابت اعتماد",     "ins_code": "7745894403636165"},   # ✓
    {"symbol": "کمند",   "name": "صندوق درآمد ثابت کمند",       "ins_code": ""},
    {"symbol": "افران",  "name": "صندوق درآمد ثابت افران",      "ins_code": ""},
    {"symbol": "یاقوت",  "name": "صندوق درآمد ثابت یاقوت",      "ins_code": ""},
    {"symbol": "فیروزا", "name": "صندوق درآمد ثابت فیروزا",     "ins_code": ""},
    {"symbol": "لبخند",  "name": "صندوق درآمد ثابت لبخند",      "ins_code": ""},
    {"symbol": "صایند",  "name": "صندوق درآمد ثابت صایند",      "ins_code": ""},
    {"symbol": "آساس",   "name": "صندوق درآمد ثابت آساس",       "ins_code": ""},
    {"symbol": "همای",   "name": "صندوق درآمد ثابت همای",       "ins_code": ""},
    {"symbol": "آفاق",   "name": "صندوق درآمد ثابت آفاق",       "ins_code": ""},
    {"symbol": "گنجین",  "name": "صندوق درآمد ثابت گنجینه",     "ins_code": ""},
    {"symbol": "خاتم",   "name": "صندوق درآمد ثابت خاتم",       "ins_code": ""},
    {"symbol": "اوصتا",  "name": "صندوق درآمد ثابت اوصتا",      "ins_code": ""},
    {"symbol": "فردا",   "name": "صندوق درآمد ثابت فردا",       "ins_code": ""},
    {"symbol": "گوهر",   "name": "صندوق درآمد ثابت گوهر",       "ins_code": ""},
    {"symbol": "سخند",   "name": "صندوق درآمد ثابت سخند",       "ins_code": ""},
    {"symbol": "حکمت",   "name": "صندوق درآمد ثابت حکمت",       "ins_code": ""},
]

# Trading cost parameters
BUYER_COMMISSION  = 0.00145   # 0.145% buyer commission
SELLER_COMMISSION = 0.00145   # 0.145% seller commission
SELLER_TAX        = 0.0       # fixed-income ETFs are exempt
CREATION_FEE      = 0.001     # ~0.1% creation fee (varies per fund)
REDEMPTION_FEE    = 0.001     # ~0.1% redemption fee (varies per fund)

# Minimum thresholds
MIN_PREMIUM_THRESHOLD  = 0.3   # %
MIN_DISCOUNT_THRESHOLD = 0.3   # %
MIN_DAILY_VOLUME       = 500_000
