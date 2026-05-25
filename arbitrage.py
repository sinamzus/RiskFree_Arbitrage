"""Arbitrage opportunity detection for fixed-income ETF funds."""

import logging
from dataclasses import dataclass, field
from typing import Optional

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
from intraday_context import IntraydayContext, qualify_signal

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
    signal: str                  # "BUY", "BUY_WEAK", "SELL", "SELL_WEAK", "HOLD"
    actionable: bool
    reasons: list[str] = field(default_factory=list)
    intraday: Optional[IntraydayContext] = field(default=None)
    # ── Order-book tradability (populated by run_scan when available) ──────
    tradable: bool       = False   # True if book depth supports the signal
    tradable_volume: int = 0       # units available at profitable price levels
    tradable_value: float= 0.0     # Rial value of the tradable volume
    spread_pct: float    = 0.0     # bid-ask spread % of mid price
    ob_score: float      = 0.0     # 0-100 composite order-book quality score
    tradability_reason: str = ""   # brief explanation for UI tooltip


def analyze_fund(fund_data: dict) -> Optional["ArbitrageOpportunity"]:
    """Analyze a single fund for arbitrage opportunities.

    Premium arbitrage (market price > NAV):
      - Create new units at issue_nav, sell on market
      - Profit = market_price - issue_nav - costs

    Discount arbitrage (market price < NAV):
      - Buy on market, redeem at cancel_nav
      - Profit = cancel_nav - market_price - costs

    If ``fund_data["intraday_context"]`` is set (an :class:`IntraydayContext`),
    the base signal is further qualified:
      - BUY  + narrowing trend  → BUY_WEAK   (discount reversing)
      - SELL + narrowing trend  → SELL_WEAK  (premium fading)
      A WEAK signal is still reported but ``actionable`` is set False
      so it appears in the watchlist, not the action list.
    """
    price_data = fund_data.get("price_data")
    nav_data = fund_data.get("nav_data")
    order_book = fund_data.get("order_book")
    ctx: Optional[IntraydayContext] = fund_data.get("intraday_context")

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

    # ── Intraday trend qualification ──────────────────────────────────────
    if ctx is not None:
        signal, trend_reasons = qualify_signal(signal, ctx)
        reasons.extend(trend_reasons)
        # A WEAK signal means the trend opposes the position — mark not actionable
        if signal in ("BUY_WEAK", "SELL_WEAK"):
            actionable = False
    # ─────────────────────────────────────────────────────────────────────

    # ── Order-book tradability ────────────────────────────────────────────
    td = fund_data.get("tradability")
    if td is None and order_book:
        # Compute on-the-fly if main.py didn't pre-compute it
        from orderbook import compute_tradability
        nav_for_arb = cancel_nav if signal in ("BUY", "BUY_WEAK") else issue_nav
        if nav_for_arb > 0:
            dir_ = "BUY" if signal in ("BUY", "BUY_WEAK") else "SELL"
            td = compute_tradability(dir_, nav_for_arb, order_book)

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
        intraday=ctx,
        tradable          = td.tradeable          if td else False,
        tradable_volume   = td.executable_volume  if td else 0,
        tradable_value    = td.executable_value   if td else 0.0,
        spread_pct        = td.spread_pct         if td else 0.0,
        ob_score          = td.book_depth_score   if td else 0.0,
        tradability_reason= td.reason             if td else "",
    )


def scan_all(fund_data_list: list[dict]) -> list["ArbitrageOpportunity"]:
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


def filter_actionable(opportunities: list["ArbitrageOpportunity"]) -> list["ArbitrageOpportunity"]:
    """Return only actionable opportunities."""
    return [o for o in opportunities if o.actionable]
