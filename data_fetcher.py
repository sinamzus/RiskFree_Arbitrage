"""Fetch real-time price and NAV data for fixed-income ETFs from TSETMC and FIPIRAN."""

import json
import re
import time
import logging
import threading
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from config import (
    TSETMC_CDN,
    FIPIRAN_WEB,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    FIXED_INCOME_ETFS,
    MIN_REQUEST_INTERVAL,
)

logger = logging.getLogger(__name__)

TSETMC_MAIN = "https://www.tsetmc.com"


class _RateLimiter:
    """Process-wide throttle that spaces request *initiations* in time.

    No matter how many worker threads call the API concurrently, this ensures
    at least ``min_interval`` seconds pass between the moments two requests are
    fired.  Network latency of in-flight requests still overlaps across threads
    (that's where the speed-up comes from), but the *rate* TSETMC sees is
    bounded — so adding workers never increases ban risk.

    The lock is intentionally held during the sleep so concurrent callers queue
    up and are released one ``min_interval`` apart (leaky-bucket behaviour).
    """

    def __init__(self, min_interval: float):
        self._lock = threading.Lock()
        self._min_interval = max(0.0, float(min_interval))
        self._next_at = 0.0

    def set_interval(self, seconds: float) -> None:
        with self._lock:
            self._min_interval = max(0.0, float(seconds))

    def acquire(self) -> None:
        with self._lock:
            if self._min_interval <= 0:
                return
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


def _try_parse_embedded_json(html: str):
    """Extract and parse the first useful JSON object from an HTML page (or raw JSON text).

    Tries in order:
    1. The entire text as JSON (the History endpoint sometimes returns JSON directly).
    2. The Next.js ``__NEXT_DATA__`` ``<script>`` block.
    3. Common global variable assignments (``window.__INITIAL_STATE__`` etc.).
    4. Any large JSON object found inside ``<script>`` tags.

    Returns the parsed Python object (dict or list), or None.
    """
    if not html:
        return None

    # 1. Raw JSON response (e.g. TSETMC returns JSON even for "HTML" URLs)
    stripped = html.strip()
    if stripped and stripped[0] in ('{', '['):
        try:
            return json.loads(stripped)
        except (ValueError, json.JSONDecodeError):
            pass

    # 2. Next.js __NEXT_DATA__
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m:
        try:
            return json.loads(m.group(1))
        except (ValueError, json.JSONDecodeError):
            pass

    # 3. window.* global assignments
    for pat in (
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;',
        r'window\.__STATE__\s*=\s*(\{.*?\})\s*;',
        r'window\.initialData\s*=\s*(\{.*?\})\s*;',
        r'var\s+pageData\s*=\s*(\{.*?\})\s*;',
    ):
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1))
            except (ValueError, json.JSONDecodeError):
                pass

    # 4. Any large JSON object in <script> tags
    for script_text in re.findall(
        r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE
    ):
        # Look for self-contained JSON objects with at least a few keys
        for json_match in re.findall(
            r'(\{(?:[^<>]|\{[^<>]*\}){30,}\})', script_text, re.DOTALL
        ):
            try:
                obj = json.loads(json_match)
                if isinstance(obj, dict) and len(obj) > 2:
                    return obj
            except (ValueError, json.JSONDecodeError):
                pass

    return None


def _normalize(text: str) -> str:
    """Normalize Arabic-script characters to Persian/ASCII equivalents.

    TSETMC stores fund names using Arabic letters (e.g. Arabic ya ي U+064A,
    Arabic kaf ك U+0643) while Python strings typically use the visually
    identical Persian codepoints (ya ی U+06CC, kaf ک U+06A9).
    Also normalizes Persian/Arabic-Indic numerals (۰-۹ / ٠-٩) to ASCII
    digits so that "اخزا۶" == "اخزا6" after normalization.
    """
    # Persian (Extended Arabic-Indic) numerals ۰-۹ → 0-9
    _FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    # Arabic-Indic numerals ٠-٩ → 0-9
    _AR = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return (
        text
        .replace("ي", "ی")  # Arabic ya → Persian ya
        .replace("ك", "ک")  # Arabic kaf → Persian kaf
        .replace("ة", "ه")  # ta marbuta → Persian he
        .translate(_FA)
        .translate(_AR)
        .strip()
    )


def classify_instrument(symbol: str, name: str) -> str:
    """Best-effort instrument-type classification from symbol + full name.

    Returns one of: option | bond | fund | right | stock | other.
    Heuristics (Iranian market conventions):
      • option (اختيار):  "اختيار" in name, or symbol starts with ض/ط
      • bond (اوراق بدهی): treasury/sukuk/lease/… keywords in name
      • fund (صندوق):      "صندوق" in name
      • right (حق تقدم):    "حق تقدم" in name, or symbol ends with "ح"
      • stock (سهام):       everything else (the default common-share case)
    """
    s = _normalize((symbol or "").strip())
    n = (name or "")
    if "اختيار" in n or "اختیار" in n or s[:1] in ("ض", "ط"):
        return "option"
    bond_kw = ("خزانه", "صکوک", "مشارکت", "اجاره", "مرابحه", "سلف",
               "اوراق", "گام", "رهنی", "منفعت")
    if any(k in n for k in bond_kw):
        return "bond"
    if "صندوق" in n:
        return "fund"
    if "حق تقدم" in n or (s.endswith("ح") and len(s) > 1):
        return "right"
    return "stock"


def _parse_jalali_date_int(digits: str) -> int:
    """Jalali date digits (YYYYMMDD or YYMMDD) → Gregorian YYYYMMDD int, else 0."""
    if not digits or not digits.isdigit():
        return 0
    try:
        import jdatetime
    except ImportError:
        return 0
    if len(digits) == 8:
        y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    elif len(digits) == 6:
        yy, m, d = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
        y = 1300 + yy if yy >= 60 else 1400 + yy
    else:
        return 0
    try:
        g = jdatetime.date(y, m, d).togregorian()
        return g.year * 10_000 + g.month * 100 + g.day
    except Exception:
        return 0


def parse_option_name(symbol: str, name: str) -> dict:
    """Parse an Iranian option instrument name into its contract spec.

    Returns {opt_type, underlying, strike, expiry} — opt_type call|put,
    underlying نمادِ پایه, strike (Rial), expiry Gregorian YYYYMMDD int.
    Missing fields are '' / 0. Falls back to the ض/ط symbol prefix for type.

    Typical name forms (after normalization):
        "اختيارخ اهرم-12000-14031213"
        "اختيارف خساپا-2000-1403/12/13"
    """
    import re
    # NOTE: _normalize maps Arabic ي→ی, so match the normalized (Persian) form.
    n = _normalize(name or "")
    s = _normalize(symbol or "")
    out = {"opt_type": "", "underlying": "", "strike": 0.0, "expiry": 0}

    if "اختیارخ" in n or "اختیار خ" in n or s[:1] == "ض":
        out["opt_type"] = "call"
    elif "اختیارف" in n or "اختیار ف" in n or s[:1] == "ط":
        out["opt_type"] = "put"

    m = re.search(r"اختیار\s*[خف]\s*", n)
    rest = n[m.end():].strip() if m else n
    parts = [p.strip() for p in rest.split("-") if p.strip()]
    if len(parts) >= 3:
        u = re.sub(r"^ت\s+", "", parts[0]).strip()   # drop ETF marker "ت "
        out["underlying"] = u.split()[0] if u else ""
        out["strike"] = float(re.sub(r"[^\d]", "", parts[1]) or 0)
        out["expiry"] = _parse_jalali_date_int(re.sub(r"[^\d]", "", parts[-1]))
    return out


# =========================================================================== #
#  TSETMC fetcher                                                              #
# =========================================================================== #

class TSETMCFetcher:
    """Fetches price and order-book data from cdn.tsetmc.com."""

    # Process-wide rate limiter, SHARED across every TSETMCFetcher instance so
    # the aggregate request rate stays bounded even when multiple fetchers /
    # threads run at once.  This is the core anti-ban guarantee.
    _rate_limiter = _RateLimiter(MIN_REQUEST_INTERVAL)

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = "https://www.tsetmc.com/"
        self.session.headers["Origin"]  = "https://www.tsetmc.com"
        # Read/written from multiple worker threads during parallel discovery;
        # plain dict get/set are atomic under CPython's GIL, and a lost race
        # only costs a redundant lookup — no corruption — so no lock needed.
        self._ins_code_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    #  Internal HTTP helper                                                #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, silent: bool = False,
             html: bool = False, retries: int = 3) -> Optional[dict | str]:
        """Fetch *url* with exponential-backoff retries.

        Returns parsed JSON dict by default.
        When *html=True* returns the raw response text (string).
        Returns None on any error after all retries are exhausted.
        """
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                # Global throttle — bounds the aggregate request rate so that
                # parallel fetching never exceeds a ban-safe req/s.
                self._rate_limiter.acquire()
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                if html:
                    return resp.text
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt < retries:
                    logger.debug(
                        "TSETMC request failed (attempt %d/%d) %s: %s — retrying in %.0fs",
                        attempt + 1, retries + 1, url, e, delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    if not silent:
                        logger.warning("TSETMC request failed for %s: %s", url, e)
                    return None
            except ValueError:
                if not silent:
                    logger.warning("Invalid JSON from %s", url)
                return None
        return None

    # ------------------------------------------------------------------ #
    #  Instrument discovery                                                #
    # ------------------------------------------------------------------ #

    def discover_ins_code(self, symbol: str, alt_symbols: list[str] = None) -> Optional[str]:
        """Search TSETMC for the instrument code of a fund symbol.

        **Fund-first priority**: TSETMC search often returns both a regular
        stock and an ETF fund with the same ticker (e.g. 'کیان' might hit a
        steel company AND the کیان fixed-income ETF).  We must pick the fund,
        not the stock.

        Priority order:
          P0 – exact ticker match AND lVal30 contains "صندوق"  ← best
          P1 – any result whose lVal30 contains "صندوق" + ("درآمد" or "ثابت")
          P2 – exact ticker match regardless of name  (last resort)

        Caches results to avoid redundant API calls.
        """
        candidates = [symbol] + (alt_symbols or [])

        for candidate in candidates:
            if candidate in self._ins_code_cache:
                return self._ins_code_cache[candidate]

            url = f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{quote(candidate)}"
            data = self._get(url)
            if not data:
                continue

            instruments = data.get("instrumentSearch", [])
            if not instruments:
                logger.debug("  TSETMC search for '%s': no results", candidate)
                continue

            logger.debug(
                "  TSETMC search for '%s': %d results → %s",
                candidate, len(instruments),
                [(i.get("lVal18AFC", "").strip(), i.get("lVal30", "")[:20])
                 for i in instruments[:6]],
            )

            norm_cand = _normalize(candidate)

            def _is_fund(inst: dict) -> bool:
                name = _normalize(inst.get("lVal30", ""))
                return "صندوق" in name

            def _is_fixed_income(inst: dict) -> bool:
                name = _normalize(inst.get("lVal30", ""))
                return _is_fund(inst) and ("درآمد" in name or "ثابت" in name)

            # P0: exact ticker + fund name
            for inst in instruments:
                if _normalize(inst.get("lVal18AFC", "")) == norm_cand and _is_fund(inst):
                    code = inst.get("insCode", "")
                    if code:
                        logger.info(
                            "  ✓ [P0] ins_code for '%s': %s (%s — %s)",
                            candidate, code,
                            inst.get("lVal18AFC", ""), inst.get("lVal30", ""),
                        )
                        self._ins_code_cache[symbol] = code
                        return code

            # P1: any fund (درآمد ثابت) in results
            for inst in instruments:
                if _is_fixed_income(inst):
                    code = inst.get("insCode", "")
                    if code:
                        logger.info(
                            "  ✓ [P1] ins_code for '%s' via fund-name: %s (%s — %s)",
                            candidate, code,
                            inst.get("lVal18AFC", ""), inst.get("lVal30", ""),
                        )
                        self._ins_code_cache[symbol] = code
                        return code

            # P2: exact ticker only (non-fund fallback — log a warning)
            for inst in instruments:
                if _normalize(inst.get("lVal18AFC", "")) == norm_cand:
                    code = inst.get("insCode", "")
                    if code:
                        full_name = inst.get("lVal30", "")
                        logger.warning(
                            "  ⚠ [P2] ins_code for '%s': %s (%s) — "
                            "name does NOT contain 'صندوق', may be wrong instrument!",
                            candidate, code, full_name,
                        )
                        self._ins_code_cache[symbol] = code
                        return code

        logger.warning(
            "  ✗ Could not find ins_code for '%s' (tried: %s)",
            symbol, candidates,
        )
        return None

    def discover_bond_ins_codes(self, keyword: str = "اخزا") -> dict[str, str]:
        """Search TSETMC for all debt instruments matching *keyword*.

        Unlike ``discover_ins_code`` (which filters for صندوق / ETF funds),
        this method returns every instrument whose symbol or name contains the
        keyword — suitable for اخزا (treasury bills), sukuk, etc.

        Returns
        -------
        dict mapping normalized_symbol → insCode for every match.
        """
        results = self.search_instrument(keyword)
        mapping: dict[str, str] = {}
        for r in results:
            code = r.get("ins_code", "").strip()
            sym  = _normalize(r.get("symbol", "").strip())
            if code and sym:
                mapping[sym] = code
                logger.info("  Bond discovery hit: %s → %s  (%s)", sym, code, r.get("full_name", ""))
        logger.info("discover_bond_ins_codes('%s'): %d results", keyword, len(mapping))
        return mapping

    def discover_akhza(self, queries: list[str] = None) -> list[dict]:
        """Discover ACTIVE اخزا treasury bills from TSETMC with real maturities.

        Runs several keyword searches (to beat TSETMC's ~40-result cap), keeps
        only genuine treasury bills (drops options/derivatives), parses each
        instrument's maturity from its name, and flags active vs matured.

        Returns a de-duplicated list of registry dicts:
            {symbol, ins_code, name, face_value, maturity_date(greg int),
             issue_date, coupon_rate, active(bool), verified=True}
        """
        from bonds import parse_akhza_maturity, is_akhza_treasury, _today_int, days_to_maturity

        # Budget-year prefixes cover the currently-tradeable universe; broad
        # "اخزا" catches the rest. Searches are cheap (rate-limited) and merged.
        queries = queries or ["اخزا", "اخزا4", "اخزا3", "اخزا2", "اخزا1", "اخزا0"]
        today = _today_int()

        by_code: dict[str, dict] = {}
        for q in queries:
            for r in self.search_instrument(q):
                code = (r.get("ins_code") or "").strip()
                sym  = (r.get("symbol") or "").strip()
                name = (r.get("full_name") or "").strip()
                if not code or not sym or code in by_code:
                    continue
                if not is_akhza_treasury(sym, name):
                    continue
                mat = parse_akhza_maturity(name)
                active = bool(mat and days_to_maturity(mat, today) > 0)
                by_code[code] = {
                    "symbol": sym, "ins_code": code, "name": name,
                    "face_value": 1_000_000, "maturity_date": mat or 0,
                    "issue_date": 0, "coupon_rate": 0.0,
                    "active": active, "verified": True,
                }

        out = sorted(by_code.values(), key=lambda d: d["maturity_date"] or 99999999)
        n_active = sum(1 for d in out if d["active"])
        logger.info("discover_akhza: %d treasury bills (%d active) from %d queries",
                    len(out), n_active, len(queries))
        return out

    # ------------------------------------------------------------------ #
    #  Market watch — enumerate ALL بورس/فرابورس instruments              #
    # ------------------------------------------------------------------ #

    # TSETMC `flow` (market) codes seen in the market-watch payload.
    _FLOW_MARKET = {1: "bourse", 2: "farabourse", 3: "other", 4: "other",
                    5: "other", 6: "other", 7: "paye"}

    def get_market_watch(self) -> list[dict]:
        """Snapshot EVERY tradeable instrument from the TSETMC market watch.

        Endpoint: ``ClosingPrice/GetMarketWatch`` — one row per instrument with
        its code, symbol, name and market flow.  This is the only TSETMC call
        that enumerates the whole بورس + فرابورس universe in one shot.

        Returns a list of dicts:
            {ins_code, symbol, name, market, board, type, base_volume}
        ``type`` is classified heuristically (stock / fund / bond / right /
        option / other) from the symbol+name.  Returns [] on failure.

        NOTE: the market-watch JSON field names are parsed defensively (several
        fallbacks) because TSETMC occasionally renames them; if a future change
        breaks parsing, the per-row counts logged here make it obvious.
        """
        # Try a few known parameter encodings — TSETMC's GetMarketWatch has
        # required refID/hEven params and the paperTypes bracket form varies.
        variants = [
            (f"{TSETMC_CDN}/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
             "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
             "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
             "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
             "&showTraded=false&withBestLimits=false&hEven=0&refID=0"),
            (f"{TSETMC_CDN}/ClosingPrice/GetMarketWatch?market=0"
             "&paperTypes[0]=1&paperTypes[1]=2&paperTypes[2]=3&paperTypes[3]=4"
             "&showTraded=false&withBestLimits=false&hEven=0&refID=0"),
        ]
        data = None
        for url in variants:
            data = self._get(url, silent=True)
            if data and (data.get("marketwatch") or data.get("marketWatch")
                         or data.get("MarketWatch")):
                break
        if not data:
            logger.warning("get_market_watch: endpoint returned no JSON "
                           "(blocked/changed). Use per-symbol --watch instead.")
            return []
        rows = (data.get("marketwatch") or data.get("marketWatch")
                or data.get("MarketWatch") or [])
        if not rows:
            logger.warning("get_market_watch: JSON had no marketwatch list; "
                           "top-level keys=%s", list(data.keys()))
        out: list[dict] = []
        for r in rows:
            code = str(r.get("insCode") or r.get("InsCode") or "").strip()
            sym  = (r.get("lva") or r.get("lVal18AFC") or r.get("symbol") or "").strip()
            name = (r.get("lvc") or r.get("lVal30") or r.get("name") or "").strip()
            if not code or not sym:
                continue
            flow = r.get("flow", r.get("Flow", 0))
            try:
                flow = int(flow)
            except (TypeError, ValueError):
                flow = 0
            market = self._FLOW_MARKET.get(flow, "other")
            base_vol = r.get("bv") or r.get("baseVol") or r.get("baseVolume") or 0
            try:
                base_vol = int(base_vol)
            except (TypeError, ValueError):
                base_vol = 0
            out.append({
                "ins_code": code, "symbol": _normalize(sym), "name": name,
                "market": market, "board": str(r.get("cs", "") or ""),
                "type": classify_instrument(sym, name),
                "base_volume": base_vol,
            })
        logger.info("get_market_watch: %d instruments", len(out))
        return out

    def discover_stocks(self) -> list[dict]:
        """Convenience: market-watch filtered to common stocks (سهام) on the
        main بورس and فرابورس boards (drops funds, bonds, rights, options)."""
        rows = self.get_market_watch()
        stocks = [r for r in rows
                  if r["type"] == "stock" and r["market"] in ("bourse", "farabourse")]
        logger.info("discover_stocks: %d common stocks (of %d instruments)",
                    len(stocks), len(rows))
        return stocks

    def discover_options(self, underlyings: list[str] | None = None) -> list[dict]:
        """Discover اختيار معامله (option) contracts from TSETMC.

        Runs a broad "اختيار" search plus, for each underlying in *underlyings*,
        an "اختيار <نماد>" search to pull that full chain (search is ~40-capped,
        so per-underlying queries are how you get a complete chain). Parses each
        contract's type/strike/expiry/underlying from the name.

        Returns de-duplicated registry dicts:
            {symbol, ins_code, name, opt_type, underlying, strike, expiry,
             contract_size, active}
        """
        from datetime import datetime as _dt
        today = int(_dt.now().strftime("%Y%m%d"))
        queries = ["اختيار"]
        if underlyings:
            queries += [f"اختيار {u}" for u in underlyings]
        by_code: dict[str, dict] = {}
        for q in queries:
            for r in self.search_instrument(q):
                code = (r.get("ins_code") or "").strip()
                sym = (r.get("symbol") or "").strip()
                name = (r.get("full_name") or "").strip()
                if not code or not sym or code in by_code:
                    continue
                if classify_instrument(sym, name) != "option":
                    continue
                spec = parse_option_name(sym, name)
                by_code[code] = {
                    "symbol": _normalize(sym), "ins_code": code, "name": name,
                    "opt_type": spec["opt_type"], "underlying": spec["underlying"],
                    "strike": spec["strike"], "expiry": spec["expiry"],
                    "contract_size": 1000,
                    "active": bool(spec["expiry"] and spec["expiry"] > today),
                }
        out = sorted(by_code.values(),
                     key=lambda d: (d["underlying"], d["expiry"] or 0, d["strike"]))
        n_act = sum(1 for d in out if d["active"])
        logger.info("discover_options: %d contracts (%d active) from %d queries",
                    len(out), n_act, len(queries))
        return out

    # ------------------------------------------------------------------ #
    #  Price data                                                          #
    # ------------------------------------------------------------------ #

    def get_closing_price_info(self, ins_code: str) -> Optional[dict]:
        url = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}"
        data = self._get(url)
        if not data or "closingPriceInfo" not in data:
            return None

        info = data["closingPriceInfo"]
        result = {
            "last_price":      info.get("pDrCotVal", 0),
            "close_price":     info.get("pClosing", 0),
            "open_price":      info.get("priceFirst", 0),
            "high_price":      info.get("priceMax", 0),
            "low_price":       info.get("priceMin", 0),
            "yesterday_price": info.get("priceYesterday", 0),
            "volume":          info.get("qTotTran5J", 0),
            "value":           info.get("qTotCap", 0),
            "trade_count":     info.get("zTotTran", 0),
        }

        # Log top-level response keys and sub-object non-zero fields at DEBUG
        logger.debug("ClosingPriceInfo top-level keys for %s: %s",
                     ins_code, list(data.keys()))
        logger.debug("ClosingPriceInfo fields for %s: %s", ins_code,
                     {k: v for k, v in info.items()
                      if not k.startswith("_") and v not in (None, 0, "", [])})

        # Check if NAV happens to be embedded (field may vary by instrument type)
        for nav_field in ("navStat", "nav", "statisticalNav", "cancelNav",
                          "navValue", "psGelStaMax", "pDrCotValStat",
                          "staticNav", "navPerUnit"):
            nav = info.get(nav_field)
            if nav and isinstance(nav, (int, float)) and nav > 0:
                result["embedded_nav"] = float(nav)
                logger.info("  Embedded NAV in ClosingPriceInfo[%s] = %s for %s",
                            nav_field, nav, ins_code)
                break

        return result

    # ------------------------------------------------------------------ #
    #  NAV data                                                            #
    # ------------------------------------------------------------------ #

    def get_fund_nav(self, ins_code: str) -> Optional[dict]:
        """Derive NAV for *ins_code* from TSETMC CDN endpoints.

        Analysis of the live TSETMC API (2026-05-24) revealed:
        - GetClosingPriceHistory  → returns empty array for ETF funds
        - /History/{insCode}/…    → returns 824-byte React SPA shell
        - ETF/* CDN paths         → return 824-byte React SPA shell (don't exist)
        - GetInstrumentInfo       → works; contains staticThreshold with
                                    psGelStaMax and psGelStaMin

        For Iranian ETFs the Exchange anchors the daily static price band to
        the fund's published NAV.  Therefore:

            NAV = (psGelStaMax + psGelStaMin) / 2

        For كمند: (10456 + 9848) / 2 = 10152, matching priceYesterday exactly.
        For a fixed-income ETF (annual yield ~20-25%) the daily NAV drift is
        ~0.07%, well below our 0.3% arbitrage threshold — so yesterday's NAV
        is close enough to today's.

        Strategies (in order):
        1. InstrumentInfo → staticThreshold midpoint  (primary — always works)
        2. Old Loader.aspx pipe-delimited API          (secondary check)
        """

        # ── 1. GetInstrumentInfo → staticThreshold midpoint ──────────────
        nav = self._nav_from_instrument_info(ins_code)
        if nav:
            return nav

        # ── 2. Old TSETMC Loader API ──────────────────────────────────────
        nav = self._nav_from_loader(ins_code)
        if nav:
            return nav

        logger.warning("  No NAV found for ins_code=%s", ins_code)
        return None

    def _nav_from_instrument_info(self, ins_code: str) -> Optional[dict]:
        """GET GetInstrumentInfo and derive NAV from staticThreshold midpoint.

        TSETMC sets the daily price band (psGelStaMin / psGelStaMax) as:
            psGelStaMax = NAV × (1 + band_pct)
            psGelStaMin = NAV × (1 - band_pct)
        → midpoint = NAV  (exact, independent of band_pct)

        Additional fields harvested:
        - etfIssuedUnit : total units outstanding
        - etfUnitDeven  : date of last NAV publication (YYYYMMDD int)
        - faraDesc       : human-readable fund type description
        """
        url  = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
        data = self._get(url, silent=True)
        if not data:
            return None

        info = data.get("instrumentInfo") or {}
        if not isinstance(info, dict):
            return None

        # ── a) staticThreshold midpoint ──────────────────────────────────
        st      = info.get("staticThreshold") or {}
        sta_max = st.get("psGelStaMax") or 0
        sta_min = st.get("psGelStaMin") or 0

        if sta_max > 0 and sta_min > 0:
            nav_val  = (sta_max + sta_min) / 2.0
            # etfUnitDeven is the date of last NAV calculation (YYYYMMDD int)
            raw_date = info.get("etfUnitDeven") or info.get("dEven") or ""
            nav_date = str(raw_date)
            total_units = info.get("etfIssuedUnit") or 0
            logger.info(
                "  NAV from staticThreshold midpoint = %.2f "
                "(band %.2f–%.2f, dEven=%s) for %s",
                nav_val, sta_min, sta_max, nav_date, ins_code,
            )
            result = self._build_nav(nav_val, nav_val, nav_val,
                                     nav_date, "TSETMC/StaticThreshold-midpoint")
            result["fund_units"] = total_units
            result["total_nav"]  = nav_val * total_units if total_units else 0
            return result

        # ── b) Fallback: check top-level info dict for any explicit nav field ─
        for nav_key in ("navStat", "nav", "statisticalNav", "cancelNav",
                        "navPerUnit", "pNavStat"):
            nav_val = info.get(nav_key)
            if nav_val and isinstance(nav_val, (int, float)) and nav_val > 0:
                logger.info("  NAV from InstrumentInfo[%s] = %s for %s",
                            nav_key, nav_val, ins_code)
                return self._build_nav(
                    nav_val,
                    info.get("cancelNav", nav_val),
                    info.get("issueNav",  nav_val),
                    str(info.get("dEven", "")),
                    "TSETMC/InstrumentInfo",
                )

        logger.debug("  InstrumentInfo: no staticThreshold or nav field for %s",
                     ins_code)
        return None

    def _nav_from_loader(self, ins_code: str) -> Optional[dict]:
        """Try the old TSETMC Loader API (ParTree=15131W) for NAV data.

        This legacy TseClient API returns pipe-delimited instrument data and
        historically included the statistical NAV for ETF/fund instruments.
        URL: https://www.tsetmc.com/Loader.aspx?ParTree=15131W&i={insCode}
        """
        url = f"{TSETMC_MAIN}/Loader.aspx?ParTree=15131W&i={ins_code}"
        text = self._get(url, silent=True, html=True)
        if not text:
            return None
        text = text.strip()
        logger.debug("Loader API for %s: %d bytes, preview: %s",
                     ins_code, len(text), text[:100].replace("\n", " "))

        # Format: semi-colon separated sections, each section has @-separated fields
        # Known position for navStat varies; try JSON first in case it changed format
        try:
            obj = json.loads(text)
            nav = self._extract_nav_from_json(obj, ins_code)
            if nav:
                logger.info("  NAV from Loader API (JSON) for %s", ins_code)
                return nav
        except Exception:
            pass

        # Pipe/semi-colon delimited: look for any 6-10 digit number that could be NAV
        # ETF fund NAV in Iran is typically 1,000,000 – 99,999,999 range (Rial)
        # Section 3 (index 2) or 4 in the old TseClient format contains price/nav data
        parts = re.split(r'[;@,\|]', text)
        # Fixed-income ETF NAV per unit is in the same range as market price.
        # كمند trades ~10,000-11,000 Rial, so NAV is in that range.
        # Accept 4-10 digit numbers (1,000 – 9,999,999,999 Rial).
        nav_candidates = []
        for p in parts:
            p = p.strip()
            if re.match(r'^\d{4,10}$', p):
                nav_candidates.append(float(p))
        if nav_candidates:
            # Filter: typical Iranian fixed-income ETF NAV per unit: 1,000 – 500,000 Rial
            plausible = [v for v in nav_candidates if 1_000 <= v <= 500_000]
            if plausible:
                nav_val = sorted(plausible)[len(plausible)//2]  # median
                logger.info("  NAV from Loader API (pipe-parse) = %s for %s",
                            nav_val, ins_code)
                return self._build_nav(nav_val, nav_val, nav_val,
                                       "", "TSETMC/Loader")
        return None

    @staticmethod
    def _extract_nav_from_json(obj, ins_code: str) -> Optional[dict]:
        """Recursively search a parsed JSON structure for NAV fields."""
        if isinstance(obj, dict):
            # Check this dict for nav keys
            for nav_key in ("navStat", "cancelNav", "cancelNAV",
                            "statisticalNav", "nav", "navPerUnit"):
                nav_val = obj.get(nav_key)
                if nav_val and isinstance(nav_val, (int, float, str)):
                    try:
                        v = float(str(nav_val).replace(",", ""))
                        if v > 100:
                            return TSETMCFetcher._build_nav(
                                v,
                                float(str(obj.get("cancelNav",
                                                   obj.get("cancelNAV", v)
                                                   )).replace(",", "") or v),
                                float(str(obj.get("issueNav",
                                                   obj.get("issueNAV",  v)
                                                   )).replace(",", "") or v),
                                str(obj.get("navDate", obj.get("dEven", ""))),
                                "TSETMC/JSON",
                            )
                    except (ValueError, TypeError):
                        pass
            # Recurse into values
            for v in obj.values():
                result = TSETMCFetcher._extract_nav_from_json(v, ins_code)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = TSETMCFetcher._extract_nav_from_json(item, ins_code)
                if result:
                    return result
        return None

    @staticmethod
    def _build_nav(stat, cancel, issue, date, source):
        return {
            "nav_per_unit":    float(cancel),
            "statistical_nav": float(stat),
            "issue_nav":       float(issue),
            "cancel_nav":      float(cancel),
            "total_nav":       0,
            "fund_units":      0,
            "nav_date":        date,
            "source":          source,
        }

    # ------------------------------------------------------------------ #
    #  Historical daily OHLCV                                             #
    # ------------------------------------------------------------------ #

    def get_historical_daily(self, ins_code: str, days: int = 365) -> list[dict]:
        """Fetch up to *days* days of daily OHLCV for *ins_code*.

        Endpoint: ``ClosingPrice/GetClosingPriceDailyList/{insCode}/{n}``

        For fixed-income ETFs ``priceYesterday`` in each entry approximates
        the fund's published NAV for that date:
            premium_pct = (pClosing - priceYesterday) / priceYesterday × 100

        Returns a list of dicts sorted **ascending** by date (oldest first).
        Each dict has keys:
            date (YYYYMMDD int), open_price, high_price, low_price,
            close_price, yesterday_price, volume, value, trade_count,
            price_change
        """
        url  = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceDailyList/{ins_code}/{days}"
        data = self._get(url, silent=True)
        if not data:
            logger.debug("get_historical_daily: no data for %s (n=%d)", ins_code, days)
            return []
        items = data.get("closingPriceDaily") or []
        result = []
        for e in items:
            d = e.get("dEven")
            if not d:
                continue
            yesterday = e.get("priceYesterday", 0)
            close     = e.get("pClosing", 0)
            premium   = ((close - yesterday) / yesterday * 100.0
                         if yesterday > 0 else 0.0)
            result.append({
                "date":            int(d),
                "open_price":      e.get("priceFirst", 0),
                "high_price":      e.get("priceMax", 0),
                "low_price":       e.get("priceMin", 0),
                "close_price":     close,
                "yesterday_price": yesterday,   # ≈ NAV for ETFs
                "volume":          e.get("qTotTran5J", 0),
                "value":           e.get("qTotCap", 0),
                "trade_count":     e.get("zTotTran", 0),
                "price_change":    e.get("priceChange", 0),
                "premium_pct":     round(premium, 4),
            })
        result.sort(key=lambda x: x["date"])
        logger.debug("get_historical_daily: %d entries for %s", len(result), ins_code)
        return result

    # ------------------------------------------------------------------ #
    #  Intraday tick trades                                                #
    # ------------------------------------------------------------------ #

    def get_intraday_trades(self, ins_code: str, date_int: int) -> list[dict]:
        """Fetch all intraday tick trades for *ins_code* on *date_int* (YYYYMMDD).

        Endpoint: ``Trade/GetTradeHistory/{insCode}/{YYYYMMDD}/false``

        Returns a list of dicts sorted **ascending** by sequence number.
        Each dict has keys:
            seq (int), time (HHMMSS int), price, volume, canceled (0|1)

        Time encoding: 91530 → 09:15:30, 130000 → 13:00:00.
        """
        url  = f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins_code}/{date_int}/false"
        data = self._get(url, silent=True)
        if not data:
            logger.debug("get_intraday_trades: no data for %s on %s", ins_code, date_int)
            return []
        trades = data.get("tradeHistory") or []
        result = []
        for t in trades:
            result.append({
                "seq":      t.get("nTran", 0),
                "time":     t.get("hEven", 0),      # HHMMSS as int
                "price":    t.get("pTran", 0),
                "volume":   t.get("qTitTran", 0),
                "canceled": 1 if t.get("canceled") else 0,
            })
        result.sort(key=lambda x: x["seq"])
        logger.debug(
            "get_intraday_trades: %d ticks for %s on %s", len(result), ins_code, date_int
        )
        return result

    # ------------------------------------------------------------------ #
    #  Intraday price snapshots (historical, ≈ per-minute aggregates)      #
    # ------------------------------------------------------------------ #

    def get_intraday_price_history(self, ins_code: str, date_int: int) -> list[dict]:
        """Fetch intraday price/volume snapshots for *date_int* (YYYYMMDD).

        Endpoint: ``ClosingPrice/GetClosingPriceHistory/{insCode}/{YYYYMMDD}``

        Returns ~3000-6000 rows per trading day — one snapshot every few seconds
        capturing cumulative state at that instant.  Fields per row:
            time (HHMMSS int), last_price, close_price, trade_count,
            cum_volume, cum_value

        Sorted ascending by time.  Returns [] if no data.
        """
        url  = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceHistory/{ins_code}/{date_int}"
        data = self._get(url, silent=True)
        if not data:
            return []
        rows = data.get("closingPriceHistory") or []
        out = []
        for r in rows:
            t = r.get("hEven", 0)
            if not t:
                continue
            out.append({
                "time":        t,
                "last_price":  r.get("pDrCotVal", 0),
                "close_price": r.get("pClosing",  0),
                "trade_count": int(r.get("zTotTran",   0) or 0),
                "cum_volume":  int(r.get("qTotTran5J", 0) or 0),
                "cum_value":   r.get("qTotCap", 0),
            })
        out.sort(key=lambda x: x["time"])
        logger.debug("get_intraday_price_history: %d snapshots for %s on %s",
                     len(out), ins_code, date_int)
        return out

    def get_today_intraday_bars(self, ins_code: str) -> list[dict]:
        """Fetch today's per-minute OHLCV bars (only available for TODAY).

        Endpoint: ``Trade/GetTradeIntraday/{insCode}``  (no date param)

        Returns list of {time, open, high, low, close, volume} sorted by time.
        """
        url  = f"{TSETMC_CDN}/Trade/GetTradeIntraday/{ins_code}"
        data = self._get(url, silent=True)
        if not data:
            return []
        rows = data.get("tradeIntraDay") or []
        bars = [{
            "time":   r.get("hEven", 0),
            "open":   r.get("openPrice",  0),
            "high":   r.get("maxPrice",   0),
            "low":    r.get("minPrice",   0),
            "close":  r.get("closePrice", 0),
            "volume": int(r.get("volume", 0) or 0),
        } for r in rows if r.get("hEven")]
        bars.sort(key=lambda x: x["time"])
        return bars

    def get_today_trades(self, ins_code: str) -> list[dict]:
        """Fetch today's tick-by-tick trades (live, accumulating since open).

        Endpoint: ``Trade/GetTrade/{insCode}``

        Returns list of {seq, time, price, volume, canceled} sorted by seq.
        """
        url  = f"{TSETMC_CDN}/Trade/GetTrade/{ins_code}"
        data = self._get(url, silent=True)
        if not data:
            return []
        trades = data.get("trade") or []
        out = [{
            "seq":      t.get("nTran",    0),
            "time":     t.get("hEven",    0),
            "price":    t.get("pTran",    0),
            "volume":   int(t.get("qTitTran", 0) or 0),
            "canceled": 1 if t.get("canceled") else 0,
        } for t in trades]
        out.sort(key=lambda x: x["seq"])
        return out

    # ------------------------------------------------------------------ #
    #  Client type (Individual vs Legal) breakdown                         #
    # ------------------------------------------------------------------ #

    def get_client_type(self, ins_code: str, date_int: int) -> Optional[dict]:
        """Fetch buy/sell volume split between individuals (حقیقی) and
        legal entities (حقوقی) for *date_int*.

        Endpoint: ``ClientType/GetClientTypeHistory/{insCode}/{YYYYMMDD}``

        Returns dict with keys:
            date, buy_i_vol, buy_n_vol, buy_i_val, buy_n_val,
            buy_i_cnt, buy_n_cnt, sell_i_vol, sell_n_vol,
            sell_i_val, sell_n_val, sell_i_cnt, sell_n_cnt
        I = حقیقی (individual), N = حقوقی (legal entity).
        Returns None for non-trading days (API returns HTTP 500).
        """
        url  = f"{TSETMC_CDN}/ClientType/GetClientTypeHistory/{ins_code}/{date_int}"
        data = self._get(url, silent=True)
        if not data:
            return None
        ct = data.get("clientType") if isinstance(data, dict) else None
        if not isinstance(ct, dict):
            return None
        return {
            "date":       ct.get("recDate", date_int),
            "buy_i_vol":  int(ct.get("buy_I_Volume",  0) or 0),
            "buy_n_vol":  int(ct.get("buy_N_Volume",  0) or 0),
            "buy_i_val":  ct.get("buy_I_Value",   0) or 0,
            "buy_n_val":  ct.get("buy_N_Value",   0) or 0,
            "buy_i_cnt":  int(ct.get("buy_I_Count",  0) or 0),
            "buy_n_cnt":  int(ct.get("buy_N_Count",  0) or 0),
            "sell_i_vol": int(ct.get("sell_I_Volume", 0) or 0),
            "sell_n_vol": int(ct.get("sell_N_Volume", 0) or 0),
            "sell_i_val": ct.get("sell_I_Value",  0) or 0,
            "sell_n_val": ct.get("sell_N_Value",  0) or 0,
            "sell_i_cnt": int(ct.get("sell_I_Count", 0) or 0),
            "sell_n_cnt": int(ct.get("sell_N_Count", 0) or 0),
        }

    # ------------------------------------------------------------------ #
    #  Order book                                                          #
    # ------------------------------------------------------------------ #

    def get_best_limits(self, ins_code: str) -> Optional[dict]:
        data = self._get(f"{TSETMC_CDN}/BestLimits/{ins_code}")
        if not data or "bestLimits" not in data:
            return None

        bids, asks = [], []
        for row in data["bestLimits"]:
            if row.get("number", 0) <= 5:
                bids.append({"price": row.get("pMeDem", 0),
                             "volume": row.get("qTitMeDem", 0),
                             "count":  row.get("zOrdMeDem", 0)})
                asks.append({"price": row.get("pMeOf", 0),
                             "volume": row.get("qTitMeOf", 0),
                             "count":  row.get("zOrdMeOf", 0)})
        return {"bids": bids, "asks": asks}

    def get_best_limits_history(self, ins_code: str, date_int: int) -> list[dict]:
        """Fetch historical order-book deltas and reconstruct minute-level snapshots.

        TSETMC `bestLimitsHistory` is a **stream of per-level delta updates**,
        not full snapshots.  Each row updates exactly one bid+ask level (number 1-5);
        `refID` is a monotonically increasing global event-sequence number.

        Fields per row:
          number     – level (1=best … 5=worst)
          hEven      – HHMMSS time of this change (int, no leading zero)
          refID      – monotonic event ID (use for sort order)
          pMeDem     – bid price    qTitMeDem – bid volume   zOrdMeDem – bid count
          pMeOf      – ask price    qTitMeOf  – ask volume   zOrdMeOf  – ask count

        Algorithm:
          1. Sort all rows by refID (true chronological order — hEven collisions exist)
          2. Replay deltas, maintaining a running 5-level book state
          3. Emit one snapshot whenever the minute changes (≈ 360-400 snapshots/day)

        Returns list[{time: HHMMSS, bids: [5 levels], asks: [5 levels]}]
        sorted ascending by time.  Returns [] if the endpoint is unavailable.
        """
        data = self._get(
            f"{TSETMC_CDN}/BestLimits/{ins_code}/{date_int}", silent=True
        )
        if not data:
            return []

        rows = data.get("bestLimitsHistory") or []
        if not rows:
            return []

        # Sort by the global event-sequence ID (refID), not by hEven
        rows_sorted = sorted(rows, key=lambda r: r.get("refID", 0))

        # Running book state: level_number (1-5) → {bid_*, ask_*}
        current: dict[int, dict] = {}
        result: list[dict] = []
        last_minute = -1

        for row in rows_sorted:
            level = row.get("number", 0)
            if not (1 <= level <= 5):
                continue

            current[level] = {
                "bid_price": row.get("pMeDem",    0),
                "bid_vol":   row.get("qTitMeDem", 0),
                "bid_cnt":   row.get("zOrdMeDem", 0),
                "ask_price": row.get("pMeOf",     0),
                "ask_vol":   row.get("qTitMeOf",  0),
                "ask_cnt":   row.get("zOrdMeOf",  0),
            }

            # Wait until all 5 levels are populated before emitting snapshots
            if len(current) < 5:
                continue

            # Sample once per minute (hEven is HHMMSS stored as int)
            t = row.get("hEven", 0)
            t_str = str(t).zfill(6)
            minute = int(t_str[:4])   # HHMM as int

            if minute != last_minute:
                bids = [
                    {"price": current[lvl]["bid_price"],
                     "volume": current[lvl]["bid_vol"],
                     "count":  current[lvl]["bid_cnt"]}
                    for lvl in sorted(current)
                ]
                asks = [
                    {"price": current[lvl]["ask_price"],
                     "volume": current[lvl]["ask_vol"],
                     "count":  current[lvl]["ask_cnt"]}
                    for lvl in sorted(current)
                ]
                # Best bid = highest price; best ask = lowest price
                bids.sort(key=lambda x: -x["price"])
                asks.sort(key=lambda x:  x["price"])

                result.append({"time": t, "bids": bids, "asks": asks})
                last_minute = minute

        return result

    # ------------------------------------------------------------------ #
    #  Search (discover mode)                                              #
    # ------------------------------------------------------------------ #

    def search_instrument(self, keyword: str) -> list[dict]:
        data = self._get(f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{quote(keyword)}")
        if not data or "instrumentSearch" not in data:
            return []
        return [{"ins_code": i.get("insCode", ""),
                 "symbol":   i.get("lVal18AFC", "").strip(),
                 "full_name": i.get("lVal30", "").strip()}
                for i in data["instrumentSearch"]]

    # ------------------------------------------------------------------ #
    #  Main fetch loop                                                     #
    # ------------------------------------------------------------------ #

    def _fetch_one_fund(self, fund: dict) -> dict:
        """Fetch price + NAV + order book for a single fund.

        Self-contained so it can run inside a worker thread.  Inter-request
        spacing is handled globally by ``_rate_limiter`` inside ``_get``, so
        no per-call ``time.sleep`` is needed here.
        """
        symbol      = fund["symbol"]
        ins_code    = fund.get("ins_code", "").strip()
        alt_symbols = fund.get("alt_symbols", [])

        entry = {
            "symbol":     symbol,
            "name":       fund["name"],
            "ins_code":   ins_code,
            "price_data": None,
            "nav_data":   None,
            "order_book": None,
            "error":      None,
        }

        # Ensure we have a valid ins_code
        if not ins_code:
            ins_code = self.discover_ins_code(symbol, alt_symbols) or ""
            entry["ins_code"] = ins_code

        # Fetch price (retry with discovery if stored code fails)
        price, ins_code = self._fetch_price_with_fallback(
            symbol, ins_code, alt_symbols
        )
        entry["ins_code"]   = ins_code
        entry["price_data"] = price
        if price is None:
            entry["error"] = "قیمت دریافت نشد"

        # Fetch NAV from TSETMC (multiple strategies)
        nav = self.get_fund_nav(ins_code) if ins_code else None
        if nav is None and price and price.get("embedded_nav"):
            embedded = price["embedded_nav"]
            nav = self._build_nav(embedded, embedded, embedded,
                                  "", "TSETMC/embedded")
        entry["nav_data"] = nav
        if nav is None:
            entry["error"] = (entry["error"] + "; NAV دریافت نشد"
                              if entry["error"] else "NAV دریافت نشد")

        # Fetch order book
        if ins_code:
            entry["order_book"] = self.get_best_limits(ins_code)

        return entry

    def fetch_all_fund_data(self, funds: list[dict] = None,
                            delay: float = 0.5,
                            workers: int = None) -> list[dict]:
        """Fetch data for every fund, concurrently.

        Funds are fetched in parallel across a bounded thread pool to overlap
        network latency.  The global ``_rate_limiter`` (see ``_get``) keeps the
        aggregate request rate ban-safe regardless of *workers*, so this is
        much faster than the old sequential loop without raising ban risk.

        Results preserve the input order of *funds*.
        """
        from concurrent.futures import ThreadPoolExecutor
        from config import FETCH_WORKERS

        if funds is None:
            funds = FIXED_INCOME_ETFS
        workers = max(1, workers or FETCH_WORKERS)

        logger.info("Fetching %d funds with %d workers ...", len(funds), workers)
        results: list[Optional[dict]] = [None] * len(funds)

        def _job(idx: int, fund: dict):
            try:
                results[idx] = self._fetch_one_fund(fund)
            except Exception as e:                       # never let one fund kill the batch
                logger.warning("Fetch failed for %s: %s", fund.get("symbol"), e)
                results[idx] = {
                    "symbol": fund.get("symbol", ""), "name": fund.get("name", ""),
                    "ins_code": fund.get("ins_code", ""), "price_data": None,
                    "nav_data": None, "order_book": None, "error": str(e),
                }

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, fund in enumerate(funds):
                ex.submit(_job, i, fund)

        return [r for r in results if r is not None]

    def _fetch_price_with_fallback(self, symbol: str, ins_code: str,
                                   alt_symbols: list[str]) -> tuple[Optional[dict], str]:
        """Return (price_dict, effective_ins_code).

        Tries stored ins_code first; if that causes a 500, discovers a new one.
        """
        if ins_code:
            price = self.get_closing_price_info(ins_code)
            if price is not None:
                return price, ins_code
            logger.info("  Stored ins_code failed — searching TSETMC for '%s'...", symbol)

        new_code = self.discover_ins_code(symbol, alt_symbols) or ""
        if new_code and new_code != ins_code:
            price = self.get_closing_price_info(new_code)
            if price is not None:
                return price, new_code

        return None, ins_code or new_code


# =========================================================================== #
#  FIPIRAN fetcher                                                             #
# =========================================================================== #

class FIPIRANFetcher:
    """Fetches fund NAV data from www.fipiran.ir.

    Strategy (in order):
    1. GET /DataService/FundCompare with AJAX headers → try JSON parse
    2. If response is HTML → parse embedded JSON from <script> tags
    3. If not found → parse HTML <table> with BeautifulSoup
    4. Fallback URL: /Fund/MFBourse (ETF-specific page)
    """

    # FIPIRAN is a React/NextJS SPA — HTML endpoints return only the app shell.
    # Data comes from JSON API routes; we try several known paths.
    _API_ENDPOINTS = [
        FIPIRAN_WEB + "/api/v1/fund/fundlist",      # returns 403 without session
        "https://fipiran.ir/api/v1/fund/fundlist",   # bare domain
        FIPIRAN_WEB + "/api/fund/fundcompare",
        FIPIRAN_WEB + "/api/v1/fund/fundcompare",
        "https://fipiran.ir/api/v1/fund/fundcompare",
    ]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = FIPIRAN_WEB + "/"
        self._session_established = False

    def _establish_session(self):
        """Visit the FIPIRAN homepage to pick up any session cookies / CSRF tokens."""
        if self._session_established:
            return
        try:
            self.session.get(FIPIRAN_WEB + "/", timeout=REQUEST_TIMEOUT)
            self._session_established = True
            logger.debug("FIPIRAN session established")
        except Exception as e:
            logger.debug("FIPIRAN session establishment failed: %s", e)

    def _fetch(self, url: str) -> Optional[requests.Response]:
        """Fetch a FIPIRAN URL, trying several header combinations."""
        header_variants = [
            # 1. Plain JSON request with correct Origin (most likely to work for API routes)
            {"Accept": "application/json", "Origin": FIPIRAN_WEB},
            # 2. Same-origin AJAX style
            {"X-Requested-With": "XMLHttpRequest",
             "Accept": "application/json, text/javascript, */*; q=0.01"},
            # 3. Bare request
            {},
        ]
        for extra in header_variants:
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT,
                                        headers={**self.session.headers, **extra})
                resp.raise_for_status()
                logger.debug(
                    "FIPIRAN %s → %d, Content-Type: %s, size: %d bytes, preview: %s",
                    url, resp.status_code,
                    resp.headers.get("Content-Type", "?"),
                    len(resp.content),
                    resp.text[:150].replace("\n", " "),
                )
                return resp
            except requests.exceptions.HTTPError as e:
                logger.debug("FIPIRAN %s [%s] → %s", url, extra, e)
            except requests.exceptions.RequestException as e:
                logger.warning("FIPIRAN request failed %s: %s", url, e)
        return None

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def get_fixed_income_funds(self) -> list[dict]:
        """Return fixed-income ETF funds with their NAV data."""
        self._establish_session()

        for url in self._API_ENDPOINTS:
            resp = self._fetch(url)
            if resp is None:
                continue

            # ── attempt 1: direct JSON ──
            parsed = self._try_json(resp)
            if parsed:
                logger.info("FIPIRAN: %d fixed-income ETFs from %s (JSON)", len(parsed), url)
                return parsed

            # ── attempt 2: JSON embedded in <script> tags ──
            parsed = self._try_script_json(resp.text)
            if parsed:
                logger.info("FIPIRAN: %d fixed-income ETFs from %s (script JSON)", len(parsed), url)
                return parsed

            # ── attempt 3: HTML <table> (only useful if response is actual HTML with data) ──
            ct = resp.headers.get("Content-Type", "")
            if "html" in ct and len(resp.content) > 5000:
                parsed = self._try_html_table(resp.text)
                if parsed:
                    logger.info("FIPIRAN: %d fixed-income ETFs from %s (HTML table)", len(parsed), url)
                    return parsed

            logger.debug("FIPIRAN: no usable data from %s (size=%d)", url, len(resp.content))

        logger.warning("FIPIRAN: no usable data from any endpoint — NAV will be unavailable")
        return []

    # ------------------------------------------------------------------ #
    #  Parsing strategies                                                  #
    # ------------------------------------------------------------------ #

    def _try_json(self, resp: requests.Response) -> list[dict]:
        try:
            data = resp.json()
            items = data if isinstance(data, list) else (
                data.get("data") or data.get("items") or
                data.get("Result") or data.get("result") or []
            )
            if isinstance(items, list):
                return self._filter_and_map(items)
        except Exception:
            pass
        return []

    def _try_script_json(self, html: str) -> list[dict]:
        """Look for JSON arrays embedded in <script> tags."""
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            text = script.string or ""
            # Match any JSON array that looks like fund data
            for match in re.findall(r'(\[{.{20,}}\])', text, re.DOTALL):
                try:
                    data = json.loads(match)
                    if isinstance(data, list) and len(data) >= 1:
                        result = self._filter_and_map(data)
                        if result:
                            return result
                except Exception:
                    pass
        return []

    def _try_html_table(self, html: str) -> list[dict]:
        """Parse an HTML <table> and map columns to fund data."""
        soup = BeautifulSoup(html, "html.parser")

        # Column header → canonical field name
        NAV_CANCEL_HEADERS = {"قیمت ابطال", "ابطال", "nav ابطال", "cancelNav"}
        NAV_ISSUE_HEADERS  = {"قیمت صدور",  "صدور",  "nav صدور",  "issueNav"}
        NAV_STAT_HEADERS   = {"آخرین nav",  "nav آماری", "statisticalNav", "nav"}
        NAME_HEADERS       = {"نام صندوق", "نام", "name"}
        TYPE_HEADERS       = {"نوع", "نوع صندوق", "fundtype", "typeoffund"}
        ETF_HEADERS        = {"قابل معامله", "etf", "isetf"}

        def _match(header: str, candidates: set) -> bool:
            h = header.strip().lower()
            return any(c in h or h in c for c in candidates)

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue

            # Build header map: col_index → field
            headers = [c.get_text(strip=True).lower()
                       for c in rows[0].find_all(["th", "td"])]
            if not any("صندوق" in h or "nav" in h for h in headers):
                continue  # not a fund table

            col = {}
            for idx, h in enumerate(headers):
                if _match(h, NAME_HEADERS):       col.setdefault("name", idx)
                elif _match(h, TYPE_HEADERS):     col.setdefault("type", idx)
                elif _match(h, ETF_HEADERS):      col.setdefault("etf", idx)
                elif _match(h, NAV_CANCEL_HEADERS): col.setdefault("cancel", idx)
                elif _match(h, NAV_ISSUE_HEADERS):  col.setdefault("issue", idx)
                elif _match(h, NAV_STAT_HEADERS):   col.setdefault("stat", idx)

            if "cancel" not in col and "stat" not in col:
                continue  # no NAV column found

            result = []
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all("td")]
                if not cells:
                    continue

                fund_type = cells[col["type"]].strip() if "type" in col and col["type"] < len(cells) else ""
                if "درآمد ثابت" not in fund_type:
                    continue

                etf_val = cells[col["etf"]].strip() if "etf" in col and col["etf"] < len(cells) else "بله"
                if etf_val and etf_val not in ("بله", "1", "true", "True", "✓", "ETF", ""):
                    continue

                def _num(idx_key):
                    if idx_key not in col or col[idx_key] >= len(cells):
                        return 0.0
                    try:
                        return float(cells[col[idx_key]].replace(",", "").replace("٬", ""))
                    except ValueError:
                        return 0.0

                cancel = _num("cancel")
                issue  = _num("issue")
                stat   = _num("stat") or cancel
                if cancel <= 0:
                    continue

                name = cells[col["name"]].strip() if "name" in col and col["name"] < len(cells) else ""
                result.append({
                    "symbol":          name,
                    "name":            name,
                    "nav":             cancel,
                    "issue_nav":       issue,
                    "statistical_nav": stat,
                    "nav_date":        "",
                    "total_units":     0,
                    "total_nav":       0,
                    "manager":         "",
                })

            if result:
                return result

        return []

    # ------------------------------------------------------------------ #
    #  JSON item normaliser                                                #
    # ------------------------------------------------------------------ #

    def _filter_and_map(self, items: list) -> list[dict]:
        """From a raw list of fund dicts, keep fixed-income ETFs and normalise."""
        result = []
        for f in items:
            fund_type = str(
                f.get("fundType") or f.get("typeOfFund") or
                f.get("FundType") or f.get("TypeOfFund") or ""
            )
            if "درآمد ثابت" not in fund_type:
                continue

            is_etf_raw = (f.get("isEtf") or f.get("isETF") or
                          f.get("IsETF") or f.get("IsEtf") or 0)
            if not (is_etf_raw == 1 or is_etf_raw is True):
                continue

            def _nav(*keys):
                for k in keys:
                    v = f.get(k)
                    if v:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return 0.0

            cancel = _nav("cancelNAV", "cancelNav", "CancelNAV", "cancel_nav")
            issue  = _nav("issueNAV",  "issueNav",  "IssueNAV",  "issue_nav")
            stat   = _nav("statisticalNAV", "statisticalNav", "StatisticalNAV") or cancel

            if cancel <= 0:
                continue

            symbol = (f.get("symbol") or f.get("Symbol") or
                      f.get("name")   or f.get("Name") or "").strip()
            if not symbol:
                continue

            result.append({
                "symbol":          symbol,
                "name":            (f.get("name") or f.get("Name") or symbol).strip(),
                "nav":             cancel,
                "issue_nav":       issue,
                "statistical_nav": stat,
                "nav_date":        f.get("navDate") or f.get("NavDate") or "",
                "total_units":     f.get("shareCount") or f.get("ShareCount") or 0,
                "total_nav":       f.get("totalNetAsset") or 0,
                "manager":         f.get("manager") or f.get("Manager") or "",
            })

        return result


# =========================================================================== #
#  Rahavard 365 NAV fetcher (alternative source)                              #
# =========================================================================== #

class RahavardFetcher:
    """Fetches fund NAV data from rahavard365.com.

    Rahavard 365 is an Iranian financial data portal that aggregates TSE data
    and typically has more accessible APIs than fipiran.ir.
    """

    _BASE = "https://rahavard365.com"
    _API  = "https://api.rahavard365.com"

    # Known API endpoints for fund data
    _FUND_ENDPOINTS = [
        _API + "/v1/funds",
        _API + "/v1/fund/list",
        _BASE + "/api/v1/fund/list",
        _BASE + "/api/funds",
    ]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            **REQUEST_HEADERS,
            "Referer":  self._BASE + "/",
            "Origin":   self._BASE,
        })

    def get_fixed_income_funds(self) -> list[dict]:
        """Try to fetch fixed-income ETF fund data from Rahavard 365."""
        for url in self._FUND_ENDPOINTS:
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                logger.debug("Rahavard %s → %d, size=%d, preview: %s",
                             url, resp.status_code, len(resp.content),
                             resp.text[:200].replace("\n", " "))
                try:
                    data = resp.json()
                except ValueError:
                    logger.debug("Rahavard %s: not JSON", url)
                    continue

                items = (data if isinstance(data, list) else
                         data.get("data") or data.get("items") or
                         data.get("result") or data.get("funds") or [])
                if not isinstance(items, list) or not items:
                    continue

                funds = self._map_items(items)
                if funds:
                    logger.info("Rahavard: %d fixed-income ETFs from %s", len(funds), url)
                    return funds

            except requests.exceptions.HTTPError as e:
                logger.debug("Rahavard %s → HTTP error: %s", url, e)
            except requests.exceptions.RequestException as e:
                logger.debug("Rahavard %s → connection error: %s", url, e)

        logger.debug("Rahavard: no usable data from any endpoint")
        return []

    def _map_items(self, items: list) -> list[dict]:
        result = []
        for f in items:
            if not isinstance(f, dict):
                continue
            # Type filter: درآمد ثابت or fixedIncome
            fund_type = str(
                f.get("fundType") or f.get("type") or
                f.get("typeTitle") or f.get("typeName") or ""
            ).lower()
            if "درآمد" not in fund_type and "fixed" not in fund_type:
                continue

            # ETF filter
            is_etf = (f.get("isEtf") or f.get("isETF") or f.get("etf") or 0)
            if not (is_etf == 1 or is_etf is True or str(is_etf).lower() == "true"):
                continue

            def _nav(*keys):
                for k in keys:
                    v = f.get(k)
                    if v:
                        try:
                            return float(str(v).replace(",", ""))
                        except (TypeError, ValueError):
                            pass
                return 0.0

            cancel = _nav("cancelNav", "cancelNAV", "navCancel", "nav")
            issue  = _nav("issueNav",  "issueNAV",  "navIssue")
            stat   = _nav("statisticalNav", "navStat") or cancel
            if cancel <= 0:
                continue

            symbol = (f.get("symbol") or f.get("ticker") or
                      f.get("name")   or "").strip()
            result.append({
                "symbol":          symbol,
                "name":            (f.get("name") or f.get("fullName") or symbol).strip(),
                "nav":             cancel,
                "issue_nav":       issue,
                "statistical_nav": stat,
                "nav_date":        str(f.get("navDate") or f.get("date") or ""),
                "total_units":     f.get("units") or f.get("shareCount") or 0,
                "total_nav":       f.get("totalNav") or f.get("totalNetAsset") or 0,
                "manager":         f.get("manager") or f.get("managerName") or "",
            })
        return result


# =========================================================================== #
#  DataAggregator                                                              #
# =========================================================================== #

class DataAggregator:
    """Combines TSETMC price data with NAV data from multiple sources."""

    def __init__(self):
        self.tsetmc   = TSETMCFetcher()
        self.fipiran  = FIPIRANFetcher()
        self.rahavard = RahavardFetcher()

    def fetch_all(self, use_fipiran_fallback: bool = True,
                  nav_cache=None) -> list[dict]:
        """Fetch prices + NAV for all funds.

        Parameters
        ----------
        use_fipiran_fallback : bool
            Try FIPIRAN / Rahavard365 if TSETMC doesn't expose NAV.
        nav_cache : Database | None
            If provided, today's NAV is looked up in the DB before hitting
            external sources.  Avoids repeated FIPIRAN/Rahavard calls for
            every intra-day scan (NAV only changes once per trading day).
        """
        logger.info("Fetching price data from TSETMC...")
        results = self.tsetmc.fetch_all_fund_data()

        if not use_fipiran_fallback:
            return results

        # ── Fill NAV from DB cache (today's stored NAV) ──────────────────
        if nav_cache is not None:
            for result in results:
                if result["nav_data"] is not None:
                    continue
                cached = nav_cache.get_cached_nav(result["symbol"])
                if cached:
                    result["nav_data"] = cached
                    result["error"]    = None
                    logger.info("  NAV for %s from DB cache (cancel_nav=%s)",
                                result["symbol"], cached["cancel_nav"])

        missing_nav = [r for r in results if r["nav_data"] is None]
        if not missing_nav:
            return results

        logger.info("%d fund(s) missing NAV — trying external NAV sources...",
                    len(missing_nav))

        # Try FIPIRAN first
        fipiran_funds = self.fipiran.get_fixed_income_funds()
        if fipiran_funds:
            self._fill_nav(results, fipiran_funds, "FIPIRAN")
        else:
            logger.info("FIPIRAN returned no data — trying Rahavard 365...")
            rahavard_funds = self.rahavard.get_fixed_income_funds()
            if rahavard_funds:
                self._fill_nav(results, rahavard_funds, "Rahavard365")
            else:
                logger.warning("All external NAV sources exhausted — NAV unavailable")

        still_missing = [r["symbol"] for r in results if r["nav_data"] is None]
        if still_missing:
            logger.warning("NAV still missing: %s", ", ".join(still_missing))

        return results

    def _fill_nav(self, results: list[dict], nav_funds: list[dict],
                  source_name: str) -> None:
        """Match *nav_funds* entries to *results* and fill in missing NAVs."""
        by_symbol = {_normalize(f["symbol"]): f for f in nav_funds}
        by_name   = {_normalize(f["name"]):   f for f in nav_funds}

        for result in results:
            if result["nav_data"] is not None:
                continue

            sym = _normalize(result["symbol"])
            fip = by_symbol.get(sym) or by_name.get(_normalize(result["name"]))

            # Fuzzy: check if any fund name contains our symbol
            if not fip:
                for ff in nav_funds:
                    if sym in _normalize(ff["name"]) or \
                       _normalize(ff.get("symbol", "")) in _normalize(result["name"]):
                        fip = ff
                        break

            if fip:
                result["nav_data"] = {
                    "nav_per_unit":    fip["nav"],
                    "statistical_nav": fip["statistical_nav"],
                    "issue_nav":       fip["issue_nav"],
                    "cancel_nav":      fip["nav"],
                    "total_nav":       fip["total_nav"],
                    "fund_units":      fip["total_units"],
                    "nav_date":        fip["nav_date"],
                    "source":          source_name,
                }
                result["error"] = None
                logger.info("  NAV for %s filled from %s (cancel_nav=%s)",
                            result["symbol"], source_name, fip["nav"])

    def discover_new_funds(self, keywords: list[str] = None) -> list[dict]:
        if keywords is None:
            keywords = ["صندوق", "درآمد", "ثابت"]
        known = {f["ins_code"] for f in FIXED_INCOME_ETFS if f["ins_code"]}
        new_funds = []
        for kw in keywords:
            for r in self.tsetmc.search_instrument(kw):
                if r["ins_code"] not in known:
                    new_funds.append(r)
                    known.add(r["ins_code"])
        return new_funds
