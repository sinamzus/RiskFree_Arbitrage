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
    # Verified fixed-income ETF funds (2026-05-24 scan).
    # All 16 below got P0 match: exact ticker + lVal30 contains "صندوق".
    # ins_codes populated at runtime via TSETMC search.
    {"symbol": "کیان",   "name": "صندوق درآمد ثابت کیان"},
    {"symbol": "پارند",  "name": "صندوق درآمد ثابت پارند"},
    {"symbol": "اعتماد", "name": "صندوق درآمد ثابت اعتماد"},
    {"symbol": "کمند",   "name": "صندوق درآمد ثابت کمند"},
    {"symbol": "افران",  "name": "صندوق درآمد ثابت افران"},
    {"symbol": "لبخند",  "name": "صندوق درآمد ثابت لبخند"},
    {"symbol": "آفاق",   "name": "صندوق درآمد ثابت آفاق"},
    {"symbol": "گنجین",  "name": "صندوق درآمد ثابت گنجینه"},
    {"symbol": "خاتم",   "name": "صندوق درآمد ثابت خاتم"},
    {"symbol": "اوصتا",  "name": "صندوق درآمد ثابت اوصتا"},
    {"symbol": "فردا",   "name": "صندوق درآمد ثابت فردا"},
    {"symbol": "سخند",   "name": "صندوق درآمد ثابت سخند"},
    {"symbol": "یاقوت",  "name": "صندوق درآمد ثابت یاقوت"},
    {"symbol": "فیروزا", "name": "صندوق درآمد ثابت فیروزا"},
    {"symbol": "صایند",  "name": "صندوق درآمد ثابت صایند"},
    {"symbol": "همای",   "name": "صندوق درآمد ثابت همای"},
    # Removed:
    # آساس  → matched "صندوق س.آسمان آرماني سهام" (equity fund, not fixed-income)
    # گوهر  → matched "صندوق طلاي كيان" (gold fund, not fixed-income)
    # سپهر  → no ETF fund match (P2 hit a non-fund instrument)
    # حکمت  → no ETF fund match (P2 hit "بانك حكمت ايرانيان", a bank)
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
