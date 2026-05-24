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

    # ------------------------------------------------------------------ #
    #  Internal HTTP helper                                                #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, silent: bool = False) -> Optional[dict]:
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
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

        Tries the primary symbol first, then any alt_symbols provided.
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
                candidate,
                len(instruments),
                [i.get("lVal18AFC", "").strip() for i in instruments[:5]],
            )

            # Priority 1: exact ticker match (normalize both sides: TSETMC uses Arabic chars)
            for inst in instruments:
                tsetmc_symbol = _normalize(inst.get("lVal18AFC", ""))
                if tsetmc_symbol == _normalize(candidate):
                    code = inst.get("insCode", "")
                    if code:
                        logger.info("  ✓ ins_code for '%s': %s (TSETMC ticker: %s)",
                                    candidate, code, inst.get("lVal18AFC", ""))
                        self._ins_code_cache[symbol] = code
                        return code

            # Priority 2: fund instrument (name contains صندوق + درآمد)
            for inst in instruments:
                name = _normalize(inst.get("lVal30", ""))
                if "صندوق" in name and "درآمد" in name:
                    code = inst.get("insCode", "")
                    if code:
                        logger.info(
                            "  ✓ ins_code for '%s' via fund-name match: %s (%s)",
                            candidate, code, name,
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

        # Check if NAV happens to be embedded (rare but possible)
        for nav_field in ("navStat", "nav", "statisticalNav", "cancelNav", "navValue"):
            nav = info.get(nav_field)
            if nav and isinstance(nav, (int, float)) and nav > 0:
                result["embedded_nav"] = float(nav)
                logger.debug("  Embedded NAV in ClosingPriceInfo[%s] = %s", nav_field, nav)
                break

        return result

    # ------------------------------------------------------------------ #
    #  NAV data — TSETMC does not expose fund NAV via a stable endpoint.  #
    #  We try several candidates silently; all currently return 404.      #
    #  The real NAV comes from FIPIRAN (see FIPIRANFetcher).              #
    # ------------------------------------------------------------------ #

    def get_fund_nav(self, ins_code: str) -> Optional[dict]:
        """Try multiple TSETMC endpoints for fund NAV (all silently)."""

        # 1. GetInstrumentInfo — check for any nav field
        url = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
        data = self._get(url)
        if data:
            info = data.get("instrumentInfo", {})
            for nav_key in ("navStat", "nav", "statisticalNav", "cancelNav", "staticNav"):
                nav = info.get(nav_key)
                if nav and isinstance(nav, (int, float)) and nav > 0:
                    logger.info("  NAV from InstrumentInfo[%s] = %s", nav_key, nav)
                    return self._build_nav(nav, info.get("cancelNav", nav),
                                          info.get("issueNav", nav),
                                          str(info.get("dEven", "")),
                                          "TSETMC/InstrumentInfo")

        # 2. Various Fund-specific endpoints (silent — expected to 404)
        for path in ("GetFundInfo", "GetFund", "GetFundLastInfo", "GetMutualFundByInsCode"):
            data = self._get(f"{TSETMC_CDN}/Fund/{path}/{ins_code}", silent=True)
            if not data:
                continue
            for key in ("fund", "fundInfo", "fundLastInfo", "data", None):
                obj = data.get(key) if key else data
                if not isinstance(obj, dict):
                    continue
                for nav_key in ("cancelNav", "cancelNAV", "navStat", "nav", "statisticalNav"):
                    nav = obj.get(nav_key)
                    if nav and isinstance(nav, (int, float)) and nav > 0:
                        logger.info("  NAV from Fund/%s: %s = %s", path, nav_key, nav)
                        return self._build_nav(
                            nav,
                            obj.get("cancelNav", obj.get("cancelNAV", nav)),
                            obj.get("issueNav",  obj.get("issueNAV",  nav)),
                            str(obj.get("dEven", obj.get("navDate", ""))),
                            f"TSETMC/{path}",
                        )

        # 3. StaticThreshold — contains price band, rarely NAV
        data = self._get(f"{TSETMC_CDN}/StaticThreshold/GetStaticThreshold/{ins_code}/0", silent=True)
        if data:
            for t in data.get("staticThreshold", []):
                for nav_key in ("navStat", "nav", "cancelNav"):
                    nav = t.get(nav_key)
                    if nav and isinstance(nav, (int, float)) and nav > 0:
                        logger.info("  NAV from StaticThreshold[%s] = %s", nav_key, nav)
                        return self._build_nav(nav, nav, nav, "", "TSETMC/StaticThreshold")

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

            # Fetch NAV from TSETMC (usually unavailable; FIPIRAN fills this later)
            nav = self.get_fund_nav(ins_code) if ins_code else None
            if nav is None and price and price.get("embedded_nav"):
                embedded = price["embedded_nav"]
                nav = self._build_nav(embedded, embedded, embedded, "", "TSETMC/embedded")
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
#  DataAggregator                                                              #
# =========================================================================== #

class DataAggregator:
    """Combines TSETMC price data with FIPIRAN NAV data."""

    def __init__(self):
        self.tsetmc  = TSETMCFetcher()
        self.fipiran = FIPIRANFetcher()

    def fetch_all(self, use_fipiran_fallback: bool = True) -> list[dict]:
        logger.info("Fetching price data from TSETMC...")
        results = self.tsetmc.fetch_all_fund_data()

        if not use_fipiran_fallback:
            return results

        missing_nav = [r for r in results if r["nav_data"] is None]
        if not missing_nav:
            return results

        logger.info("%d fund(s) missing NAV — fetching from FIPIRAN...", len(missing_nav))
        fipiran_funds = self.fipiran.get_fixed_income_funds()

        if not fipiran_funds:
            logger.warning("FIPIRAN returned no data — NAV will be unavailable")
            return results

        # Build a flexible lookup: try exact symbol match, then partial name match
        fipiran_by_symbol = {f["symbol"]: f for f in fipiran_funds}
        fipiran_by_name   = {f["name"]:   f for f in fipiran_funds}

        for result in results:
            if result["nav_data"] is not None:
                continue

            fip = (fipiran_by_symbol.get(result["symbol"]) or
                   fipiran_by_name.get(result["name"]))

            # Fallback: partial name match
            if not fip:
                for ff in fipiran_funds:
                    if result["symbol"] in ff["name"] or ff["symbol"] in result["name"]:
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
                    "source":          "FIPIRAN",
                }
                result["error"] = None
                logger.info("  NAV for %s filled from FIPIRAN", result["symbol"])

        still_missing = [r["symbol"] for r in results if r["nav_data"] is None]
        if still_missing:
            logger.warning("NAV still missing after FIPIRAN: %s", ", ".join(still_missing))

        return results

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
