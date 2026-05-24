"""Fetch real-time price and NAV data for fixed-income ETFs from TSETMC and FIPIRAN."""

import time
import logging
from typing import Optional
from urllib.parse import quote

import requests

from config import (
    TSETMC_CDN,
    FIPIRAN_WEB,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    FIXED_INCOME_ETFS,
)

logger = logging.getLogger(__name__)


class TSETMCFetcher:
    """Fetches price and order-book data from cdn.tsetmc.com."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = "https://www.tsetmc.com/"
        self._ins_code_cache: dict[str, str] = {}  # symbol → ins_code

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, silent: bool = False) -> Optional[dict]:
        """GET a URL and return parsed JSON, or None on any error."""
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

    def discover_ins_code(self, symbol: str) -> Optional[str]:
        """Search TSETMC for the instrument code of a fund symbol.

        Returns the ins_code string, or None if not found.
        Caches results to avoid redundant API calls.
        """
        if symbol in self._ins_code_cache:
            return self._ins_code_cache[symbol]

        url = f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{quote(symbol)}"
        data = self._get(url)
        if not data:
            return None

        instruments = data.get("instrumentSearch", [])

        # Priority 1: exact ticker match (lVal18AFC is the short symbol field)
        for inst in instruments:
            if inst.get("lVal18AFC", "").strip() == symbol:
                code = inst.get("insCode", "")
                if code:
                    logger.info("  ✓ ins_code discovered for %s: %s", symbol, code)
                    self._ins_code_cache[symbol] = code
                    return code

        # Priority 2: fund instrument whose full name contains "صندوق" + "درآمد"
        for inst in instruments:
            name = inst.get("lVal30", "")
            if "صندوق" in name and "درآمد" in name:
                code = inst.get("insCode", "")
                if code:
                    logger.info("  ✓ ins_code discovered for %s via fund-name match: %s", symbol, code)
                    self._ins_code_cache[symbol] = code
                    return code

        logger.warning("  Could not find ins_code for symbol: %s", symbol)
        return None

    # ------------------------------------------------------------------ #
    #  Price data                                                          #
    # ------------------------------------------------------------------ #

    def get_closing_price_info(self, ins_code: str) -> Optional[dict]:
        """Get current market price data from TSETMC ClosingPriceInfo endpoint."""
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

        # TSETMC may embed NAV in the closing price response for fund instruments
        for nav_field in ("navStat", "nav", "statisticalNav", "cancelNav", "navValue"):
            nav = info.get(nav_field)
            if nav and isinstance(nav, (int, float)) and nav > 0:
                result["embedded_nav"] = float(nav)
                logger.debug("  Embedded NAV found in ClosingPriceInfo[%s] = %s", nav_field, nav)
                break

        return result

    # ------------------------------------------------------------------ #
    #  NAV data — tries several TSETMC endpoints silently                 #
    # ------------------------------------------------------------------ #

    def get_fund_nav(self, ins_code: str) -> Optional[dict]:
        """Try multiple TSETMC endpoints to get fund NAV.

        Returns a dict with cancel_nav, issue_nav, statistical_nav, etc.,
        or None if no NAV data could be found.
        """
        # 1. GetInstrumentInfo — sometimes includes navStat for ETF funds
        url = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
        data = self._get(url)
        if data:
            info = data.get("instrumentInfo", {})
            for nav_key in ("navStat", "nav", "statisticalNav", "cancelNav", "staticNav", "navValue"):
                nav = info.get(nav_key)
                if nav and isinstance(nav, (int, float)) and nav > 0:
                    logger.debug("  NAV from InstrumentInfo[%s] = %s", nav_key, nav)
                    cancel = info.get("cancelNav", nav)
                    issue  = info.get("issueNav", nav)
                    return {
                        "nav_per_unit":    float(cancel),
                        "statistical_nav": float(nav),
                        "issue_nav":       float(issue),
                        "cancel_nav":      float(cancel),
                        "total_nav":       0,
                        "fund_units":      info.get("cs", 0),
                        "nav_date":        str(info.get("dEven", "")),
                        "source":          "TSETMC/InstrumentInfo",
                    }

        # 2. Various Fund-specific endpoints (try silently — most may 404)
        fund_endpoints = [
            f"{TSETMC_CDN}/Fund/GetFundInfo/{ins_code}",
            f"{TSETMC_CDN}/Fund/GetFund/{ins_code}",
            f"{TSETMC_CDN}/Fund/GetFundLastInfo/{ins_code}",
            f"{TSETMC_CDN}/Fund/GetMutualFundByInsCode/{ins_code}",
        ]
        for url in fund_endpoints:
            data = self._get(url, silent=True)
            if not data:
                continue
            # Unwrap common response wrappers
            for key in ("fund", "fundInfo", "fundLastInfo", "data", None):
                obj = data.get(key) if key else data
                if not isinstance(obj, dict):
                    continue
                for nav_key in ("cancelNav", "cancelNAV", "navStat", "nav",
                                "statisticalNav", "statisticalNAV", "NAV"):
                    nav = obj.get(nav_key)
                    if nav and isinstance(nav, (int, float)) and nav > 0:
                        logger.info("  NAV from %s: %s = %s", url, nav_key, nav)
                        cancel = obj.get("cancelNav", obj.get("cancelNAV", nav))
                        issue  = obj.get("issueNav",  obj.get("issueNAV",  nav))
                        stat   = obj.get("statisticalNav", obj.get("statisticalNAV", nav))
                        return {
                            "nav_per_unit":    float(cancel),
                            "statistical_nav": float(stat),
                            "issue_nav":       float(issue),
                            "cancel_nav":      float(cancel),
                            "total_nav":       obj.get("totalNetAsset", 0),
                            "fund_units":      obj.get("fundUnit", 0) or obj.get("shareCount", 0),
                            "nav_date":        str(obj.get("dEven", obj.get("navDate", ""))),
                            "source":          f"TSETMC/{url.split('/')[-2]}",
                        }

        # 3. StaticThreshold — occasionally contains NAV for fund instruments
        url = f"{TSETMC_CDN}/StaticThreshold/GetStaticThreshold/{ins_code}/0"
        data = self._get(url, silent=True)
        if data:
            thresholds = data.get("staticThreshold", [])
            if thresholds:
                t = thresholds[0]
                for nav_key in ("navStat", "nav", "cancelNav"):
                    nav = t.get(nav_key)
                    if nav and isinstance(nav, (int, float)) and nav > 0:
                        logger.info("  NAV from StaticThreshold[%s] = %s", nav_key, nav)
                        return {
                            "nav_per_unit": float(nav), "statistical_nav": float(nav),
                            "issue_nav": float(nav),    "cancel_nav": float(nav),
                            "total_nav": 0,             "fund_units": 0,
                            "nav_date":  "",            "source": "TSETMC/StaticThreshold",
                        }

        return None

    # ------------------------------------------------------------------ #
    #  Order book                                                          #
    # ------------------------------------------------------------------ #

    def get_best_limits(self, ins_code: str) -> Optional[dict]:
        url = f"{TSETMC_CDN}/BestLimits/{ins_code}"
        data = self._get(url)
        if not data or "bestLimits" not in data:
            return None

        bids, asks = [], []
        for row in data["bestLimits"]:
            if row.get("number", 0) <= 5:
                bids.append({
                    "price":  row.get("pMeDem", 0),
                    "volume": row.get("qTitMeDem", 0),
                    "count":  row.get("zOrdMeDem", 0),
                })
                asks.append({
                    "price":  row.get("pMeOf", 0),
                    "volume": row.get("qTitMeOf", 0),
                    "count":  row.get("zOrdMeOf", 0),
                })
        return {"bids": bids, "asks": asks}

    # ------------------------------------------------------------------ #
    #  Search (for discover mode)                                          #
    # ------------------------------------------------------------------ #

    def search_instrument(self, keyword: str) -> list[dict]:
        url = f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{quote(keyword)}"
        data = self._get(url)
        if not data or "instrumentSearch" not in data:
            return []
        return [
            {
                "ins_code":  item.get("insCode", ""),
                "symbol":    item.get("lVal18AFC", "").strip(),
                "full_name": item.get("lVal30", "").strip(),
            }
            for item in data["instrumentSearch"]
        ]

    # ------------------------------------------------------------------ #
    #  Main fetch loop                                                     #
    # ------------------------------------------------------------------ #

    def fetch_all_fund_data(self, funds: list[dict] = None, delay: float = 0.5) -> list[dict]:
        """Fetch price + NAV for every fund in the list.

        For funds with an empty ins_code, auto-discovery via TSETMC search is
        attempted. If the stored ins_code causes a 500, discovery is retried.
        """
        if funds is None:
            funds = FIXED_INCOME_ETFS

        results = []
        total = len(funds)

        for i, fund in enumerate(funds):
            symbol   = fund["symbol"]
            ins_code = fund.get("ins_code", "").strip()
            logger.info("[%d/%d] Fetching %s ...", i + 1, total, symbol)

            entry = {
                "symbol":     symbol,
                "name":       fund["name"],
                "ins_code":   ins_code,
                "price_data": None,
                "nav_data":   None,
                "order_book": None,
                "error":      None,
            }

            # --- Step 1: ensure we have an ins_code ---
            if not ins_code:
                ins_code = self.discover_ins_code(symbol) or ""
                entry["ins_code"] = ins_code

            # --- Step 2: fetch price ---
            price = self._fetch_price_with_fallback(symbol, ins_code)
            if price is not None:
                ins_code = price.pop("_ins_code", ins_code)  # update if discovery changed it
                entry["ins_code"] = ins_code
                entry["price_data"] = price
            else:
                entry["error"] = "قیمت دریافت نشد"

            time.sleep(delay * 0.3)

            # --- Step 3: fetch NAV ---
            nav = self.get_fund_nav(ins_code) if ins_code else None

            # Fallback: use NAV embedded in price response
            if nav is None and price and price.get("embedded_nav"):
                embedded = price["embedded_nav"]
                nav = {
                    "nav_per_unit": embedded, "statistical_nav": embedded,
                    "issue_nav":    embedded, "cancel_nav":      embedded,
                    "total_nav": 0,           "fund_units": 0,
                    "nav_date":  "",          "source": "TSETMC/embedded",
                }
                logger.debug("  Using embedded NAV: %s", embedded)

            entry["nav_data"] = nav
            if nav is None:
                suffix = "NAV دریافت نشد"
                entry["error"] = f"{entry['error']}; {suffix}" if entry["error"] else suffix

            time.sleep(delay * 0.3)

            # --- Step 4: order book ---
            if ins_code:
                entry["order_book"] = self.get_best_limits(ins_code)

            results.append(entry)
            if i < total - 1:
                time.sleep(delay * 0.4)

        return results

    def _fetch_price_with_fallback(self, symbol: str, ins_code: str) -> Optional[dict]:
        """Try stored ins_code; if it fails (500), discover and retry once."""
        if ins_code:
            price = self.get_closing_price_info(ins_code)
            if price is not None:
                return price
            # 500 → stored code is wrong, try discovery
            logger.info("  Stored ins_code failed, searching TSETMC for %s...", symbol)

        new_code = self.discover_ins_code(symbol)
        if new_code and new_code != ins_code:
            price = self.get_closing_price_info(new_code)
            if price is not None:
                price["_ins_code"] = new_code  # bubble up the corrected code
                return price

        return None


# --------------------------------------------------------------------------- #
#  FIPIRAN — primary source for NAV (one request returns all funds)           #
# --------------------------------------------------------------------------- #

class FIPIRANFetcher:
    """Fetches fund NAV data from www.fipiran.ir."""

    # Multiple endpoints to try — fipiran sometimes changes paths
    _ENDPOINTS = [
        f"{FIPIRAN_WEB}/DataService/FundCompare",
        f"{FIPIRAN_WEB}/api/v1/fund/fundlist",
        f"{FIPIRAN_WEB}/api/fund/fundcompare",
    ]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = FIPIRAN_WEB + "/"

    def _get(self, url: str) -> Optional[list | dict]:
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            text = resp.text.strip()
            if text.startswith("[") or text.startswith("{"):
                return resp.json()
            return None
        except Exception as e:
            logger.warning("FIPIRAN request failed for %s: %s", url, e)
            return None

    def get_fixed_income_funds(self) -> list[dict]:
        """Return list of fixed-income ETF funds with their NAV data."""
        for url in self._ENDPOINTS:
            data = self._get(url)
            if not data:
                continue
            items = data if isinstance(data, list) else (
                data.get("data") or data.get("items") or data.get("funds") or []
            )
            if not isinstance(items, list) or not items:
                continue
            parsed = self._parse(items)
            if parsed:
                logger.info("FIPIRAN: got %d fixed-income ETFs from %s", len(parsed), url)
                return parsed

        logger.warning("FIPIRAN: no data from any endpoint")
        return []

    def _parse(self, items: list) -> list[dict]:
        """Parse a raw FIPIRAN fund list, keeping only fixed-income ETFs."""
        result = []
        for f in items:
            # fund type — handle various key names
            fund_type = str(
                f.get("fundType") or f.get("typeOfFund") or f.get("FundType") or
                f.get("TypeOfFund") or ""
            )
            if "درآمد ثابت" not in fund_type:
                continue

            # ETF flag — int 1 or bool True
            is_etf_raw = (
                f.get("isEtf") or f.get("isETF") or f.get("IsETF") or
                f.get("IsEtf") or 0
            )
            is_etf = (is_etf_raw == 1 or is_etf_raw is True)
            if not is_etf:
                continue

            # NAV fields — FIPIRAN uses mixed capitalization
            cancel = float(
                f.get("cancelNAV") or f.get("cancelNav") or f.get("CancelNAV") or
                f.get("cancel_nav") or 0
            )
            issue = float(
                f.get("issueNAV") or f.get("issueNav") or f.get("IssueNAV") or
                f.get("issue_nav") or 0
            )
            stat = float(
                f.get("statisticalNAV") or f.get("statisticalNav") or
                f.get("StatisticalNAV") or cancel
            )

            if cancel <= 0:
                continue

            symbol = (
                f.get("symbol") or f.get("Symbol") or f.get("name") or f.get("Name") or ""
            ).strip()
            if not symbol:
                continue

            result.append({
                "symbol":        symbol,
                "name":          (f.get("name") or f.get("Name") or symbol).strip(),
                "nav":           cancel,
                "issue_nav":     issue,
                "statistical_nav": stat,
                "nav_date":      f.get("navDate") or f.get("NavDate") or "",
                "total_units":   f.get("shareCount") or f.get("ShareCount") or 0,
                "total_nav":     f.get("totalNetAsset") or 0,
                "manager":       f.get("manager") or f.get("Manager") or "",
            })

        return result


# --------------------------------------------------------------------------- #
#  DataAggregator — merges TSETMC price + FIPIRAN NAV                        #
# --------------------------------------------------------------------------- #

class DataAggregator:
    """Combines data from TSETMC (price) and FIPIRAN (NAV)."""

    def __init__(self):
        self.tsetmc  = TSETMCFetcher()
        self.fipiran = FIPIRANFetcher()

    def fetch_all(self, use_fipiran_fallback: bool = True) -> list[dict]:
        """Fetch all fund data.

        Strategy:
        1. TSETMC provides price + order book (and NAV when available).
        2. FIPIRAN fills in any missing NAVs as fallback.
        """
        logger.info("Fetching price data from TSETMC...")
        results = self.tsetmc.fetch_all_fund_data()

        if not use_fipiran_fallback:
            return results

        funds_missing_nav = [r for r in results if r["nav_data"] is None]
        if not funds_missing_nav:
            return results

        logger.info(
            "%d fund(s) still missing NAV — fetching from FIPIRAN...",
            len(funds_missing_nav),
        )
        fipiran_funds = self.fipiran.get_fixed_income_funds()
        if not fipiran_funds:
            logger.warning("FIPIRAN returned no data")
            return results

        fipiran_map = {f["symbol"]: f for f in fipiran_funds}

        for result in results:
            if result["nav_data"] is not None:
                continue
            fip = fipiran_map.get(result["symbol"])
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

        return results

    def discover_new_funds(self, keywords: list[str] = None) -> list[dict]:
        """Search TSETMC for fixed-income funds not in the config list."""
        if keywords is None:
            keywords = ["صندوق", "درآمد", "ثابت"]

        known_codes = {f["ins_code"] for f in FIXED_INCOME_ETFS if f["ins_code"]}
        new_funds = []

        for kw in keywords:
            for r in self.tsetmc.search_instrument(kw):
                if r["ins_code"] not in known_codes:
                    new_funds.append(r)
                    known_codes.add(r["ins_code"])

        return new_funds
