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

# ── Parallel fetch settings (anti-ban) ──────────────────────────────────────
# Data fetching runs concurrently across funds to overlap network latency.
# CRITICAL: the *aggregate* request rate is capped by a process-wide rate
# limiter (MIN_REQUEST_INTERVAL), so raising FETCH_WORKERS only overlaps
# latency — it does NOT increase the request rate that TSETMC observes.
# This keeps us fast without tripping TSETMC's per-IP throttling / ban.
#
#   FETCH_WORKERS        — number of concurrent fund fetchers
#   MIN_REQUEST_INTERVAL — minimum seconds between request *initiations*
#                          (global, across all threads). 0.2s ≈ 5 req/s.
FETCH_WORKERS        = 5
MIN_REQUEST_INTERVAL = 0.2

# Fixed-income ETF funds — 30 funds, all ins_codes verified via TSETMC
# GetClosingPriceInfo on 2026-05-24 (all returned live prices).
#
# Sources:
#   • TSETMC GetInstrumentSearch "درآمد ثابت" / "پایدار"
#   • fipiran.ir JS bundle analysis (fundlistissuebyfundtype)
#   • Direct verification via GetClosingPriceInfo
#
# flow=1 → بازار اول/دوم بورس اوراق بهادار تهران
# flow=2 → فرابورس ایران
FIXED_INCOME_ETFS = [
    # ══ کشف‌شده از TSETMC ۲۰۲۶-۰۵-۲۵ — ۸۷ صندوق تأیید‌شده با قیمت ══
    # مرتب‌شده بر اساس حجم معاملات روزانه (نزولی)
    # cgrValCot: H1 = بازار صندوق‌های قابل معامله بورس
    #            1A = بازار ابزارهای نوین مالی فرابورس

    # ── flow=2  فرابورس (1A) ─────────────────────────────────────────────
    {"symbol": "پاسارگاد",      "name": "صندوق درآمد ثابت پاسارگاد",          "ins_code": "61920683599787147"},
    {"symbol": "آوند",           "name": "صندوق آوند مفید",                    "ins_code": "31039212000825988"},
    {"symbol": "لبخند",          "name": "صندوق لبخند فارابی",                 "ins_code": "31569200988534548"},
    {"symbol": "اونیکس",         "name": "صندوق درآمد ثابت کیمیا",             "ins_code": "23498719713662118"},
    {"symbol": "کارآمد",         "name": "صندوق درآمد ثابت کارآمد",            "ins_code": "7803396484851273"},
    {"symbol": "دارا",           "name": "صندوق دارا الگوریتم",                "ins_code": "62012736978844991"},
    {"symbol": "پاداش",          "name": "صندوق ارزش پاداش",                   "ins_code": "43267179898797137"},
    {"symbol": "رشد",            "name": "صندوق رشد پایدار آبان",              "ins_code": "48287767791629523"},
    {"symbol": "ستاره",          "name": "صندوق ستاره پایدار سپهر",            "ins_code": "38845574746937458"},
    {"symbol": "دامون",          "name": "صندوق درآمد ثابت آسمان دامون",       "ins_code": "43009306066217458"},
    {"symbol": "آسان",           "name": "صندوق درآمد ثابت گنجینه مهر",        "ins_code": "65640021232361587"},
    {"symbol": "سام",            "name": "صندوق درآمد ثابت سام",               "ins_code": "55308018877404137"},
    {"symbol": "پایا",           "name": "صندوق پایا قلک پویا",                "ins_code": "55070742656326885"},
    {"symbol": "ثبات",           "name": "صندوق ثبات ویستا",                   "ins_code": "26780282166315918"},
    {"symbol": "پایش",           "name": "صندوق درآمد ثابت دینا",              "ins_code": "58722731270352481"},
    {"symbol": "نشان",           "name": "صندوق نشان هامرز",                   "ins_code": "1241998328504490"},
    {"symbol": "اعتماد",         "name": "صندوق اعتماد آفرین پارسیان",         "ins_code": "66818022341772870"},
    {"symbol": "سپنتارود",       "name": "صندوق درآمد ثابت ماه آفرید سپینود",  "ins_code": "31379272181300633"},
    {"symbol": "کاج",            "name": "صندوق نوع دوم نو ویرا",              "ins_code": "42670427020727409"},
    {"symbol": "یارا",           "name": "صندوق آریا",                         "ins_code": "45284811973404357"},
    {"symbol": "درین",           "name": "صندوق درین آتا",                     "ins_code": "21077182490095731"},
    {"symbol": "کامیاب",         "name": "صندوق کامیاب آشنا",                  "ins_code": "16040900750729921"},
    {"symbol": "کارما",          "name": "صندوق درآمد ثابت کیهان",             "ins_code": "33015297618582406"},
    {"symbol": "ساحل",           "name": "صندوق ساحل سرمایه امن خلیج فارس",   "ins_code": "62708526880913292"},
    {"symbol": "اوصتا",          "name": "صندوق اندیشه‌ورزان صباتامین",       "ins_code": "57761388729898548"},
    {"symbol": "اعتبار",         "name": "صندوق نوع دوم اعتبار",              "ins_code": "35163287528816137"},
    {"symbol": "اصیل",           "name": "صندوق درآمد ثابت مشترک البرز",      "ins_code": "8021561335311415"},
    {"symbol": "صایند",          "name": "صندوق گنجینه آینده روشن",            "ins_code": "45205530868811305"},
    {"symbol": "فاخر",           "name": "صندوق ثروت افزون فاخر",              "ins_code": "56344907495802692"},
    {"symbol": "شمیم",           "name": "صندوق درآمد ثابت شمیم تابان",        "ins_code": "64659064562132185"},
    {"symbol": "گنجین",          "name": "صندوق گنجینه یکم آوید",              "ins_code": "57728534324022361"},
    {"symbol": "هدف",            "name": "صندوق درآمد ثابت مشترک صبای هدف",   "ins_code": "56712769076499345"},
    {"symbol": "داریک",          "name": "صندوق اعتماد داریک",                 "ins_code": "71076372178147339"},
    {"symbol": "آکورد",          "name": "صندوق آرمان آتی کوثر",               "ins_code": "30282299500988269"},
    {"symbol": "سپیدما",         "name": "صندوق سپید دماوند",                  "ins_code": "2161110547458064"},
    {"symbol": "آلا",            "name": "صندوق اعتماد ارغوان",                "ins_code": "19828734979381742"},
    {"symbol": "رابین",          "name": "صندوق توسعه افق رابین",              "ins_code": "33527290777160784"},
    {"symbol": "اکسیژن",         "name": "صندوق درآمد ثابت اکسیژن",            "ins_code": "44558786393585356"},
    {"symbol": "اطمینان",        "name": "صندوق درآمد ثابت اطمینان هیوا",      "ins_code": "50243708970398750"},
    {"symbol": "ترنج",           "name": "صندوق درآمد ثابت ترنج سودمند",       "ins_code": "50264175787486822"},
    {"symbol": "همگام",          "name": "صندوق اوراق دولتی همگام",            "ins_code": "49681831162536304"},
    {"symbol": "سیناد",          "name": "صندوق سپهرسودمند سینا",              "ins_code": "31913287805282551"},
    {"symbol": "خورشید",         "name": "صندوق طلوع تدبیر پایا",              "ins_code": "4523009251964699"},
    {"symbol": "بمان",           "name": "صندوق بازده مانا",                   "ins_code": "52551569846042389"},
    {"symbol": "طلوع",           "name": "صندوق طلوع نوین ثابت",               "ins_code": "19060410060488876"},
    {"symbol": "خزانه‌ملت",      "name": "صندوق مختص اوراق دولتی ملت",        "ins_code": "65190029034514887"},
    {"symbol": "آکام",           "name": "صندوق نوع دوم آکام",                 "ins_code": "490987973229371"},
    {"symbol": "بازده",          "name": "صندوق بازده ثابت",                   "ins_code": "34856765062083074"},
    {"symbol": "گنجینه",         "name": "صندوق گنجینه داریوش",                "ins_code": "47101579271117172"},
    {"symbol": "آسود",           "name": "صندوق درآمد ثابت آرمان اقتصاد",     "ins_code": "16582961426722208"},
    {"symbol": "نیلی",           "name": "صندوق نوع دوم نیلی دماوند",          "ins_code": "31188566503248753"},
    {"symbol": "سخند",           "name": "صندوق سپهرخبرگان نفت",              "ins_code": "59598536122397373"},
    {"symbol": "نخل",            "name": "صندوق خلیج فارس",                   "ins_code": "27797446447955609"},
    {"symbol": "ماکان",          "name": "صندوق درآمد ثابت ثروت ماکان",       "ins_code": "62869646049188469"},
    {"symbol": "اندوخته‌داریوش", "name": "صندوق درآمد ثابت اندوخته داریوش",   "ins_code": "11540236653207133"},
    {"symbol": "آسامید",         "name": "صندوق مشترک آسمان امید",             "ins_code": "24869832924911721"},

    # ── flow=1  بورس (H1) ────────────────────────────────────────────────
    {"symbol": "یاقوت",          "name": "صندوق یاقوت آگاه",                   "ins_code": "1438514795814416"},
    {"symbol": "ماهور",          "name": "صندوق سرمایه‌گذاری ماهور",           "ins_code": "10458396610199724"},
    {"symbol": "ثمر",            "name": "صندوق ثمرگندم",                       "ins_code": "15420554853151242"},
    {"symbol": "همای",           "name": "صندوق همای آگاه",                    "ins_code": "15494954332657697"},
    {"symbol": "فیروزا",         "name": "صندوق ارمغان فیروزه آسیا",           "ins_code": "10795723506538053"},
    {"symbol": "افران",          "name": "صندوق افرا نماد پایدار",             "ins_code": "3846143218462419"},
    {"symbol": "ارکیده",         "name": "صندوق سرمایه‌گذاری ارکیده",         "ins_code": "12629673694762396"},
    {"symbol": "تداوم",          "name": "صندوق تداوم اطمینان تمدن",           "ins_code": "39453972158399542"},
    {"symbol": "کیان",           "name": "صندوق درآمد ثابت کیان",              "ins_code": "53251602435454519"},
    {"symbol": "توسکا",          "name": "صندوق توسعه فولاد ثابت",             "ins_code": "56871139881800017"},
    {"symbol": "آفاق",           "name": "صندوق افق آتی",                      "ins_code": "37073830945037165"},
    {"symbol": "سپر",            "name": "صندوق سپر سرمایه بیدار",             "ins_code": "17226661368470120"},
    {"symbol": "زمردکوروش",      "name": "صندوق زمرد کوروش",                   "ins_code": "57191287546444240"},
    {"symbol": "کمند",           "name": "صندوق درآمد ثابت کمند",              "ins_code": "34718633636164421"},
    {"symbol": "پارند",          "name": "صندوق پارند پایدار سپهر",            "ins_code": "70595828753641750"},
    {"symbol": "فردا",           "name": "صندوق آوای فردای زاگرس",             "ins_code": "65249046611427924"},
    {"symbol": "کارین",          "name": "صندوق نگین سامان",                   "ins_code": "16056283141617755"},
    {"symbol": "امین‌یکم",       "name": "صندوق درآمد ثابت امین یکم فردا",    "ins_code": "45728383369147894"},
    {"symbol": "آسا",            "name": "صندوق آرمان کارآفرین",               "ins_code": "26393127587277568"},
    {"symbol": "آرامش",          "name": "صندوق سرمایه‌گذاری آرامش",          "ins_code": "7104496710718469"},
    {"symbol": "رایبد",          "name": "صندوق رایبد",                        "ins_code": "25728853053886443"},
    {"symbol": "تصمیم",          "name": "صندوق درآمد ثابت تصمیم",             "ins_code": "53419976284977130"},
    {"symbol": "آتیه‌ملت",       "name": "صندوق آتیه ملت",                    "ins_code": "47492157735162813"},
    {"symbol": "صنهال",          "name": "صندوق نهال ایرانیان",                "ins_code": "52846735736632974"},
    {"symbol": "ماني",           "name": "صندوق درآمد ثابت مانی",             "ins_code": "61265100181977543"},
    {"symbol": "خاتم",           "name": "صندوق خاتم ایساتیس پویا",            "ins_code": "18865325633315847"},
    {"symbol": "هامرز",          "name": "صندوق اعتماد هامرز",                 "ins_code": "50503654866742146"},
    {"symbol": "بلوط",           "name": "صندوق شکوه بامداد زاگرس",            "ins_code": "4769396408895066"},
    {"symbol": "رایکا",          "name": "صندوق نوع دوم رایکا",                "ins_code": "66105959479616770"},
    {"symbol": "نیک‌گستر",       "name": "صندوق توسعه سرمایه نیکی",            "ins_code": "30977409629493496"},
    {"symbol": "دیبا",           "name": "صندوق اوراق دولتی صبا",              "ins_code": "70698996132397388"},
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
