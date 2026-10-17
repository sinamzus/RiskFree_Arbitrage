# تاریخچهٔ تغییرات پروژه (از ابتدا تا امروز)

> این سند از روی `git log --reverse --date=short` (۱۵۳ کامیت روی برنچ کاری) و متن کامل پیام‌های کامیت ساخته شده است.
> تاریخ‌ها میلادی‌اند و معادل جلالی در عنوان هر مرحله آمده است. فهرست کامل کامیت‌ها در پیوست انتهای سند است.
> برای معماری و نحوهٔ اجرا [`README_FA.md`](README_FA.md) و برای یافته‌ها [`FINDINGS_FA.md`](FINDINGS_FA.md) را ببینید.

## خلاصهٔ زمانی

| مرحله | بازه | موضوع | تعداد کامیت |
|---|---|---|---|
| ۰ | ۲۰۲۶-۰۶-۰۹ | کد پایه: اسکنر صندوق‌های درآمد ثابت + داشبورد وضعیت داده | ۱ |
| ۱ | ۲۰۲۶-۰۶-۰۹ | منحنی بازده و بک‌تست اخزا | ۲۸ |
| ۲ | ۲۰۲۶-۰۶-۱۰ | بهینه‌ساز سریع اخزا | ۹ |
| ۳ | ۲۰۲۶-۰۶-۱۳ | بک‌فیل ۱۰ سالهٔ اخزا | ۱ |
| ۴ | ۲۰۲۶-۰۶-۱۵ | بازارگردانی | ۳ |
| ۵ | ۲۰۲۶-۰۶-۱۶ | آپشن آربیتراژ | ۸ |
| ۶ | ۲۰۲۶-۱۰-۰۶ | dump NAV درون‌روزی و تولد تب «تخفیف از NAV» | ۱۷ |
| ۷ | ۲۰۲۶-۱۰-۰۶ | ابزارهای پژوهشی راهبرد حباب و نخستین مجموعهٔ آزمون | ۱۵ |
| ۸ | ۲۰۲۶-۱۰-۰۷ تا ۱۰-۱۰ | فیلتر ریزش، سرمایهٔ بیکار و حالت «همیشه سرمایه‌گذاری» | ۱۳ |
| ۹ | ۲۰۲۶-۱۰-۱۰ | دادهٔ تیک و اردربوک TSE | ۸ |
| ۱۰ | ۲۰۲۶-۱۰-۱۱ | سرعت: کش، موازی‌سازی، numba | ۹ |
| ۱۱ | ۲۰۲۶-۱۰-۱۱ تا ۱۰-۱۲ | واقع‌گرایی اجرا و نخستین پارامترهای مسجل | ۱۱ |
| ۱۲ | ۲۰۲۶-۱۰-۱۳ تا ۱۰-۱۴ | ممیزی‌ها، راستی‌آزمای مستقل، قابلیت اطمینان بهینه‌ساز | ۱۰ |
| ۱۳ | ۲۰۲۶-۱۰-۱۴ تا ۱۰-۱۵ | NAV ریزدانه (nav_feed) و dump خام PostgreSQL | ۷ |
| ۱۴ | ۲۰۲۶-۱۰-۱۵ | مطالعهٔ قدرت پیش‌بینی سیگنال و اجرای بدون مرورگر | ۵ |
| ۱۵ | ۲۰۲۶-۱۰-۱۶ تا ۱۰-۱۷ | بازبینی مسجل‌ها، لبهٔ کند، رژیم بازار، پیکربندی نهایی | ۸ |
| ۱۶ | ۲۰۲۶-۱۰-۱۷ | مطالعهٔ سفارش‌های پرت در اردربوک (ثانیه‌به‌ثانیه) | ۱ |

---

## مرحلهٔ ۰ — کد پایه (۱۹ خرداد ۱۴۰۵ / ۲۰۲۶-۰۶-۰۹)

مخزن کم‌عمق (shallow) است و تاریخچهٔ پیش از کامیت `2a3fb2d` در آن نیست؛ این کامیت ۲۳ فایل و حدود ۱۳۸۰۰ خط را یکجا در بر دارد:

- **اسکنر آربیتراژ بین‌روزی صندوق‌های درآمد ثابت** (`main.py`، `arbitrage.py`، `orderbook.py`، `intraday_context.py`، `display.py`): مقایسهٔ قیمت بازار با NAV ابطال/صدور، بررسی اجراپذیری در اردربوک، حالت‌های ترمینالی، `--watch` و داشبورد وب (`--serve`).
- **جمع‌آوری داده** از TSETMC و FIPIRAN (`data_fetcher.py`) با محدودکنندهٔ نرخ سراسری برای جلوگیری از مسدود شدن IP؛ ذخیره در SQLite (`database.py`): تاریخچهٔ روزانه، تیک، اردربوک، اسنپ‌شات اسکن‌ها، کش NAV.
- `bonds.py` و نسخهٔ اولیهٔ `bond_backtest.py` (اخزا)، `backtest.py` (بازگشت قیمت صندوق‌ها) و ابزارهای اکتشافی `tools/` (کشف صندوق‌ها، جست‌وجوی منبع NAV درون‌روزی، بک‌فیل تیک و اردربوک).
- **ویژگی همین کامیت:** پنجرهٔ «📊 وضعیت داده» برای دیدن کامل بودن هر نوع داده به‌ازای هر نماد و دستور جمع‌آوری انتخابی (`collector.py`، `/api/data_coverage`، `/api/collect`).

## مرحلهٔ ۱ — منحنی بازده و بک‌تست اخزا (۱۹ خرداد ۱۴۰۵)

- **رابط:** نمودار منحنی بازدهٔ راست‌به‌چپ، متحرک و تعاملی (`f8f6b8d`)؛ انتخابگر تاریخ جلالی (`11fd98d`)؛ جدول‌های قابل‌مرتب‌سازی، انتخاب مبنای YTM و پنل جزئیات هر اوراق (`a553724`)؛ پنجرهٔ اردربوک در لحظهٔ معامله (`8b95ece`، `ec90e89`).
- **درستی داده:** محدودکردن بک‌تست به جلسهٔ ۰۹:۰۰ تا ۱۲:۳۰ برای حذف قیمت‌های پیش‌گشایش (`15f8c29`)؛ فیلتر اخزای سررسیدشده (`78c331d`).
- **بهینه‌ساز:** جستجوی درشت‌به‌ریز با Sharpe/Sortino/Calmar، walk-forward و نمرهٔ پایداری (`d3fd884`)؛ نگه‌داری چندروزه (`daf0408`)؛ افزایش سرعت ۲ تا ۳ برابری بیت‌به‌بیت (`ecceb75`) و جریان تصمیم کش‌شده (`e7cb5cd`).
- **راهبرد «سفارش پرت»** کنار z-spread: خرید یک سفارش فروش بسیار ارزان‌تر از منحنی به اندازهٔ حجم خودش (`af9c846`) — ایده‌ای که اکنون برای صندوق‌های طلا دوباره بررسی می‌شود.
- **رفع نقص‌های ساختاری:** سیگنال روی قیمت میانی ولی اجرا روی bid/ask بود؛ حالت `exec` پیش‌فرض شد (`2a4b60e`). پر کردن کل نردبان اردربوک به‌جای محدود کردن به قیمت لبه (`1bd3dbd`).
- **مدیریت سرمایه و ریسک:** حساب سرمایهٔ مشترک و MWRR (`b347807`)؛ باند ورود z و برازش مقاوم منحنی (`1dec59e`)؛ `min_dtm` (`eef66b1`)؛ مدل «نقد = زیان» یعنی خروج فقط با جایگزین (`bea534f`)؛ `min_hold_days` (`72ba43a`)؛ توضیح علت زیان هر معامله (`565753e`)؛ ابزار تشخیص زیان (`a3d3d03`، `a07bd70`).

## مرحلهٔ ۲ — بهینه‌ساز سریع اخزا (۲۰ خرداد ۱۴۰۵)

- افزودن پارامترهای پرتأثیر به شبکهٔ جستجو (`3d4cb5b`، `b07637b`، `67812b3`، `19a1a03`)؛ فضای درشت به حدود ۱٫۶ میلیون ترکیب رسید.
- **بازپخش فشردهٔ تریگرها + موازی‌سازی fork** (`949ee23`): ۱۲ تا ۲۸ برابر سریع‌تر، با آزمون برابری بیت‌به‌بیت `test_optimizer_parity.py` روی بیش از ۲۲۵ هزار ترکیب.
- موازی‌سازی بومی ویندوز با spawn (`d0df9e1`)؛ تاریخچهٔ اجراها با بازگردانی یک‌کلیکی پارامترها (`b4fe8ce`)؛ نمایش بازهٔ واقعی تاریخ (`5ef4cb0`)؛ شبکهٔ متراکم‌تر (`4a30119`).

## مرحلهٔ ۳ — بک‌فیل ۱۰ سالهٔ اخزا (۲۳ خرداد ۱۴۰۵)

- `tools/backfill_akhza_10y.py` (`90af8ed`): جمع‌آوری روزانه، تیک و اردربوک ۱۰ سال اخزا **شامل سری‌های سررسیدشده**، مرحله‌ای و قابل‌ازسرگیری؛ چون TSETMC به IP مراکز داده 403 می‌دهد باید روی رایانهٔ کاربر اجرا شود.

## مرحلهٔ ۴ — بازارگردانی (۲۵ خرداد ۱۴۰۵)

- موتور، بهینه‌ساز و تب بازارگردانی قانونی سهام با قیود دستورالعمل سازمان بورس و مدل پرشدن محافظه‌کارانهٔ صف (`842f094`).
- مدل صف خرید/فروش (قفل در سقف/کف دامنه) و وظیفهٔ رفع صف (`58d08a2`)؛ رفع مشکل واچ‌لیست سهام (`751fab2`).

## مرحلهٔ ۵ — آپشن آربیتراژ (۲۶ خرداد ۱۴۰۵)

- موتور Conversion / Box / Reversal با قیمت‌های اجرایی و کارمزد، بهینه‌ساز و ابزار جمع‌آوری (`0ac1c0d`)؛ نمودار سود و زیان در سررسید (`b36c3da`).
- اسکن زندهٔ زنجیره با قیمت لحظه‌ای TSETMC (`a33bf6e`)، تشخیص بسته بودن بازار و Box بدون نیاز به پایه (`ac76cc2`)؛ پنل مدیریت داده و طراحی سه‌زبانه‌ای (`69c0aab`، `3f39305`)؛ رفع خطای منطقهٔ زمانی ویندوز با `tzdata` (`8e7f2ba`).

> پس از این مرحله حدود چهار ماه کامیتی ثبت نشده است.

## مرحلهٔ ۶ — dump NAV درون‌روزی و تولد تب «تخفیف از NAV» (۱۴ مهر ۱۴۰۵ / ۲۰۲۶-۱۰-۰۶)

انگیزه: برای بک‌تست صادقانهٔ راهبردی که به NAV تکیه دارد، NAV «همان لحظه» لازم بود و TSETMC تاریخچهٔ NAV درون‌روزی ندارد. با رسیدن dump پایگاه دادهٔ PostgreSQL منبع داده (`symbols_symbolintraday`) این امکان فراهم شد.

- `Database.get_nav_series` (`9cd21f5`) و **واردکنندهٔ dump** (`sample.copy.zst`) به جدول `nav_intraday` با تطبیق `symbol_id` به نماد از روی حجم روزانه (`2f2d0b4`، `de81755`).
- **تب جدید و پیش‌فرض «📉 تخفیف از NAV»** (`70f4c18`): خرید صندوق زیر NAV و خروج با همگرایی، دسته‌بندی صندوق‌ها، بهینه‌ساز شبکه‌ای با تقسیم خارج از نمونه؛ تب‌های اوراق، بازارگردانی و آپشن پنهان شدند.
- تاریخ جلالی و سرمایه به میلیون ریال (`d69609a`)؛ طبقه‌بندی خودکار دسته‌ها (`e05bc9f`)؛ پنل فهرست صندوق‌ها با ویرایش (`665c1fd`).
- `tools/label_nav_funds.py`: نام‌گذاری همهٔ صندوق‌های dump از TSETMC و تعیین دسته (`f520001`، `6287f4b`، `68f191e`، `3174d30`).
- **حساب سرمایهٔ واحد با اندازهٔ پوزیشن مرکب** (`e7fe93b`)؛ **معیار منفعل** (شاخص هم‌وزن، خرید و نگه‌داری) با کارمزد هر تعدیل (`16dc52d`، `2e32518`، `8a0d3b0`).
- **«معمول» هر صندوق (baseline)** و حد ضررهای مبتنی بر NAV (`f05bcd3`): حباب دائمی هر صندوق کنار گذاشته می‌شود تا فقط ارزانی نسبت به معمول خودش سیگنال باشد.
- پیش‌فرض فقط دستهٔ طلا و دورهٔ پیش‌فرض از ۱ مهر ۱۴۰۴؛ بارگذاری تاریخچهٔ اضافه برای آماده بودن معمول از روز اول (`4a43d05`).

## مرحلهٔ ۷ — ابزارهای پژوهشی و نخستین مجموعهٔ آزمون (۱۴ مهر ۱۴۰۵)

- **حالت ورود با شاخص حباب** (میانگین ساده حباب صندوق‌های فعال) (`0a0de17`).
- **مجموعهٔ اعتبارسنجی** `discount_validation.py` (`41b6194`): آیا شکست بک‌تست از اجراست، از ایده یا از پارامترها؟ آزمون‌های جهان معلوم، علیت، تفکیک سود، p-value با null جایگشتی (کالیبره: ۴٫۳٪ مثبت کاذب در سطح ۵٪)، فشار هزینه، walk-forward.
- **فیلتر بازگشت به میانگین** و نمودار حباب درون‌روزی (`58652d9`)؛ راهنمای hover برای همهٔ کنترل‌ها و نمودار تعاملی با lightweight-charts (`2cfe2b6`).
- **ساعت جلسه:** داده نشان داد صندوق‌های طلا ساعت ۱۱:۳۰ تا ۱۸ هم اسنپ‌شات دارند و پنجرهٔ ثابت ۹ تا ۱۲:۳۰ تقریباً همه را دور می‌ریخت؛ کنترل ساعت (`e93b024`، `7d5be60`) و سپس **جلسهٔ خودکار هر صندوق و روز از رشد حجم** (`c6fd7b5`).
- **توضیح معاملات زیان‌ده** (`887bf32`)؛ نمودار سرمایهٔ درگیر با حسابداری زمان واقعی (`694218e`)؛ کارمزد پیش‌فرض ۰٫۱۲٪ (`779d82e`).
- **مطالعهٔ جامع پارامترها** به‌جای بهینه‌ساز شبکه‌ای کوچک (`8a9d7fb`): نمونه‌گیری متوازن، پالایش، اهمیت η²، جدول‌های حاشیه‌ای، همبستگی آموزش/آزمون، walk-forward؛ با اصلاحات بعدی (`97fe4e8`، `84d3382`، `e6beabe`).
- **مجموعهٔ آزمون سراسری** `discount_fulltest.py` (`1e73587`): ممیزی پارامترهای مرده، سیم‌کشی وب، برابری پیش‌فرض‌های فرم و موتور، ناوردایی‌ها و **آزمون جهش** (باگ‌های واقعی گذشته باید آزمون را بشکنند).

## مرحلهٔ ۸ — فیلتر ریزش، سرمایهٔ بیکار و «همیشه سرمایه‌گذاری» (۱۵ تا ۱۸ مهر ۱۴۰۵)

- **فیلتر ریزش بازار** (`b130a87`): وقتی قیمت گروه سریع می‌افتد ورود تازه ممنوع است؛ علّی و فقط بر پایهٔ قیمت.
- بهینه‌ساز همهٔ پارامترها را پوشش می‌دهد و ترکیب‌های ساختاری نمونه‌گیری می‌شوند نه شمارش (`dd9af7e`).
- **تشخیص سرمایهٔ بیکار و پارک آن در بهترین صندوق** (`fill_mode = best`) (`e1eeae6`، `f49b3e0`).
- حذف صندوق‌های دارای NAV کهنه در سطح دقیقه (`1f54899`).
- **حالت «همیشه سرمایه‌گذاری»** (`fill_mode = hold`) (`599f29e`): فروش فقط برای جابه‌جایی به کاندیدای دست‌کم `fill_switch_pct` ارزان‌تر — راهبردی که بعدها برنده شد.
- پنجرهٔ انتخاب پارامترهای بهینه‌ساز و مقدار ثابت (`13c47df`)؛ **پیش‌فرض‌های تازه و دکمهٔ ارسال نتایج به برنچ `analysis-results`** (`365a1a4`، ماژول `result_export.py`).
- تفکیک سود (NAV/حباب)، `fill_min_hold_min` و `fill_quote_age_min` (`7ade5d7`)؛ **زبانه‌های نتیجه، سقف حجم «تا همان لحظه»** به‌جای کل روز (حذف نگاه به آینده) و تأخیر اجرا (`444c61c`).
- **کارمزد ۰٫۱۲۵٪ هر طرف** و حکم سخت‌گیرانه‌تر «بهتر از منفعل» (`f07cc4b`).
- **اعتبارسنجی حالت hold با placebo** (رتبه‌بندی با حباب صندوقی دیگر) (`98f9f39`)؛ رفع نادیده‌ماندن سناریوهای حساسیت (`5355679`).

## مرحلهٔ ۹ — دادهٔ تیک و اردربوک TSE (۱۸ مهر ۱۴۰۵)

انگیزه: dump NAV فقط آخرین قیمت داشت و اسپرد فرضی بود؛ برای سنجش واقعی اجرا، اردربوک واقعی لازم بود.

- **فاز ۱ – جمع‌آوری** (`0665e4a`): `tse_gold.py` برای هر (صندوق، روز) همهٔ معاملات و جریان کامل تغییرات اردربوک را در `tse_raw` ذخیره می‌کند؛ پنل «📥 دادهٔ TSE» و `tools/collect_tse_gold.py`.
- **فاز ۲ – دیتاست TSE** (`b0e5781`): `tse_dataset.py` تیک و اردربوک را روی شبکهٔ زمانی بازپخش می‌کند؛ خرید روی askها و فروش روی bidها، محدود به عمق.
- **دیتاست ترکیبی (hybrid)** (`ec0a754`): سیگنال از dump، اجرا روی اردربوک واقعی؛ حذف کامل اسپرد فرضی در دیتاست‌های اردربوکی و فروش جابه‌جایی روی اردربوک لحظهٔ فروش (`388272b`).
- تصمیم با قیمت‌های اجرایی و **بررسی همخوانی ساعت NAV و TSE** (`38d7e26`)؛ حذف اسنپ‌شات‌های خارج از پنجرهٔ بازار (`04d7404`)؛ کش اردربوک (`8144760`)؛ رفع گیرکردن صفحه به‌خاطر یک `async` نابه‌جا و افزودن بررسی نحو JS به آزمون‌ها (`52ac9e6`).

## مرحلهٔ ۱۰ — سرعت (۱۹ مهر ۱۴۰۵)

- ذخیرهٔ وضعیت‌های اردربوک ترکیبی در `tse_hybrid`، خواندن یکجای پنجره‌های بازار، زمان‌سنجی (`aa022e8`).
- **کارگرهای موازی بهینه‌ساز** (`7f41e9f`، `e184c5a`) و کار با ترکیب‌های ساختاری کامل در هر کارگر (`b9dd78b`)؛ اشتراک دادهٔ آماده با فایل موقت (`38eb55f`).
- **کش روی دیسک** که پس از راه‌اندازی دوباره باقی می‌ماند (`0a2d66a`) و آماده‌سازی یک‌بارهٔ کل تاریخچهٔ هر صندوق (`7ff8760`).
- **حالت hold: یک اجرای پیوسته به‌ازای هر پیکربندی** به‌جای اجرای جدا در هر بلوک؛ بلوک‌ها از ارزش‌گذاری روزانه خوانده می‌شوند (`f0bf83f`).
- **بازپخش کامپایل‌شده با numba** (`c28cab5`): حدود ۱۰ برابر سریع‌تر و دقیقاً برابر نسخهٔ پایتونی.

## مرحلهٔ ۱۱ — واقع‌گرایی اجرا و نخستین پارامترهای مسجل (۱۹ و ۲۰ مهر ۱۴۰۵)

- **تأیید دوگانهٔ سیگنال** (آخرین قیمت + سر صف مقابل) و مقایسهٔ نمودار با خرید و نگه‌داری (`3783e6f`).
- **نخستین پارامترهای مسجل** از مطالعه روی دیتاست ترکیبی (`35e98cd`): `hold`، جلسهٔ خودکار، قیمت تازه، فیلتر بازگشت خاموش، گروه ریزش = دسته.
- پنجرهٔ hover برای دیدن اردربوک، سیگنال و حباب‌ها در لحظهٔ هر معامله (`3b8cc8c`)؛ **مطالعهٔ حجم (ظرفیت)** و غیرفعال‌شدن فیلدهای بی‌معنا (`1b637b1`).
- **قاعدهٔ NAV کهنه:** صندوق تا NAV به‌روز نشود نه خریده می‌شود نه فروخته؛ حداکثر سن ۵ دقیقه (`3441526`)، اعمال در **لحظهٔ هر معامله** و گزارش سن NAV هر معامله (`a039ebd`)؛ سن NAV دیگر پارامتر بهینه‌ساز نیست بلکه قاعده است (`65329a2`، `4c8c76a`).
- **قیمت میانگین کل سفارش** برای تأیید سیگنال (`69482d0`)؛ ورود جزئی در حد قیمت و فروش جابه‌جایی فقط به اندازهٔ نیاز (`2559c94`)؛ **حداقل سفارش ۱/۲۰ پوزیشن** و خرید تکمیلی در lotهای جدا (`49c1d1b`).

## مرحلهٔ ۱۲ — ممیزی‌ها و قابلیت اطمینان (۲۱ و ۲۲ مهر ۱۴۰۵)

- **منحنی سرمایه و افت با ارزش روزانهٔ بازار** به‌جای بهای تمام‌شده (`1b4153b`).
- **ممیزی داده و اجرا** (`a1362dd`، `c73fe3e`): اثرانگشت محتوا برای کش‌ها، حذف نگاه به آینده در NAV، بودجهٔ مشترک روزانهٔ حجم، عدم استفادهٔ دوباره از یک bid، تأخیر اجرا؛ رفع کندی بررسی تازگی کش (`1c1f958`).
- **فروش هرگز فراتر از bidهای قابل‌مشاهده** و **راستی‌آزمای مستقل** `verify_independent.py` بدون اشتراک کد با موتور (`4b889c8`)؛ نمایش آن در زبانهٔ مستقل (`203bbcb`).
- **قابلیت اطمینان بهینه‌ساز** (`76f50f5`): PBO، بوت‌استرپ بلوکی ایستا، انتخاب «فلات»، همگرایی؛ بدون برتری معنادار، حکم هرگز «قابل اتکا» نیست.
- **سنجش بازده از پایان گرم‌شدن** و جستجوی فیلتر بازگشت به میانگین (`cf5ca3c`)؛ **فیلتر صعود** قرینهٔ فیلتر ریزش (`1f42c21`)؛ نادیده‌گرفتن سن NAV روزانه وقتی قاعدهٔ دقیقه فعال است (`08cd503`).

## مرحلهٔ ۱۳ — NAV ریزدانه (۲۲ و ۲۳ مهر ۱۴۰۵)

انگیزه: dump قدیمی نمونه‌برداری‌شده بود و ممکن بود NAVی را نشان دهد که بعداً دریافت شده (نگاه به آینده) یا به‌روزرسانی‌ای را جا بیندازد.

- `tools/import_nav_feed.py` و جدول `nav_feed` با `report_time` (لحظهٔ دریافت) و `nav_time` (`7fcd717`، `4f2f80a`، `d984dad`: ۲۳ میلیون ردیف، تطبیق با `code_fa`).
- **بک‌تست با «NAV دریافت‌شده تا هر لحظه»** در همهٔ دیتاست‌ها (`6b6773e`، ماژول `nav_feed.py`)؛ راستی‌آزما هم همین قاعده را مستقل اعمال می‌کند.
- `tools/survey_data_folder.py` (`d4ad916`) و **`tools/import_pg_dump.py`**: خواندن مستقیم dump خام PostgreSQL بدون نصب PostgreSQL و گزارش تازگی NAV (`fe94543`). یافته: `nav_time` حتی با NAV ثابت جلو می‌رود؛ پس تازگی باید با `nav_time` سنجیده شود.
- راستی‌آزما برای دیتاست `tse` با NAV معلوم در هر لحظهٔ شبکه (`af3f9ea`).

## مرحلهٔ ۱۴ — مطالعهٔ سیگنال و اجرای بدون مرورگر (۲۳ مهر ۱۴۰۵)

- **`discount_signal.py`** (`8721c91`): IC رتبه‌ای حباب خام، فاصله از معمول و تغییر یک‌روزه با بازده نسبی آینده (۱ ساعت، ۱ و ۵ روز)، تفکیک قیمت/NAV؛ گروه‌بندی نوار ابزار؛ دورهٔ پیش‌فرض ۱ مهر ۱۴۰۴ تا ۱ مهر ۱۴۰۵.
- **سود اجرایی جابه‌جایی** با bid/ask و کارمزد به‌عنوان شرط «قابل‌معامله بودن» سیگنال (`020f826`)؛ افق ۱۰ روزه و حداقل نگه‌داری تا ۵ و ۷ روز (`a262f88`)؛ آستانهٔ جابه‌جایی تا ۳ نقطه (`c73c3de`).
- **بهینه‌سازی که لپ‌تاپ را خفه نکند** و `tools/run_study.py` (`c551330`): کارگرها با اولویت پایین و محدود به حافظهٔ آزاد؛ سقف‌ها تا ۷ روز، ۲۰۰ هزار نمونه، ۲۰۰۰ ترکیب ساختاری.

## مرحلهٔ ۱۵ — بازبینی مسجل‌ها و پیکربندی نهایی (۲۴ و ۲۵ مهر ۱۴۰۵)

- **پیش‌تنظیم «بازبینی مسجل‌ها»** (`5ff73a8`) و داوری با **اجراهای جفتی** که پارامترهای بی‌اثر را «بی‌اثر» گزارش می‌کند (`cac6d1d`)؛ سقف زمان پس از آماده‌سازی داده شروع می‌شود (`f4e8469`).
- **پیش‌تنظیم «🐢 لبهٔ کند»** (`40f60fe`) بر پایهٔ یافتهٔ مطالعهٔ سیگنال (پیش‌بینی‌پذیری فقط در افق ۵ تا ۱۰ روز).
- **حذف حداقل تعداد معامله در حالت hold** (`c3bedde`): این شرط دقیقاً کندترین پیکربندی‌ها را دور می‌ریخت.
- **تفکیک مطالعهٔ سیگنال بر حسب رژیم بازار** (`d85e0a0`).
- **درخواست آمادهٔ مطالعهٔ تمرکز** `tools/requests/concentration.json` (`8586cf2`).
- **`86f5379` — پیش‌فرض‌ها = پیکربندی مسجل:** دیتاست TSE با شبکهٔ ۱۰ دقیقه، همیشه سرمایه‌گذاری، جابه‌جایی به صندوقی دست‌کم ۱ نقطه ارزان‌تر نسبت به معمول ۲۰ روزهٔ خودش، حداقل نگه‌داری ۲ روز، خرید فقط زیر معمول −۰٫۳، ۲۰٪ سرمایه برای هر صندوق، فیلتر ریزش ۱٫۵٪. موتور، فرم و دامنه‌های بهینه‌ساز با هم همسو شدند.

## مطالعهٔ سفارش‌های پرت

- **`5b9f01d` — مطالعهٔ سفارش‌های پرت** (`discount_stray.py`، دکمهٔ «🎣 سفارش‌های پرت»): بک‌تست بازار را فقط روی شبکهٔ زمانی می‌بیند، پس سفارشی که بین دو لحظهٔ شبکه می‌آید و می‌رود به آن نمی‌رسد. این مطالعه جریان خام اردربوک را ثانیه‌به‌ثانیه بازپخش می‌کند و برای فاصله‌های ۰٫۵ تا ۳ درصد از قیمت معمول می‌سنجد: چند بار، چه مدت، معامله یا لغو، آنچه با تأخیر واکنش ۰ تا ۶۰ ثانیه قابل برداشتن بود، سود کاغذی پس از کارمزد و سود واقعی ۱ ساعت و ۱ روز بعد. یک دنیای مصنوعی با سه سفارش پرت کاشته‌شده همهٔ اعداد را آزمون می‌کند (۲۸۰ بررسی پاس). نتیجهٔ دادهٔ واقعی هنوز نیامده است.

## مستندسازی

- پوشهٔ `docs/`: `README_FA.md` (معماری، داده، اجرا، مرجع پارامترها)، `CHANGELOG_FA.md` (همین تاریخچه)، `FINDINGS_FA.md` (یافته‌های پژوهش و پیکربندی نهایی).

## برنچ `analysis-results`

از ۱۸ مهر ۱۴۰۵ (۲۰۲۶-۱۰-۱۰) هر بار که دکمهٔ «📤 ارسال آخرین نتایج برای تحلیل» یا گزینهٔ `--push` ابزارها زده شود، یک کامیت `export results <زمان>` روی این برنچ ثبت می‌شود (تا ۲۵ مهر ۱۴۰۵، ۳۴ کامیت). محتوا در `results/`: `backtest.json`، `optimizer.json`، `validation.json`، `signal.json`، `verify.json`، `data_survey.*`، `pg_dump_report.*`، `meta.json`.

---

## پیوست — فهرست کامل کامیت‌ها (قدیمی به جدید)

عنوان‌ها همان عنوان اصلی کامیت‌اند.

| # | هش | تاریخ | عنوان |
|---|---|---|---|
| 1 | `2a3fb2d` | 2026-06-09 | feat: data-coverage dashboard + on-demand collection for funds & اخزا |
| 2 | `f8f6b8d` | 2026-06-09 | feat: RTL-correct, animated, interactive yield-curve chart |
| 3 | `11fd98d` | 2026-06-09 | feat: Jalali date picker + parameter optimizer for bond backtest |
| 4 | `a553724` | 2026-06-09 | feat: sortable tables, YTM basis selector, per-bond detail panel |
| 5 | `15f8c29` | 2026-06-09 | fix: restrict bond backtest to the 09:00–12:30 trading session |
| 6 | `78c331d` | 2026-06-09 | feat: filter matured اخزا in data view and backtest |
| 7 | `8b95ece` | 2026-06-09 | feat: editable bond backtest fees + hover order-book popup |
| 8 | `389fb64` | 2026-06-09 | Set default bond commission to 0.075% per side |
| 9 | `d3fd884` | 2026-06-09 | Advanced bond optimizer: coarse-to-fine, risk metrics, walk-forward, stability |
| 10 | `daf0408` | 2026-06-09 | Support multi-day (overnight) position holding in bond backtest |
| 11 | `77e7822` | 2026-06-09 | Show backtest entry/exit dates in Jalali calendar |
| 12 | `ecceb75` | 2026-06-09 | Speed up intraday bond backtest 2-3x (bit-exact) |
| 13 | `e7cb5cd` | 2026-06-09 | Speed up optimizer via cached z-spread decision streams |
| 14 | `af9c846` | 2026-06-09 | Add outlier-order strategy, restrict curve degree to 1-2, TSE links |
| 15 | `ec90e89` | 2026-06-09 | Fix backtest OB popup: compact ladders, show executed price, Jalali dates |
| 16 | `d9f980e` | 2026-06-09 | Fix bond backtest exit-date label and deep-link to dated TSE page |
| 17 | `c66d5f1` | 2026-06-09 | Add optional stricter signal-exit guard (min net-profit) for bonds |
| 18 | `b347807` | 2026-06-09 | Add portfolio capital management + MWRR (money-weighted return) |
| 19 | `2a4b60e` | 2026-06-09 | Fix structural flaw: z-spread signalled on mid but executes at bid/ask |
| 20 | `10f1364` | 2026-06-09 | Tidy bond backtest params UI; simplify capital model |
| 21 | `0e08e58` | 2026-06-09 | Fix trades table hidden after UI reorganization |
| 22 | `1bd3dbd` | 2026-06-09 | Fix structural loss: cap fills at the edge price, not the whole ladder |
| 23 | `a3d3d03` | 2026-06-09 | Add deeper loss breakdowns to bond diagnostic |
| 24 | `a07bd70` | 2026-06-09 | Add per-series curve-bias data-sanity report to bond diagnostic |
| 25 | `1dec59e` | 2026-06-09 | Add robustness guards: entry z-band + outlier-trimmed curve fit |
| 26 | `eef66b1` | 2026-06-09 | Add min_dtm filter; flag noisy series in diagnostic |
| 27 | `bea534f` | 2026-06-09 | Add 'cash = loss' exit model: hold unless a fresh buy exists |
| 28 | `72ba43a` | 2026-06-09 | Add min_hold_days guard to block intraday signal exits |
| 29 | `565753e` | 2026-06-09 | Add per-trade loss-reason tooltip to backtest trades table |
| 30 | `3d4cb5b` | 2026-06-10 | Add entry_max_bps, min_dtm, min_hold_days to optimizer search grid |
| 31 | `b07637b` | 2026-06-10 | Add force_eod, min_exit_profit_bps, exit_needs_replacement to optimizer grid |
| 32 | `b4fe8ce` | 2026-06-10 | Add persistent backtest & optimizer history with one-click param restore |
| 33 | `949ee23` | 2026-06-10 | Transform optimizer speed: trigger-compressed batch replay + fork parallelism |
| 34 | `d0df9e1` | 2026-06-10 | Add native Windows parallelism to the optimizer (spawn workers) |
| 35 | `5ef4cb0` | 2026-06-10 | Show actual date range used by optimizer in result panel |
| 36 | `4a30119` | 2026-06-10 | Densify coarse optimizer grid on cheap replay-only dimensions |
| 37 | `67812b3` | 2026-06-10 | Add z-momentum entry confirm + best-first funding as optional params |
| 38 | `19a1a03` | 2026-06-10 | Search real profit thresholds for min_exit_profit_bps in the optimizer |
| 39 | `90af8ed` | 2026-06-13 | Add resumable 10-year اخزا backfill (daily + ticks + order book) |
| 40 | `842f094` | 2026-06-15 | Add market-making (بازارگردانی) backtest, optimizer, tab & stock data tooling |
| 41 | `58d08a2` | 2026-06-15 | Model صف خرید/فروش (band-limit queue lock) in the market-making engine |
| 42 | `751fab2` | 2026-06-15 | Make stock --watch self-resolve ins_code (independent of MarketWatch) |
| 43 | `0ac1c0d` | 2026-06-16 | Add options arbitrage backtest: engine, optimizer, tab & data tooling |
| 44 | `b36c3da` | 2026-06-16 | Add payoff diagram (SVG) for options arbitrage opportunities |
| 45 | `8e7f2ba` | 2026-06-16 | Fix ZoneInfoNotFoundError on Windows (missing tzdata) |
| 46 | `a33bf6e` | 2026-06-16 | Add live options arbitrage scanner using real-time TSETMC prices |
| 47 | `69c0aab` | 2026-06-16 | Add options data management panel with discover from TSETMC |
| 48 | `e54a4f9` | 2026-06-16 | Fix URL-encoding of Persian names in options data panel |
| 49 | `3f39305` | 2026-06-16 | Redesign options tab into 3 clear sub-tabs (live / backtest / data) |
| 50 | `ac76cc2` | 2026-06-16 | Improve live options scan: market-closed detection + box-without-underlying |
| 51 | `9cd21f5` | 2026-10-06 | Add Database.get_nav_series to read merged daily NAV history |
| 52 | `2f2d0b4` | 2026-10-06 | Add importer for intraday-NAV PostgreSQL COPY dump (zst) + symbol_id mapping |
| 53 | `de81755` | 2026-10-06 | Improve NAV symbol mapping: fuzzy volume/price scoring, unmatched report, manual CSV map |
| 54 | `70f4c18` | 2026-10-06 | Add NAV-discount backtest tab: buy ETFs below intraday NAV, exit on convergence |
| 55 | `d69609a` | 2026-10-06 | NAV-discount tab: Jalali date pickers for start/end, capital in million rials |
| 56 | `e05bc9f` | 2026-10-06 | Auto-classify fund categories once on first open of the NAV-discount tab |
| 57 | `665c1fd` | 2026-10-06 | NAV-discount tab: fund list panel (matched ticker, category, source, stats) with inline edit |
| 58 | `f520001` | 2026-10-06 | Add label_nav_funds: name every NAV-dump fund from TSETMC once and for all |
| 59 | `6287f4b` | 2026-10-06 | Fix fund naming: word-prefix name rules, gold cluster wins over names, widen search to all instruments, flag duplicate ids |
| 60 | `68f191e` | 2026-10-06 | label_nav_funds: widen to every non-option/non-bond instrument, add price-series matching tier |
| 61 | `3174d30` | 2026-10-06 | label_nav_funds: extended paperTypes market-watch + --everything option for the last unnamed ids |
| 62 | `e7fe93b` | 2026-10-06 | NAV-discount backtest: single capital pool with compounding position sizing; report final capital |
| 63 | `16dc52d` | 2026-10-06 | NAV-discount backtest: passive benchmark (equal-weight index, buy-and-hold, NAV index) + comparison table and chart |
| 64 | `2e32518` | 2026-10-06 | Passive benchmark: daily-rebalanced equal-weight account now pays buy/sell fees on every rebalance |
| 65 | `f05bcd3` | 2026-10-06 | NAV-discount backtest: NAV-based stop-loss modes and per-fund permanent-bubble baseline |
| 66 | `8a0d3b0` | 2026-10-06 | Remove the cost-free theoretical index from the benchmark report |
| 67 | `4a43d05` | 2026-10-06 | NAV-discount tab: per-fund on/off inside each category, gold-only default, Jalali default period |
| 68 | `0a0de17` | 2026-10-06 | NAV-discount backtest: bubble-index entry mode (simple average of the active funds' bubbles) |
| 69 | `41b6194` | 2026-10-06 | Add validation suite: is a failing backtest due to execution, the idea, or the parameters? |
| 70 | `58652d9` | 2026-10-06 | Discount backtest: mean-reversion eligibility filter + intraday bubble chart |
| 71 | `2cfe2b6` | 2026-10-06 | Discount tab: hover tooltips everywhere + interactive TradingView bubble chart |
| 72 | `7d5be60` | 2026-10-06 | tools: nav_coverage — data points per day and hour for one fund |
| 73 | `e93b024` | 2026-10-06 | Session-hours control + nav_coverage --hours diagnostic |
| 74 | `c6fd7b5` | 2026-10-06 | Auto trading session per fund and day from volume growth |
| 75 | `887bf32` | 2026-10-06 | Explain losing trades: per-trade cause, loss-cause summary, jump to chart |
| 76 | `8a9d7fb` | 2026-10-06 | Comprehensive parameter study replaces the small grid optimizer |
| 77 | `97fe4e8` | 2026-10-06 | Study: exhaustive mode for small spaces, per-combo time slices, auto-relaxed min trades, clear errors |
| 78 | `84d3382` | 2026-10-06 | Study: fill dimensions that become active during refinement; apply shows what changed |
| 79 | `779d82e` | 2026-10-06 | Discount tab: default fee 0.12% on each side |
| 80 | `694218e` | 2026-10-06 | Capital-engaged chart + real-time exposure accounting with independent cross-check |
| 81 | `e6beabe` | 2026-10-06 | Study: search the entry/exit parameters that belong to the entry mode |
| 82 | `1e73587` | 2026-10-06 | End-to-end test suite for the discount tab (dead parameters, wiring, defaults, optimizer, invariants, mutation check) |
| 83 | `b130a87` | 2026-10-07 | Market-fall filter: no new entries while the group's price is falling fast |
| 84 | `dd9af7e` | 2026-10-07 | Optimizer covers every parameter; structural combinations sampled instead of enumerated |
| 85 | `e1eeae6` | 2026-10-07 | Idle-capital diagnostics + parking idle cash in the best-ranked fund |
| 86 | `f49b3e0` | 2026-10-10 | Optimizer: idle-capital parking (fill) is a searched dimension |
| 87 | `1f54899` | 2026-10-10 | Exclude funds with stale NAV (minute-level) from candidates |
| 88 | `599f29e` | 2026-10-10 | Add always-invested mode: sell only to switch into a better candidate |
| 89 | `13c47df` | 2026-10-10 | Optimizer UI: choose searched parameters and a fixed value for the rest |
| 90 | `365a1a4` | 2026-10-10 | New UI/engine defaults, optimizer parameter dialog, result export + push button |
| 91 | `7ade5d7` | 2026-10-10 | Profit attribution (NAV vs bubble), min hold before switching, quote age, liquidity stats for always-invested mode |
| 92 | `444c61c` | 2026-10-10 | Result tabs; volume-so-far cap and execution delay |
| 93 | `f07cc4b` | 2026-10-10 | Default fees 0.125% per side; stricter 'better than passive' verdict in the optimizer |
| 94 | `98f9f39` | 2026-10-10 | Validation for the always-invested mode |
| 95 | `5355679` | 2026-10-10 | Fix optimizer sensitivity: assumption overrides were ignored; add fill-delay scenario |
| 96 | `0665e4a` | 2026-10-10 | TSE tick + order-book collection for the gold funds (phase 1) |
| 97 | `b0e5781` | 2026-10-10 | Backtest / optimize / validate on the TSE dataset (trades + real order book) — phase 2 |
| 98 | `ec0a754` | 2026-10-10 | Hybrid dataset: signal from the NAV dump, execution against the real TSE order book |
| 99 | `388272b` | 2026-10-10 | Order-book datasets: no assumed spread anywhere; switch sales use the real book at the sale instant |
| 100 | `52ac9e6` | 2026-10-10 | Fix app stuck on loading: misplaced async keyword broke the page script; add JS syntax check to tests |
| 101 | `38d7e26` | 2026-10-10 | Order-book datasets: decisions at executable prices + NAV/TSE clock check |
| 102 | `04d7404` | 2026-10-10 | Hybrid dataset: drop NAV snapshots outside the TSE market window (no fills after the close) |
| 103 | `8144760` | 2026-10-10 | Order-book datasets: cache book loads per process and memoise book-at-instant lookups |
| 104 | `aa022e8` | 2026-10-11 | Fast order-book backtests: persistent hybrid book states, bulk market windows, cached clock check, timing |
| 105 | `7f41e9f` | 2026-10-11 | Optimizer: parallel worker processes + precomputed executable-price corrections |
| 106 | `0a2d66a` | 2026-10-11 | Order-book data survives restarts: per-fund prepared loads and the clock check cached on disk |
| 107 | `e184c5a` | 2026-10-11 | Optimizer: automatic parallelism uses every core |
| 108 | `f0bf83f` | 2026-10-11 | Optimizer ('hold' mode): one continuous run per configuration instead of one per block |
| 109 | `c28cab5` | 2026-10-11 | Compiled (numba) replay of the 'always invested' strategy for the optimizer and the placebo test |
| 110 | `7ff8760` | 2026-10-11 | Order-book datasets: prepare each fund's whole history once and slice it (no rebuild per warm-up start) |
| 111 | `b9dd78b` | 2026-10-11 | Optimizer: whole structural combinations per worker; fix in-memory data cache; cache diagnostics |
| 112 | `3783e6f` | 2026-10-11 | Order-book signals need double confirmation; backtest chart compares with buy & hold |
| 113 | `35e98cd` | 2026-10-11 | Settled parameters: new defaults, unticked in the comprehensive study |
| 114 | `3b8cc8c` | 2026-10-11 | Backtest trades: hover a buy / sell price to see the order book, signal and bubbles at that instant |
| 115 | `38eb55f` | 2026-10-11 | Optimizer workers share the prepared order-book data |
| 116 | `1b637b1` | 2026-10-11 | Volume (capacity) study button; disable form fields a dropdown choice makes meaningless |
| 117 | `3441526` | 2026-10-12 | Stale NAV: a fund is neither bought nor sold until its NAV is current; NAV age settled at 5 minutes |
| 118 | `4c8c76a` | 2026-10-12 | Optimizer coverage check: every backtest parameter is a study dimension with its default among the choices |
| 119 | `65329a2` | 2026-10-12 | NAV age is a fixed rule, not an optimizer parameter; never sell for nothing on a switch |
| 120 | `a039ebd` | 2026-10-12 | NAV age enforced at the instant of every trade; NAV age at buy / sale reported per trade |
| 121 | `69482d0` | 2026-10-12 | Signals confirmed at the average price of the whole order, not only the head of the queue |
| 122 | `2559c94` | 2026-10-12 | Partial entry at the limit price; a switch sells only what the buy needs |
| 123 | `49c1d1b` | 2026-10-12 | Minimum order 1/20 of a position; held funds are topped up in lots |
| 124 | `1b4153b` | 2026-10-13 | Backtest equity curve and drawdown at daily market value |
| 125 | `a1362dd` | 2026-10-13 | Audit fixes: data caches, NAV look-ahead, no-bid sales, benchmarks, optimizer stats |
| 126 | `c73fe3e` | 2026-10-13 | Execution audit fixes: daily volume budget, no double use of bids, delayed fills |
| 127 | `1c1f958` | 2026-10-13 | Fast cache freshness checks (backtest slowdown) |
| 128 | `4b889c8` | 2026-10-14 | Sales never beyond the visible bids; independent verifier |
| 129 | `76f50f5` | 2026-10-14 | Optimizer reliability: PBO, bootstrap significance, plateau pick, convergence |
| 130 | `7fcd717` | 2026-10-14 | NAV feed importer and dump check |
| 131 | `203bbcb` | 2026-10-14 | Independent verification in its own result tab |
| 132 | `cf5ca3c` | 2026-10-14 | Measure returns from the end of the warm-up; search the mean-reversion filter |
| 133 | `4f2f80a` | 2026-10-14 | NAV feed importer: read .csv.gz directly; keep only distinct NAV changes in memory |
| 134 | `d984dad` | 2026-10-14 | NAV feed importer: map funds by code_fa, stream 23M rows through a temp table, report gold-fund coverage |
| 135 | `6b6773e` | 2026-10-14 | Backtest NAVs from the fine-grained feed: the NAV received by each instant |
| 136 | `1f42c21` | 2026-10-14 | Market-rally filter: the mirror of the fall filter |
| 137 | `08cd503` | 2026-10-14 | NAV age in days is ignored while the minute limit is on |
| 138 | `d4ad916` | 2026-10-15 | Data folder survey tool: structure, statistics and NAV time-stamp behaviour of every file |
| 139 | `fe94543` | 2026-10-15 | Import the raw PostgreSQL intraday dump without PostgreSQL; NAV freshness report |
| 140 | `af3f9ea` | 2026-10-15 | Verifier: the fixed-grid order-book dataset (tse) with the NAV known at each grid instant |
| 141 | `8721c91` | 2026-10-15 | Signal predictive-power study; hover shows the signal vs the fund's own norm; toolbar groups; default period one year |
| 142 | `020f826` | 2026-10-15 | Signal study: executable switch gain (bid/ask + fees) decides whether a signal is tradable |
| 143 | `a262f88` | 2026-10-15 | Signal study: 10-day horizon; optimizer: minimum hold up to 5 and 7 days |
| 144 | `c73c3de` | 2026-10-15 | Optimizer: switch threshold up to 3 percentage points |
| 145 | `c551330` | 2026-10-15 | Optimizer that does not choke the laptop; headless runner for big studies |
| 146 | `5ff73a8` | 2026-10-16 | Re-check the settled parameters on the current data |
| 147 | `f4e8469` | 2026-10-16 | Optimizer: the time limit starts after the data is prepared; a timeout says so |
| 148 | `cac6d1d` | 2026-10-16 | Settled-parameter check: judge on paired runs, report no-effect parameters |
| 149 | `40f60fe` | 2026-10-16 | Optimizer: 'slow edge' preset (high switch threshold, multi-day hold) |
| 150 | `c3bedde` | 2026-10-17 | Optimizer, always-invested mode: no trade minimum, no entry/exit thresholds |
| 151 | `d85e0a0` | 2026-10-17 | Signal study: split by market regime |
| 152 | `8586cf2` | 2026-10-17 | Ready study request: concentration (position size x switch threshold) |
| 153 | `86f5379` | 2026-10-17 | Defaults = the settled configuration |
| 154 | `5b9f01d` | 2026-10-17 | Stray-order study: the order book replayed second by second |
