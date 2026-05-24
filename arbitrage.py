"""Arbitrage opportunity detection for fixed-income ETF funds."""

import logging
from dataclasses import dataclass, field

from config import (
    BUYER_COMMISSION,
    SELLER_COMMISSION,
    SELLER_TAX,
    CREATION_FEE,
    REDEMPTION_FEE,
    MIN_PREMIUM_THRESHOLD,
    MIN_DISCOUNT_THRESHOLD,
    MIN_DAILY_VOLUME,
)

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    symbol: str
    name: str
    market_price: float
    nav: float
    statistical_nav: float
    issue_nav: float
    cancel_nav: float
    premium_discount_pct: float  # positive = premium, negative = discount
    net_profit_pct: float        # after estimated costs
    volume: int
    value: float
    trade_count: int
    best_bid: float
    best_ask: float
    bid_depth: int               # total bid volume at top 5 levels
    ask_depth: int               # total ask volume at top 5 levels
    signal: str                  # "BUY", "SELL", "HOLD"
    actionable: bool
    reasons: list[str] = field(default_factory=list)


def analyze_fund(fund_data: dict) -> ArbitrageOpportunity | None:
    """Analyze a single fund for arbitrage opportunities.

    Premium arbitrage (market price > NAV):
      - Create new units at issue_nav, sell on market
      - Profit = market_price - issue_nav - costs

    Discount arbitrage (market price < NAV):
      - Buy on market, redeem at cancel_nav
      - Profit = cancel_nav - market_price - costs
    """
    price_data = fund_data.get("price_data")
    nav_data = fund_data.get("nav_data")
    order_book = fund_data.get("order_book")

    if not price_data or not nav_data:
        return None

    market_price = price_data.get("last_price", 0)
    close_price = price_data.get("close_price", 0)
    volume = price_data.get("volume", 0)
    value = price_data.get("value", 0)
    trade_count = price_data.get("trade_count", 0)

    effective_price = market_price if market_price > 0 else close_price
    if effective_price <= 0:
        return None

    nav = nav_data.get("cancel_nav", 0)
    statistical_nav = nav_data.get("statistical_nav", 0)
    issue_nav = nav_data.get("issue_nav", 0)
    cancel_nav = nav_data.get("cancel_nav", 0)

    if nav <= 0:
        return None

    premium_discount_pct = ((effective_price - nav) / nav) * 100

    best_bid = 0
    best_ask = 0
    bid_depth = 0
    ask_depth = 0
    if order_book:
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        if bids:
            best_bid = bids[0].get("price", 0)
            bid_depth = sum(b.get("volume", 0) for b in bids)
        if asks:
            best_ask = asks[0].get("price", 0)
            ask_depth = sum(a.get("volume", 0) for a in asks)

    total_buy_cost = BUYER_COMMISSION
    total_sell_cost = SELLER_COMMISSION + SELLER_TAX

    signal = "HOLD"
    net_profit_pct = 0.0
    reasons = []

    if premium_discount_pct < 0:
        # Discount: buy on market + redeem at cancel_nav
        buy_cost_per_unit = effective_price * (1 + total_buy_cost)
        redeem_value = cancel_nav * (1 - REDEMPTION_FEE)
        gross_profit = redeem_value - buy_cost_per_unit
        net_profit_pct = (gross_profit / buy_cost_per_unit) * 100

        if abs(premium_discount_pct) >= MIN_DISCOUNT_THRESHOLD:
            signal = "BUY"
            reasons.append(
                f"تخفیف {abs(premium_discount_pct):.2f}% — خرید از بازار و ابطال"
            )

    elif premium_discount_pct > 0:
        # Premium: create at issue_nav + sell on market
        create_cost = issue_nav * (1 + CREATION_FEE)
        sell_revenue = effective_price * (1 - total_sell_cost)
        gross_profit = sell_revenue - create_cost
        net_profit_pct = (gross_profit / create_cost) * 100

        if premium_discount_pct >= MIN_PREMIUM_THRESHOLD:
            signal = "SELL"
            reasons.append(
                f"صرف {premium_discount_pct:.2f}% — صدور و فروش در بازار"
            )

    actionable = True
    if abs(premium_discount_pct) < max(MIN_PREMIUM_THRESHOLD, MIN_DISCOUNT_THRESHOLD):
        actionable = False
        reasons.append("اختلاف قیمت و NAV کمتر از حد آستانه")

    if net_profit_pct <= 0:
        actionable = False
        reasons.append("سود خالص پس از کسر کارمزد منفی است")

    if volume < MIN_DAILY_VOLUME:
        actionable = False
        reasons.append(f"حجم معاملات ناکافی ({volume:,} < {MIN_DAILY_VOLUME:,})")

    if bid_depth == 0 and ask_depth == 0 and order_book is not None:
        actionable = False
        reasons.append("عمق سفارشات صفر — نقدشوندگی ناکافی")

    return ArbitrageOpportunity(
        symbol=fund_data["symbol"],
        name=fund_data["name"],
        market_price=effective_price,
        nav=nav,
        statistical_nav=statistical_nav,
        issue_nav=issue_nav,
        cancel_nav=cancel_nav,
        premium_discount_pct=round(premium_discount_pct, 3),
        net_profit_pct=round(net_profit_pct, 3),
        volume=volume,
        value=value,
        trade_count=trade_count,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        signal=signal,
        actionable=actionable,
        reasons=reasons,
    )


def scan_all(fund_data_list: list[dict]) -> list[ArbitrageOpportunity]:
    """Analyze all funds and return sorted opportunities."""
    opportunities = []
    for fund_data in fund_data_list:
        opp = analyze_fund(fund_data)
        if opp:
            opportunities.append(opp)
        else:
            logger.warning(
                "Could not analyze %s: missing data", fund_data.get("symbol", "?")
            )

    opportunities.sort(key=lambda o: abs(o.premium_discount_pct), reverse=True)
    return opportunities


def filter_actionable(opportunities: list[ArbitrageOpportunity]) -> list[ArbitrageOpportunity]:
    """Return only actionable opportunities."""
    return [o for o in opportunities if o.actionable]
