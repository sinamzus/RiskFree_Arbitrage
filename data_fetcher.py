"""Fetch real-time price and NAV data for fixed-income ETFs from TSETMC and FIPIRAN."""

import time
import logging
from typing import Optional

import requests

from config import (
    TSETMC_CDN,
    TSETMC_OLD,
    FIPIRAN_API,
    FIPIRAN_WEB,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    FIXED_INCOME_ETFS,
)

logger = logging.getLogger(__name__)


class TSETMCFetcher:
    """Fetches data from tsetmc.com (Tehran Securities Exchange)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = "https://www.tsetmc.com/"

    def _get(self, url: str) -> Optional[dict]:
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("TSETMC request failed for %s: %s", url, e)
            return None
        except ValueError:
            logger.warning("Invalid JSON from %s", url)
            return None

    def get_closing_price_info(self, ins_code: str) -> Optional[dict]:
        """Get current closing price data for an instrument.

        Returns dict with keys: last_price, close_price, open_price, high, low,
        volume, value, trade_count, yesterday_price, etc.
        """
        url = f"{TSETMC_CDN}/ClosingPrice/GetClosingPriceInfo/{ins_code}"
        data = self._get(url)
        if not data or "closingPriceInfo" not in data:
            return None

        info = data["closingPriceInfo"]
        return {
            "last_price": info.get("pDrCotVal", 0),
            "close_price": info.get("pClosing", 0),
            "open_price": info.get("priceFirst", 0),
            "high_price": info.get("priceMax", 0),
            "low_price": info.get("priceMin", 0),
            "yesterday_price": info.get("priceYesterday", 0),
            "volume": info.get("qTotTran5J", 0),
            "value": info.get("qTotCap", 0),
            "trade_count": info.get("zTotTran", 0),
        }

    def get_fund_nav(self, ins_code: str) -> Optional[dict]:
        """Get NAV data for an ETF fund from TSETMC.

        Returns dict with: nav_per_unit, statistical_nav, total_nav,
        fund_units, cancel_nav, issue_nav, date.
        """
        url = f"{TSETMC_CDN}/Fund/GetFundByInsCode/{ins_code}"
        data = self._get(url)
        if not data or "fund" not in data:
            return None

        fund = data["fund"]
        return {
            "nav_per_unit": fund.get("cancelNav", 0),
            "statistical_nav": fund.get("statisticalNav", 0),
            "issue_nav": fund.get("issueNav", 0),
            "cancel_nav": fund.get("cancelNav", 0),
            "total_nav": fund.get("totalNetAsset", 0),
            "fund_units": fund.get("fundUnit", 0),
            "nav_date": fund.get("dEven", ""),
        }

    def get_instrument_info(self, ins_code: str) -> Optional[dict]:
        """Get instrument metadata."""
        url = f"{TSETMC_CDN}/Instrument/GetInstrumentInfo/{ins_code}"
        data = self._get(url)
        if not data or "instrumentInfo" not in data:
            return None

        info = data["instrumentInfo"]
        return {
            "symbol": info.get("lVal18AFC", "").strip(),
            "full_name": info.get("lVal30", "").strip(),
            "sector": info.get("sector", {}).get("lSecVal", ""),
            "market": info.get("cgrValCotTitle", ""),
            "base_volume": info.get("baseVol", 0),
        }

    def get_best_limits(self, ins_code: str) -> Optional[dict]:
        """Get order book (best bid/ask) data."""
        url = f"{TSETMC_CDN}/BestLimits/{ins_code}"
        data = self._get(url)
        if not data or "bestLimits" not in data:
            return None

        limits = data["bestLimits"]
        bids = []
        asks = []
        for row in limits:
            if row.get("number", 0) <= 5:
                bids.append({
                    "price": row.get("pMeDem", 0),
                    "volume": row.get("qTitMeDem", 0),
                    "count": row.get("zOrdMeDem", 0),
                })
                asks.append({
                    "price": row.get("pMeOf", 0),
                    "volume": row.get("qTitMeOf", 0),
                    "count": row.get("zOrdMeOf", 0),
                })

        return {"bids": bids, "asks": asks}

    def search_instrument(self, keyword: str) -> list[dict]:
        """Search for instruments by keyword."""
        url = f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{keyword}"
        data = self._get(url)
        if not data or "instrumentSearch" not in data:
            return []

        results = []
        for item in data["instrumentSearch"]:
            results.append({
                "ins_code": item.get("insCode", ""),
                "symbol": item.get("lVal18AFC", "").strip(),
                "full_name": item.get("lVal30", "").strip(),
            })
        return results

    def fetch_all_fund_data(self, funds: list[dict] = None, delay: float = 0.5) -> list[dict]:
        """Fetch price + NAV for all configured funds.

        Returns list of dicts with combined price, NAV, and instrument info.
        """
        if funds is None:
            funds = FIXED_INCOME_ETFS

        results = []
        total = len(funds)

        for i, fund in enumerate(funds):
            ins_code = fund["ins_code"]
            symbol = fund["symbol"]
            logger.info("[%d/%d] Fetching data for %s ...", i + 1, total, symbol)

            entry = {
                "symbol": symbol,
                "name": fund["name"],
                "ins_code": ins_code,
                "price_data": None,
                "nav_data": None,
                "order_book": None,
                "error": None,
            }

            price = self.get_closing_price_info(ins_code)
            if price:
                entry["price_data"] = price
            else:
                entry["error"] = "Price data unavailable"

            time.sleep(delay * 0.3)

            nav = self.get_fund_nav(ins_code)
            if nav:
                entry["nav_data"] = nav
            else:
                if entry["error"]:
                    entry["error"] += "; NAV data unavailable"
                else:
                    entry["error"] = "NAV data unavailable"

            time.sleep(delay * 0.3)

            order_book = self.get_best_limits(ins_code)
            if order_book:
                entry["order_book"] = order_book

            results.append(entry)

            if i < total - 1:
                time.sleep(delay * 0.4)

        return results


class FIPIRANFetcher:
    """Fetches fund data from fipiran.ir as a fallback/supplement."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.session.headers["Referer"] = "https://www.fipiran.ir/"

    def _get(self, url: str) -> Optional[dict | list]:
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("FIPIRAN request failed for %s: %s", url, e)
            return None
        except ValueError:
            logger.warning("Invalid JSON from %s", url)
            return None

    def get_fund_list(self) -> list[dict]:
        """Get list of all funds from FIPIRAN.

        Returns list of fund dicts with NAV, returns, and metadata.
        """
        url = f"{FIPIRAN_API}/fund/fundlist"
        data = self._get(url)
        if not data or not isinstance(data, list):
            return []
        return data

    def get_fund_compare(self) -> list[dict]:
        """Get fund comparison data with NAV and performance."""
        url = f"{FIPIRAN_WEB}/DataService/FundCompare"
        data = self._get(url)
        if not data or not isinstance(data, list):
            return []
        return data

    def get_fixed_income_funds(self) -> list[dict]:
        """Filter and return only fixed-income ETF funds."""
        funds = self.get_fund_list()
        fixed_income = []
        for f in funds:
            fund_type = f.get("typeOfFund", "")
            is_etf = f.get("isETF", False)
            if "درآمد ثابت" in str(fund_type) or "Fixed" in str(fund_type):
                if is_etf:
                    fixed_income.append({
                        "symbol": f.get("symbol", "").strip(),
                        "name": f.get("name", "").strip(),
                        "nav": f.get("cancelNav", 0),
                        "issue_nav": f.get("issueNav", 0),
                        "statistical_nav": f.get("statisticalNav", 0),
                        "nav_date": f.get("date", ""),
                        "total_units": f.get("fundUnit", 0),
                        "total_nav": f.get("totalNetAsset", 0),
                        "fund_type": fund_type,
                        "manager": f.get("manager", ""),
                    })
        return fixed_income


class DataAggregator:
    """Combines data from TSETMC and FIPIRAN for comprehensive fund analysis."""

    def __init__(self):
        self.tsetmc = TSETMCFetcher()
        self.fipiran = FIPIRANFetcher()

    def fetch_all(self, use_fipiran_fallback: bool = True) -> list[dict]:
        """Fetch all fund data, using FIPIRAN as fallback for missing NAVs."""
        logger.info("Fetching fund data from TSETMC...")
        results = self.tsetmc.fetch_all_fund_data()

        if use_fipiran_fallback:
            funds_missing_nav = [r for r in results if r["nav_data"] is None]
            if funds_missing_nav:
                logger.info(
                    "%d funds missing NAV from TSETMC, trying FIPIRAN...",
                    len(funds_missing_nav),
                )
                fipiran_funds = self.fipiran.get_fixed_income_funds()
                fipiran_map = {f["symbol"]: f for f in fipiran_funds}

                for result in results:
                    if result["nav_data"] is None:
                        fip = fipiran_map.get(result["symbol"])
                        if fip:
                            result["nav_data"] = {
                                "nav_per_unit": fip["nav"],
                                "statistical_nav": fip["statistical_nav"],
                                "issue_nav": fip["issue_nav"],
                                "cancel_nav": fip["nav"],
                                "total_nav": fip["total_nav"],
                                "fund_units": fip["total_units"],
                                "nav_date": fip["nav_date"],
                            }
                            result["nav_source"] = "fipiran"
                            result["error"] = None

        return results

    def discover_new_funds(self, keywords: list[str] = None) -> list[dict]:
        """Search TSETMC for fixed-income funds not in the config list."""
        if keywords is None:
            keywords = ["صندوق", "درآمد", "ثابت"]

        known_codes = {f["ins_code"] for f in FIXED_INCOME_ETFS}
        new_funds = []

        for kw in keywords:
            results = self.tsetmc.search_instrument(kw)
            for r in results:
                if r["ins_code"] not in known_codes:
                    new_funds.append(r)
                    known_codes.add(r["ins_code"])

        return new_funds
