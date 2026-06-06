"""Intraday backtest engine for the fixed-income ETF arbitrage strategy.

Strategy under test (long-only, secondary market round-trips)
-------------------------------------------------------------
The scanner suggests a BUY when the market price trades at a *discount* to NAV
and a SELL when it trades at a *premium*. In a long-only world you can only:

  1. ENTER  — buy units on-exchange when the best ask sits below
              NAV × (1 − entry_discount).  You *lift the ask queue*, so the
              fill is limited by the volume actually offered (ask ladder).
  2. EXIT   — sell those units when the best bid rises to
              NAV × (1 + exit_premium).  You *hit the bid queue*, so the fill
              is limited by the volume actually bid (bid ladder).

Every fill is constrained by the real order-book depth recorded in
``intraday_orderbook`` — you can never trade more than the book shows, and the
average fill price walks down/up the ladder as size grows. This makes the
backtest *executable and realistic* rather than assuming infinite liquidity at
the touch.

Costs
-----
Round-trip on the secondary market (no creation/redemption):
  buy  side: BUYER_COMMISSION
  sell side: SELLER_COMMISSION + SELLER_TAX

NAV reference
-------------
Per (symbol, day) NAV is taken from the live OB snapshot's ``nav`` column when
present (days collected live), otherwise from ``daily_history.yesterday_price``
(≈ published NAV for fixed-income ETFs) for that date.

Force close
-----------
A position still open at the last snapshot of the day is liquidated against the
final bid ladder (any price) — an honest worst-case exit, flagged ``eod``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict

from config import (
    BUYER_COMMISSION,
    SELLER_COMMISSION,
    SELLER_TAX,
)

logger = logging.getLogger(__name__)

BUY_COST  = BUYER_COMMISSION                  # paid on buy notional
SELL_COST = SELLER_COMMISSION + SELLER_TAX    # paid on sell notional


# --------------------------------------------------------------------------- #
#  Parameters & results                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class BacktestParams:
    capital: float = 1_000_000_000      # max Rials deployed per open position
    entry_discount_pct: float = 0.30    # enter when ask ≤ NAV·(1−this/100)
    exit_premium_pct: float = 0.30      # exit  when bid ≥ NAV·(1+this/100)
    force_eod: bool = True              # liquidate any open position at day end


@dataclass
class Trade:
    symbol: str
    date: int                # YYYYMMDD
    entry_time: int          # HHMMSS
    exit_time: int           # HHMMSS
    volume: int              # units traded (round trip)
    entry_price: float       # avg fill (Rials/unit)
    exit_price: float        # avg fill (Rials/unit)
    nav: float
    buy_notional: float      # volume·entry_price
    sell_notional: float     # volume·exit_price
    fees: float              # total buy+sell commissions
    net_pnl: float           # after all fees
    net_pct: float           # net_pnl / invested · 100
    hold_secs: int
    exit_reason: str         # "signal" | "eod"


# --------------------------------------------------------------------------- #
#  Order-book ladder helpers                                                    #
# --------------------------------------------------------------------------- #

def _ladder(row: dict, side: str) -> list[tuple[float, int]]:
    """Extract a [(price, volume), …] ladder (best-first) for *side* ∈ {bid, ask}."""
    out = []
    for i in range(1, 6):
        p = row.get(f"{side}{i}_price", 0) or 0
        v = row.get(f"{side}{i}_vol", 0) or 0
        if p > 0 and v > 0:
            out.append((float(p), int(v)))
    return out


def _buy_against_asks(asks, price_ceiling, capital_left):
    """Buy up the ask ladder while price ≤ *price_ceiling*, capped by capital.

    Returns (units, notional). Units are integer (whole ETF units).
    """
    units = 0
    notional = 0.0
    for price, avail in asks:
        if price > price_ceiling:
            break
        affordable = int((capital_left - notional) // price)
        take = min(avail, affordable)
        if take <= 0:
            break
        units += take
        notional += take * price
    return units, notional


def _sell_against_bids(bids, price_floor, units_to_sell):
    """Sell down the bid ladder while price ≥ *price_floor*, up to units_to_sell.

    Returns (units, notional).
    """
    units = 0
    notional = 0.0
    for price, avail in bids:
        if price < price_floor:
            break
        take = min(avail, units_to_sell - units)
        if take <= 0:
            break
        units += take
        notional += take * price
    return units, notional


def _secs(hhmmss: int) -> int:
    t = int(hhmmss)
    return (t // 10000) * 3600 + ((t // 100) % 100) * 60 + (t % 100)


# --------------------------------------------------------------------------- #
#  Per-day simulation                                                           #
# --------------------------------------------------------------------------- #

def _simulate_day(symbol: str, date_int: int, nav: float,
                  snaps: list[dict], p: BacktestParams) -> list[Trade]:
    """Run the long-only round-trip state machine over one day's OB snapshots."""
    if nav <= 0 or not snaps:
        return []

    entry_ceiling = nav * (1 - p.entry_discount_pct / 100.0)
    exit_floor    = nav * (1 + p.exit_premium_pct / 100.0)

    trades: list[Trade] = []
    position = 0            # units held
    buy_notional = 0.0      # cost basis of the held units
    entry_time = 0

    def _close(exit_units, sell_notional, t_exit, reason):
        nonlocal position, buy_notional
        frac = exit_units / position if position else 0
        cost_part = buy_notional * frac
        buy_fee  = cost_part * BUY_COST
        sell_fee = sell_notional * SELL_COST
        invested = cost_part + buy_fee
        net = (sell_notional - sell_fee) - invested
        trades.append(Trade(
            symbol=symbol, date=date_int,
            entry_time=entry_time, exit_time=t_exit,
            volume=int(exit_units),
            entry_price=round(cost_part / exit_units, 2) if exit_units else 0,
            exit_price=round(sell_notional / exit_units, 2) if exit_units else 0,
            nav=round(nav, 2),
            buy_notional=round(cost_part, 0),
            sell_notional=round(sell_notional, 0),
            fees=round(buy_fee + sell_fee, 0),
            net_pnl=round(net, 0),
            net_pct=round(net / invested * 100, 4) if invested else 0,
            hold_secs=max(0, _secs(t_exit) - _secs(entry_time)),
            exit_reason=reason,
        ))
        position -= exit_units
        buy_notional -= cost_part

    for snap in snaps:
        asks = _ladder(snap, "ask")
        bids = _ladder(snap, "bid")
        best_ask = asks[0][0] if asks else 0
        best_bid = bids[0][0] if bids else 0
        t = int(snap.get("time", 0))

        if position == 0:
            # ── Entry: market offered below the discount threshold ──────────
            if best_ask > 0 and best_ask <= entry_ceiling:
                units, notional = _buy_against_asks(asks, entry_ceiling, p.capital)
                if units > 0:
                    position = units
                    buy_notional = notional
                    entry_time = t
        else:
            # ── Exit: market bidding above the premium threshold ────────────
            if best_bid > 0 and best_bid >= exit_floor:
                units, notional = _sell_against_bids(bids, exit_floor, position)
                if units > 0:
                    _close(units, notional, t, "signal")

    # ── Force-close any open position at the final snapshot ────────────────
    if position > 0 and p.force_eod:
        last = snaps[-1]
        bids = _ladder(last, "bid")
        t = int(last.get("time", 0))
        if bids:
            # Liquidate at any price (walk the whole ladder)
            units, notional = _sell_against_bids(bids, 0, position)
            if units < position:
                # Book too thin — value the remainder at the worst available bid
                rem = position - units
                notional += rem * bids[-1][0]
                units = position
            _close(units, notional, t, "eod")

    return trades


# --------------------------------------------------------------------------- #
#  Public API                                                                  #
# --------------------------------------------------------------------------- #

def _summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"trade_count": 0, "win_count": 0, "loss_count": 0,
                "win_rate": 0, "total_net_pnl": 0, "total_invested": 0,
                "total_return_pct": 0, "avg_net_pct": 0,
                "best_pct": 0, "worst_pct": 0, "eod_count": 0,
                "avg_hold_min": 0, "total_fees": 0}
    invested = sum(t.buy_notional + t.buy_notional * BUY_COST for t in trades)
    net = sum(t.net_pnl for t in trades)
    wins = [t for t in trades if t.net_pnl > 0]
    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_net_pnl": round(net, 0),
        "total_invested": round(invested, 0),
        "total_return_pct": round(net / invested * 100, 4) if invested else 0,
        "avg_net_pct": round(sum(t.net_pct for t in trades) / len(trades), 4),
        "best_pct": round(max(t.net_pct for t in trades), 4),
        "worst_pct": round(min(t.net_pct for t in trades), 4),
        "eod_count": sum(1 for t in trades if t.exit_reason == "eod"),
        "avg_hold_min": round(sum(t.hold_secs for t in trades) / len(trades) / 60, 1),
        "total_fees": round(sum(t.fees for t in trades), 0),
    }


def run_backtest(db, symbol: str,
                 start_date: int | None = None,
                 end_date: int | None = None,
                 params: BacktestParams | None = None) -> dict:
    """Backtest the long-only round-trip strategy for *symbol*.

    Parameters
    ----------
    db          : Database — provides OB history + daily NAV
    symbol      : fund symbol
    start_date  : YYYYMMDD (inclusive) or None for all available
    end_date    : YYYYMMDD (inclusive) or None for all available
    params      : BacktestParams (defaults applied if None)

    Returns a dict: {symbol, params, days_tested, trades:[…], summary:{…}}.
    """
    p = params or BacktestParams()

    # NAV per day from daily history (yesterday_price ≈ NAV)
    nav_by_date: dict[int, float] = {}
    for row in db.get_daily_history(symbol, days=400):
        d = row["date"]
        nav = row.get("yesterday_price") or row.get("close_price") or 0
        if nav > 0:
            nav_by_date[d] = float(nav)

    dates = db.get_ob_dates(symbol)
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]

    all_trades: list[Trade] = []
    days_tested = 0
    for date_int in dates:
        snaps = db.get_orderbook_history(symbol, date_int, limit=5000)
        if not snaps:
            continue
        # Prefer the snapshot NAV (live days) else fall back to daily NAV
        snap_nav = next((s.get("nav") for s in snaps if (s.get("nav") or 0) > 0), 0)
        nav = float(snap_nav) if snap_nav else nav_by_date.get(date_int, 0)
        if nav <= 0:
            logger.debug("backtest %s %d: no NAV — skipping", symbol, date_int)
            continue
        days_tested += 1
        all_trades.extend(_simulate_day(symbol, date_int, nav, snaps, p))

    return {
        "symbol": symbol,
        "params": asdict(p),
        "days_tested": days_tested,
        "trades": [asdict(t) for t in all_trades],
        "summary": _summarize(all_trades),
    }
