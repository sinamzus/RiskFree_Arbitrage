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
    # ── Confirmed "درآمد ثابت" funds (direct name match, 2026-05-24 discovery) ──
    {"symbol": "امین‌یکم", "name": "صندوق درآمد ثابت امین یکم فردا",
     "ins_code": "45728383369147894"},
    {"symbol": "تصمیم",   "name": "صندوق با درآمد ثابت تصمیم",
     "ins_code": "53419976284977130"},
    {"symbol": "سام",     "name": "صندوق درآمد ثابت سام",
     "ins_code": "55308018877404137"},
    {"symbol": "کارآمد",  "name": "صندوق درآمد ثابت کارآمد",
     "ins_code": "7803396484851273"},
    {"symbol": "کارما",   "name": "صندوق درآمد ثابت کیهان",
     "ins_code": "33015297618582406"},
    {"symbol": "کمند",    "name": "صندوق درآمد ثابت کمند",
     "ins_code": "34718633636164421"},
    {"symbol": "کیان",    "name": "صندوق درآمد ثابت کیان",
     "ins_code": "53251602435454519"},
    {"symbol": "ماني",    "name": "صندوق با درآمد ثابت ماني",
     "ins_code": "61265100181977543"},
    {"symbol": "پاسارگاد", "name": "صندوق درآمد ثابت پاسارگاد",
     "ins_code": "61920683599787147"},
    {"symbol": "پایش",    "name": "صندوق درآمد ثابت دینا",
     "ins_code": "58722731270352481"},
    # ── Confirmed fixed-income ETFs with "ثابت" in name ──
    {"symbol": "پارند",   "name": "صندوق پارند پایدار سپهر",
     "ins_code": "70595828753641750"},
    {"symbol": "اعتماد",  "name": "صندوق اعتماد آفرین پارسیان",
     "ins_code": "66818022341772870"},
    {"symbol": "افران",   "name": "صندوق افرا نماد پایدار",
     "ins_code": "3846143218462419"},
    {"symbol": "لبخند",   "name": "صندوق لبخند فارابی",
     "ins_code": "31569200988534548"},
    {"symbol": "آفاق",    "name": "صندوق افق آتی",
     "ins_code": "37073830945037165"},
    {"symbol": "گنجین",   "name": "صندوق گنجینه یکم آوید",
     "ins_code": "57728534324022361"},
    {"symbol": "خاتم",    "name": "صندوق خاتم ایساتیس پویا",
     "ins_code": "18865325633315847"},
    {"symbol": "اوصتا",   "name": "صندوق اندیشه‌ورزان صباتامین",
     "ins_code": "57761388729898548"},
    {"symbol": "فردا",    "name": "صندوق آوای فردای زاگرس",
     "ins_code": "65249046611427924"},
    {"symbol": "سخند",    "name": "صندوق سپهرخبرگان نفت",
     "ins_code": "59598536122397373"},
    {"symbol": "یاقوت",   "name": "صندوق یاقوت آگاه",
     "ins_code": "1438514795814416"},
    {"symbol": "فیروزا",  "name": "صندوق ارمغان فیروزه آسیا",
     "ins_code": "10795723506538053"},
    {"symbol": "صایند",   "name": "صندوق گنجینه آینده روشن",
     "ins_code": "45205530868811305"},
    {"symbol": "همای",    "name": "صندوق همای آگاه",
     "ins_code": "15494954332657697"},
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
