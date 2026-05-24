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
    # All ins_codes must be confirmed to belong to the ETF FUND instrument, not
    # a stock with the same ticker.  Discovery uses lVal30.contains("صندوق") as
    # the primary selector (see TSETMCFetcher.discover_ins_code).
    # ins_codes are populated at runtime via TSETMC search; leave empty here so
    # they are always re-verified against the "صندوق" name filter.
    {"symbol": "کیان",   "name": "صندوق درآمد ثابت کیان"},
    {"symbol": "پارند",  "name": "صندوق درآمد ثابت پارند"},
    {"symbol": "اعتماد", "name": "صندوق درآمد ثابت اعتماد"},
    {"symbol": "کمند",   "name": "صندوق درآمد ثابت کمند"},
    {"symbol": "افران",  "name": "صندوق درآمد ثابت افران"},
    {"symbol": "لبخند",  "name": "صندوق درآمد ثابت لبخند"},
    {"symbol": "آساس",   "name": "صندوق درآمد ثابت آساس"},
    {"symbol": "آفاق",   "name": "صندوق درآمد ثابت آفاق"},
    {"symbol": "گنجین",  "name": "صندوق درآمد ثابت گنجینه"},
    {"symbol": "خاتم",   "name": "صندوق درآمد ثابت خاتم"},
    {"symbol": "اوصتا",  "name": "صندوق درآمد ثابت اوصتا"},
    {"symbol": "فردا",   "name": "صندوق درآمد ثابت فردا"},
    {"symbol": "گوهر",   "name": "صندوق درآمد ثابت گوهر"},
    {"symbol": "سخند",   "name": "صندوق درآمد ثابت سخند"},
    # Funds whose ticker contains Persian characters not found by direct search
    {"symbol": "سپهر",   "name": "صندوق سرمایه‌گذاری سپهر آتی"},
    {"symbol": "یاقوت",  "name": "صندوق درآمد ثابت یاقوت"},
    {"symbol": "فیروزا", "name": "صندوق درآمد ثابت فیروزا"},
    {"symbol": "صایند",  "name": "صندوق درآمد ثابت صایند"},
    {"symbol": "همای",   "name": "صندوق درآمد ثابت همای"},
    {"symbol": "حکمت",   "name": "صندوق درآمد ثابت حکمت",
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
