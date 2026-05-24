"""Fetch real-time price and NAV data for fixed-income ETFs from TSETMC and FIPIRAN."""

import json
import re
import time
import logging
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
)

logger = logging.getLogger(__name__)

TSETMC_MAIN = "https://www.tsetmc.com"


def _normalize(text: str) -> str:
    """Normalize Arabic-script characters to Persian equivalents.

    TSETMC stores fund names using Arabic letters (e.g. Arabic ya ي U+064A,
    Arabic kaf ك U+0643) while Python strings typically use the visually
    identical Persian codepoints (ya ی U+06CC, kaf ک U+06A9).
    A plain == comparison therefore fails even though the names look the same.
    """
    return (
        text
        .replace("ي", "ی")  # ي → ی  (Arabic ya → Persian ya)
        .replace("ك", "ک")  # ك → ک  (Arabic kaf → Persian kaf)
        .replace("ة", "ه")  # ة → ه  (ta marbuta → Persian he)
        .strip()
    )


# =========================================================================== #
#  TSETMC fetcher                                                              #
# =========================================================================== #

class TSETMCFetcher:
    """Fetches price and order-book data from cdn.tsetmc.com."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = "https://www.tsetmc.com/"
        self._ins_code_cache: dict[str, str] = {}
        self._etf_list_cache: Optional[list[dict]] = None   # cached ETF list

    # ------------------------------------------------------------------ #
    #  Internal HTTP helper                                                #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, silent: bool = False,
             html: bool = False) -> Optional[dict | str]:
        """Fetch *url*.

        Returns parsed JSON dict by default.
        When *html=True* returns the raw response text (string).
        Returns None on any error.
        """
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if html:
                return resp.text
            return resp.json()
        except requests.exceptions.RequestException as e:
            if not silent:
                logger.warning("TSETMC request failed for %s: %s", url, e)
            return None
        except ValueError:
            if not silent:
                logger.warning("Invalid JSON from %s", url)
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
    #  NAV data — multiple strategies tried in sequence                   #
    # ------------------------------------------------------------------ #

    def get_fund_nav(self, ins_code: str) -> Optional[dict]:
        """Try every known strategy to get fund NAV for *ins_code*.

        Strategies (ordered by confidence):
        1. InstrumentInfo — navStat field (TSETMC statistical NAV)
        2. ETF-specific CDN endpoints
        3. Fund-specific CDN endpoints (historically 404)
        4. ETFList bulk endpoint (cached)
        5. Scrape www.tsetmc.com/instrument HTML page
        6. StaticThreshold (low probability)
        """

        # ── 1. GetInstrumentInfo ──────────────────────────────────────────
        url = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
        data = self._get(url)
        if data:
            # Log top-level keys (might have nav at root, not just inside sub-object)
            logger.debug("InstrumentInfo top-level keys for %s: %s",
                         ins_code, list(data.keys()))
            info = data.get("instrumentInfo", {})
            logger.debug("InstrumentInfo non-zero fields for %s: %s", ins_code,
                         {k: v for k, v in info.items()
                          if v not in (None, 0, "", [], {})})
            # Check both top-level and sub-object
            for search_obj in (info, data):
                for nav_key in ("navStat", "nav", "statisticalNav", "cancelNav",
                                "staticNav", "psGelStaMax", "navPerUnit",
                                "pStat", "pStatNav", "pNavStat"):
                    nav = search_obj.get(nav_key)
                    if nav and isinstance(nav, (int, float)) and nav > 0:
                        logger.info("  NAV from InstrumentInfo[%s] = %s for %s",
                                    nav_key, nav, ins_code)
                        return self._build_nav(
                            nav,
                            search_obj.get("cancelNav", nav),
                            search_obj.get("issueNav",  nav),
                            str(search_obj.get("dEven", "")),
                            "TSETMC/InstrumentInfo",
                        )

        # ── 1b. Old TSETMC Loader API (TseClient pipe-separated format) ──
        nav = self._nav_from_loader(ins_code)
        if nav:
            return nav

        # ── 2. ETF-specific endpoints ─────────────────────────────────────
        for path in (
            "ETF/ETFByInsCode",
            "ETF/GetETFByInsCode",
            "ETF/GetETFInfo",
        ):
            data = self._get(f"{TSETMC_CDN}/{path}/{ins_code}", silent=True)
            if not data:
                continue
            logger.debug("ETF endpoint '%s' raw for %s: %s", path, ins_code,
                         str(data)[:300])
            obj = (data.get("eTF") or data.get("etf") or
                   data.get("ETF") or data.get("data") or data)
            if not isinstance(obj, dict):
                continue
            for nav_key in ("cancelNav", "cancelNAV", "navStat", "nav",
                            "statisticalNav", "navPerUnit"):
                nav = obj.get(nav_key)
                if nav and isinstance(nav, (int, float)) and nav > 0:
                    logger.info("  NAV from %s: %s = %s", path, nav_key, nav)
                    return self._build_nav(
                        nav,
                        obj.get("cancelNav", obj.get("cancelNAV", nav)),
                        obj.get("issueNav",  obj.get("issueNAV",  nav)),
                        str(obj.get("navDate", obj.get("dEven", ""))),
                        f"TSETMC/{path}",
                    )

        # ── 3. Fund-specific endpoints (historically 404) ─────────────────
        for path in ("GetFundInfo", "GetFund", "GetFundLastInfo",
                     "GetMutualFundByInsCode", "FundInfo", "FundData"):
            data = self._get(f"{TSETMC_CDN}/Fund/{path}/{ins_code}", silent=True)
            if not data:
                continue
            logger.debug("Fund endpoint '%s' raw for %s: %s", path, ins_code,
                         str(data)[:300])
            for key in ("fund", "fundInfo", "fundLastInfo", "data", None):
                obj = data.get(key) if key else data
                if not isinstance(obj, dict):
                    continue
                for nav_key in ("cancelNav", "cancelNAV", "navStat", "nav",
                                "statisticalNav"):
                    nav = obj.get(nav_key)
                    if nav and isinstance(nav, (int, float)) and nav > 0:
                        logger.info("  NAV from Fund/%s: %s = %s",
                                    path, nav_key, nav)
                        return self._build_nav(
                            nav,
                            obj.get("cancelNav", obj.get("cancelNAV", nav)),
                            obj.get("issueNav",  obj.get("issueNAV",  nav)),
                            str(obj.get("dEven", obj.get("navDate", ""))),
                            f"TSETMC/Fund/{path}",
                        )

        # ── 4. ETFList bulk endpoint (fetch once, cache) ──────────────────
        nav = self._nav_from_etf_list(ins_code)
        if nav:
            return nav

        # ── 5. Scrape www.tsetmc.com/instrument HTML page ─────────────────
        nav = self._nav_from_html(ins_code)
        if nav:
            return nav

        # ── 6. StaticThreshold (rarely has NAV) ───────────────────────────
        data = self._get(
            f"{TSETMC_CDN}/StaticThreshold/GetStaticThreshold/{ins_code}/0",
            silent=True,
        )
        if data:
            for t in data.get("staticThreshold", []):
                for nav_key in ("navStat", "nav", "cancelNav"):
                    nav_val = t.get(nav_key)
                    if nav_val and isinstance(nav_val, (int, float)) and nav_val > 0:
                        logger.info("  NAV from StaticThreshold[%s] = %s",
                                    nav_key, nav_val)
                        return self._build_nav(nav_val, nav_val, nav_val,
                                               "", "TSETMC/StaticThreshold")

        logger.debug("  No NAV found for ins_code=%s", ins_code)
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
        # Statistical NAV for Iranian ETFs is 7-10 digits
        nav_candidates = []
        for p in parts:
            p = p.strip()
            if re.match(r'^\d{7,10}$', p):
                nav_candidates.append(float(p))
        if nav_candidates:
            # The largest plausible value is probably the NAV (unit price range)
            # Filter: typical Iranian fixed-income ETF NAV: 8,000,000 – 30,000,000 Rial
            plausible = [v for v in nav_candidates if 1_000_000 <= v <= 200_000_000]
            if plausible:
                nav_val = sorted(plausible)[len(plausible)//2]  # median
                logger.info("  NAV from Loader API (pipe-parse) = %s for %s",
                            nav_val, ins_code)
                return self._build_nav(nav_val, nav_val, nav_val,
                                       "", "TSETMC/Loader")
        return None

    def _nav_from_etf_list(self, ins_code: str) -> Optional[dict]:
        """Fetch the ETFList once, cache it, then look up *ins_code*."""
        if self._etf_list_cache is None:
            raw = self._get(f"{TSETMC_CDN}/ETF/ETFList", silent=True)
            if raw:
                logger.debug("ETFList raw (first 500 chars): %s", str(raw)[:500])
                self._etf_list_cache = (
                    raw.get("eTFList") or raw.get("etfList") or
                    raw.get("data") or raw if isinstance(raw, list) else []
                )
            else:
                self._etf_list_cache = []

        for item in (self._etf_list_cache or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("insCode", "")) != str(ins_code):
                continue
            for nav_key in ("cancelNav", "cancelNAV", "navStat", "nav",
                            "statisticalNav"):
                nav_val = item.get(nav_key)
                if nav_val and isinstance(nav_val, (int, float)) and nav_val > 0:
                    logger.info("  NAV from ETFList[%s] = %s for %s",
                                nav_key, nav_val, ins_code)
                    return self._build_nav(
                        nav_val,
                        item.get("cancelNav", nav_val),
                        item.get("issueNav",  nav_val),
                        str(item.get("navDate", "")),
                        "TSETMC/ETFList",
                    )
        return None

    def _nav_from_html(self, ins_code: str) -> Optional[dict]:
        """Scrape the TSETMC instrument HTML page for NAV data.

        TSETMC renders partial data server-side (or embeds it in a __NEXT_DATA__
        JSON blob).  We try three extraction patterns:
        a) <script id="__NEXT_DATA__"> JSON blob (Next.js SSR)
        b) Inline JSON variables in any <script> tag
        c) Direct HTML element search (data-nav, aria-label, specific IDs)
        """
        url = f"{TSETMC_MAIN}/instrument/{ins_code}"
        html = self._get(url, html=True)
        if not html:
            logger.debug("  HTML page unavailable for ins_code=%s", ins_code)
            return None

        logger.debug("  HTML page for %s: %d bytes", ins_code, len(html))

        # a) Next.js __NEXT_DATA__ blob
        match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                          html, re.DOTALL | re.IGNORECASE)
        if match:
            try:
                blob = json.loads(match.group(1))
                nav = self._extract_nav_from_json(blob, ins_code)
                if nav:
                    logger.info("  NAV from TSETMC HTML __NEXT_DATA__ for %s", ins_code)
                    return nav
            except Exception as e:
                logger.debug("  __NEXT_DATA__ parse error for %s: %s", ins_code, e)

        # b) Look for known variable/key patterns in all <script> tags
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            text = script.string or ""
            if not text:
                continue

            # Pattern: navStat:12345678 or "navStat":"12345678"
            for pattern in (
                r'["\']?navStat["\']?\s*[:=]\s*["\']?(\d[\d,]*)',
                r'["\']?cancelNav["\']?\s*[:=]\s*["\']?(\d[\d,]*)',
                r'["\']?statisticalNav["\']?\s*[:=]\s*["\']?(\d[\d,]*)',
            ):
                m = re.search(pattern, text)
                if m:
                    nav_str = m.group(1).replace(",", "")
                    try:
                        nav_val = float(nav_str)
                        if nav_val > 100:   # sanity: NAV for Iranian funds is always >> 100
                            logger.info(
                                "  NAV from TSETMC HTML script pattern (%s) = %s for %s",
                                pattern[:30], nav_val, ins_code,
                            )
                            return self._build_nav(nav_val, nav_val, nav_val,
                                                   "", "TSETMC/HTML-script")
                    except ValueError:
                        pass

            # Try generic JSON arrays/objects embedded in script
            for json_match in re.findall(r'(\{[^<]{50,}\})', text, re.DOTALL):
                try:
                    obj = json.loads(json_match)
                    nav = self._extract_nav_from_json(obj, ins_code)
                    if nav:
                        logger.info("  NAV from TSETMC HTML inline JSON for %s", ins_code)
                        return nav
                except Exception:
                    pass

        # c) Element-based extraction
        for css_id in ("NavStat", "navStat", "LastNav", "cancelNav", "nav"):
            el = soup.find(id=css_id)
            if el:
                text = el.get_text(strip=True).replace(",", "").replace("٬", "")
                try:
                    nav_val = float(text)
                    if nav_val > 100:
                        logger.info("  NAV from HTML #%s = %s for %s",
                                    css_id, nav_val, ins_code)
                        return self._build_nav(nav_val, nav_val, nav_val,
                                               "", "TSETMC/HTML-element")
                except ValueError:
                    pass

        logger.debug("  No NAV found in TSETMC HTML for ins_code=%s", ins_code)
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

    def fetch_all_fund_data(self, funds: list[dict] = None,
                            delay: float = 0.5) -> list[dict]:
        if funds is None:
            funds = FIXED_INCOME_ETFS

        results = []
        for i, fund in enumerate(funds):
            symbol      = fund["symbol"]
            ins_code    = fund.get("ins_code", "").strip()
            alt_symbols = fund.get("alt_symbols", [])
            logger.info("[%d/%d] Fetching %s ...", i + 1, len(funds), symbol)

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

            time.sleep(delay * 0.3)

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

            time.sleep(delay * 0.3)

            # Fetch order book
            if ins_code:
                entry["order_book"] = self.get_best_limits(ins_code)

            results.append(entry)
            if i < len(funds) - 1:
                time.sleep(delay * 0.4)

        return results

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
