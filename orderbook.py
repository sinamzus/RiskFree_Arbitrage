"""Order-book tradability analysis for fixed-income ETF arbitrage.

Core question answered here:
  Given a BUY or SELL signal (price diverged from NAV), can an arbitrageur
  actually *execute* the required market order at a profitable price, or is
  the order book too thin / spread too wide to support a real trade?

Terminology
-----------
BUY arb   – market price is at a *discount* to NAV.
            Strategy: buy units on-exchange → redeem at cancel_nav (T+2..T+4).
            Side executed: ASK (you lift the sell queue).

SELL arb  – market price is at a *premium* to NAV.
            Strategy: create units at issue_nav → sell on-exchange (T+1..T+3).
            Side executed: BID (you hit the buy queue).

Profitable threshold
--------------------
BUY  profitable if:  ask_price  < cancel_nav × (1 – total_buy_cost)
SELL profitable if:  bid_price  > issue_nav  × (1 + total_sell_cost)

where total_buy_cost  = BUYER_COMMISSION  + REDEMPTION_FEE
      total_sell_cost = SELLER_COMMISSION + SELLER_TAX + CREATION_FEE
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Trading cost constants (from config, or use defaults) ─────────────────────
try:
    from config import (
        BUYER_COMMISSION,
        SELLER_COMMISSION,
        SELLER_TAX,
        CREATION_FEE,
        REDEMPTION_FEE,
    )
except ImportError:
    BUYER_COMMISSION  = 0.00145
    SELLER_COMMISSION = 0.00145
    SELLER_TAX        = 0.0
    CREATION_FEE      = 0.001
    REDEMPTION_FEE    = 0.001

TOTAL_BUY_COST  = BUYER_COMMISSION  + REDEMPTION_FEE
TOTAL_SELL_COST = SELLER_COMMISSION + SELLER_TAX + CREATION_FEE


@dataclass
class TradabilityResult:
    """
    Whether a signal can be *executed* against the current order book.

    Attributes
    ----------
    tradeable : bool
        True if the book has profitable depth (executable_volume > 0).
    direction : str
        "BUY" | "SELL".
    nav : float
        NAV reference used for calculations.
    threshold_price : float
        The price boundary below (BUY) or above (SELL) which execution is
        still profitable after all costs.
    best_executable_price : float
        The best price available in the book on the execution side
        (best ask for BUY, best bid for SELL).  0 if book is empty.
    executable_volume : int
        Total units available in the book at prices that remain profitable.
    executable_value : float
        Approximate Toman value of executable volume
        (executable_volume × avg_fill_price).
    avg_fill_price : float
        Volume-weighted average fill price across profitable levels.
    slippage_pct : float
        (avg_fill_price / threshold_price − 1) × 100.
        Negative for BUY (you pay below threshold), positive for SELL
        (you receive above threshold).  Practical slippage vs threshold.
    spread_pct : float
        Bid-ask spread as % of mid price.  0 if one side is empty.
    book_depth_score : float
        Composite liquidity score 0-100.
        100 → deep, tight spread, high executable volume.
        0   → empty book.
    reason : str
        Human-readable summary for the UI tooltip.
    levels : list[dict]
        Raw 5-level bid/ask snapshot used for calculation (for UI display).
    """
    tradeable            : bool
    direction            : str
    nav                  : float
    threshold_price      : float
    best_executable_price: float
    executable_volume    : int
    executable_value     : float
    avg_fill_price       : float
    slippage_pct         : float
    spread_pct           : float
    book_depth_score     : float
    reason               : str
    levels               : dict = field(default_factory=dict)


def compute_tradability(
    direction: str,
    nav: float,
    order_book: Optional[dict],
    position_value: float = 50_000_000,   # target position in Rials (5M Toman)
) -> TradabilityResult:
    """Compute whether an arb signal is executable against *order_book*.

    Parameters
    ----------
    direction : "BUY" | "SELL"
        BUY  → discount arb, executing against the ask queue.
        SELL → premium arb, executing against the bid queue.
    nav : float
        For BUY  use cancel_nav  (the price you'll redeem at).
        For SELL use issue_nav   (the price you'll create at).
    order_book : dict | None
        {"bids": [{"price": p, "volume": v, "count": c}, ...x5],
         "asks": [{"price": p, "volume": v, "count": c}, ...x5]}
        Levels must be sorted best-first (highest bid, lowest ask).
    position_value : float
        Target position size in Rials.  Used only to interpret executable %
        of intended position.

    Returns
    -------
    TradabilityResult
    """
    _empty = TradabilityResult(
        tradeable=False, direction=direction, nav=nav,
        threshold_price=0, best_executable_price=0,
        executable_volume=0, executable_value=0,
        avg_fill_price=0, slippage_pct=0, spread_pct=0,
        book_depth_score=0,
        reason="اردربوک خالی است",
        levels={},
    )

    if not order_book or nav <= 0:
        return _empty

    bids = order_book.get("bids", []) or []
    asks = order_book.get("asks", []) or []

    # ── Spread ────────────────────────────────────────────────────────────────
    best_bid_price = bids[0]["price"] if bids and bids[0]["price"] > 0 else 0
    best_ask_price = asks[0]["price"] if asks and asks[0]["price"] > 0 else 0
    spread_pct = 0.0
    if best_bid_price > 0 and best_ask_price > 0:
        mid = (best_bid_price + best_ask_price) / 2
        spread_pct = (best_ask_price - best_bid_price) / mid * 100

    _empty.spread_pct = spread_pct
    _empty.levels = {"bids": bids, "asks": asks}

    # ── Threshold prices ──────────────────────────────────────────────────────
    if direction == "BUY":
        # Execute against asks; profitable if ask < cancel_nav × (1 − cost)
        threshold = nav * (1 - TOTAL_BUY_COST)
        exec_side = asks
    else:
        # Execute against bids; profitable if bid > issue_nav × (1 + cost)
        threshold = nav * (1 + TOTAL_SELL_COST)
        exec_side = bids

    if not exec_side:
        _empty.threshold_price = threshold
        _empty.reason = "طرف مقابل اردربوک خالی است"
        return _empty

    best_exec_price = exec_side[0]["price"] if exec_side[0]["price"] > 0 else 0

    # ── Walk the book to find executable volume ────────────────────────────────
    exec_volume = 0
    exec_value  = 0.0
    for level in exec_side:
        p = level.get("price", 0)
        v = level.get("volume", 0)
        if p <= 0 or v <= 0:
            continue
        if direction == "BUY" and p > threshold:
            break    # this ask level is above our break-even → not profitable
        if direction == "SELL" and p < threshold:
            break    # this bid level is below our break-even → not profitable
        exec_volume += v
        exec_value  += v * p

    avg_fill = exec_value / exec_volume if exec_volume > 0 else 0

    # ── Slippage vs threshold ─────────────────────────────────────────────────
    if threshold > 0 and avg_fill > 0:
        slippage_pct = (avg_fill / threshold - 1) * 100
    else:
        slippage_pct = 0.0

    # ── Book depth score (0-100) ──────────────────────────────────────────────
    # Factors: executable volume (vs position), spread tightness
    vol_score = min(exec_volume * avg_fill / position_value, 1.0) * 70  # 0-70
    spread_score = max(0, 1 - spread_pct / 0.5) * 30                    # 0-30 (0 if spread ≥ 0.5%)
    book_depth_score = vol_score + spread_score

    tradeable = exec_volume > 0

    # ── Human-readable reason ─────────────────────────────────────────────────
    if tradeable:
        val_m = exec_value / 1_000_000   # in Tomans
        if direction == "BUY":
            reason = (
                f"قابل اجرا: {exec_volume:,} واحد در دسترس "
                f"(میانگین {avg_fill:,.0f} ریال) — "
                f"ارزش: {val_m:.1f}M تومان"
            )
        else:
            reason = (
                f"قابل اجرا: {exec_volume:,} واحد در دسترس "
                f"(میانگین {avg_fill:,.0f} ریال) — "
                f"ارزش: {val_m:.1f}M تومان"
            )
    else:
        if best_exec_price > 0:
            if direction == "BUY":
                reason = (
                    f"بهترین فروشنده {best_exec_price:,.0f} ریال "
                    f"(بالاتر از سقف سودآور {threshold:,.0f} ریال)"
                )
            else:
                reason = (
                    f"بهترین خریدار {best_exec_price:,.0f} ریال "
                    f"(پایین‌تر از کف سودآور {threshold:,.0f} ریال)"
                )
        else:
            reason = "اردربوک طرف مقابل خالی است"

    return TradabilityResult(
        tradeable            = tradeable,
        direction            = direction,
        nav                  = nav,
        threshold_price      = threshold,
        best_executable_price= best_exec_price,
        executable_volume    = exec_volume,
        executable_value     = exec_value,
        avg_fill_price       = avg_fill,
        slippage_pct         = slippage_pct,
        spread_pct           = spread_pct,
        book_depth_score     = round(book_depth_score, 1),
        reason               = reason,
        levels               = {"bids": bids, "asks": asks},
    )


def orderbook_from_db_row(row: dict) -> Optional[dict]:
    """Reconstruct a {"bids": [...], "asks": [...]} dict from a DB row."""
    bids, asks = [], []
    for i in range(1, 6):
        bp = row.get(f"bid{i}_price", 0) or 0
        bv = row.get(f"bid{i}_vol",   0) or 0
        bc = row.get(f"bid{i}_cnt",   0) or 0
        ap = row.get(f"ask{i}_price", 0) or 0
        av = row.get(f"ask{i}_vol",   0) or 0
        ac = row.get(f"ask{i}_cnt",   0) or 0
        bids.append({"price": bp, "volume": bv, "count": bc})
        asks.append({"price": ap, "volume": av, "count": ac})
    return {"bids": bids, "asks": asks}
