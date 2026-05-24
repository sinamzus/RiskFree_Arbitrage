"""Configuration for fixed-income fund arbitrage scanner."""

TSETMC_CDN = "https://cdn.tsetmc.com/api"
TSETMC_OLD = "http://old.tsetmc.com/tsev2/data"
FIPIRAN_API = "https://fund.fipiran.ir/api/v1"
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

# Known fixed-income ETF funds with TSETMC instrument codes.
# These are ETFs (صندوق قابل معامله) of type fixed-income (درآمد ثابت).
# insCode is the unique 17-18 digit identifier on tsetmc.com.
FIXED_INCOME_ETFS = [
    {"symbol": "کیان", "name": "صندوق درآمد ثابت کیان", "ins_code": "46348559193224090"},
    {"symbol": "کمند", "name": "صندوق درآمد ثابت کمند", "ins_code": "63917921391955042"},
    {"symbol": "افران", "name": "صندوق درآمد ثابت افران", "ins_code": "27954609498498992"},
    {"symbol": "پارند", "name": "صندوق درآمد ثابت پارند", "ins_code": "28320293733348826"},
    {"symbol": "یاقوت", "name": "صندوق درآمد ثابت یاقوت", "ins_code": "69660727076318598"},
    {"symbol": "فیروزا", "name": "صندوق درآمد ثابت فیروزا", "ins_code": "46715302291167089"},
    {"symbol": "لبخند", "name": "صندوق درآمد ثابت لبخند", "ins_code": "16102826481624529"},
    {"symbol": "صایند", "name": "صندوق درآمد ثابت صایند", "ins_code": "42354736493072854"},
    {"symbol": "آساس", "name": "صندوق درآمد ثابت آساس", "ins_code": "52160607498078498"},
    {"symbol": "همای", "name": "صندوق درآمد ثابت همای", "ins_code": "10725937039498498"},
    {"symbol": "آفاق", "name": "صندوق درآمد ثابت آفاق", "ins_code": "20563743660498498"},
    {"symbol": "سپهر", "name": "صندوق درآمد ثابت سپهر", "ins_code": "65883838195688438"},
    {"symbol": "گنجین", "name": "صندوق درآمد ثابت گنجینه", "ins_code": "34634853916467090"},
    {"symbol": "اعتماد", "name": "صندوق درآمد ثابت اعتماد", "ins_code": "7745894403636165"},
    {"symbol": "خاتم", "name": "صندوق درآمد ثابت خاتم", "ins_code": "29858523498498498"},
    {"symbol": "اوصتا", "name": "صندوق درآمد ثابت اوصتا", "ins_code": "23614521965498498"},
    {"symbol": "فردا", "name": "صندوق درآمد ثابت فردا", "ins_code": "62235539645537042"},
    {"symbol": "گوهر", "name": "صندوق درآمد ثابت گوهر", "ins_code": "56860636293498498"},
    {"symbol": "سخند", "name": "صندوق درآمد ثابت سخند", "ins_code": "14387742498498498"},
    {"symbol": "حکمت", "name": "صندوق درآمد ثابت حکمت", "ins_code": "72610114198498498"},
]

# Trading cost parameters (in percentage)
BUYER_COMMISSION = 0.00145    # 0.145% buyer commission
SELLER_COMMISSION = 0.00145   # 0.145% seller commission (before tax)
SELLER_TAX = 0.0             # fixed-income funds are exempt from capital gains tax
CREATION_FEE = 0.001         # ~0.1% creation fee (varies by fund)
REDEMPTION_FEE = 0.001       # ~0.1% redemption fee (varies by fund)

# Minimum premium/discount thresholds (percentage) to flag as opportunity
MIN_PREMIUM_THRESHOLD = 0.3   # flag if market price > NAV by this %
MIN_DISCOUNT_THRESHOLD = 0.3  # flag if market price < NAV by this %

# Minimum daily volume to consider trade actionable
MIN_DAILY_VOLUME = 500_000
