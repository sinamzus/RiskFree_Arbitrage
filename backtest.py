"""Intraday backtest engine for the fixed-income ETF arbitrage strategy.

Why this engine does *not* use NAV
----------------------------------
The live scanner ranks BUY/SELL signals against the published NAV. But NAV is
only published **once per day, after the close**, and we never stored an
intraday NAV series — for every back-filled historical day the
``intraday_orderbook.nav`` column is ``0`` and ``daily_history.yesterday_price``
is a single end-of-day number. Back-testing a NAV-referenced rule on history is
therefore impossible to do honestly.

So this engine drops NAV entirely and tests **price/order-book reversion**
strategies that depend only on data we actually recorded tick-by-tick:

  * the top-5 order book (``intraday_orderbook``)          → always available
  * cumulative price/volume (``intraday_price_history``)   → running VWAP
  * raw tick trades (``intraday_trades``)                  → VWAP fallback

These are well suited to fixed-income ETFs, which trade in a *very* tight band:
the price oscillates by a few basis points around its own short-term mean, so a
mean-reversion proxy (VWAP / moving-average / prior close) is a sound fair-value
estimate without ever needing NAV.

Strategies (all NAV-free, long-only, secondary-market round-trips)
-----------------------------------------------------------------
``vwap``        Fair value = running session VWAP (volume-weighted avg traded
                price so far today). BUY when the best ask falls below
                VWAP·(1−entry), SELL when the best bid rises above VWAP·(1+exit).
                Uses intraday_price_history (cum_value/cum_volume), falling back
                to tick trades. Best fit for these funds.

``sma``         Fair value = trailing simple moving average of the order-book
                mid price over the last ``ma_window`` snapshots. Uses ONLY the
                order book, so it works on every back-filled day. BUY below the
                MA by ``entry``, SELL above it by ``exit``.

``prev_close``  Fair value = the *previous* day's published close
                (``daily_history.yesterday_price``) — a known constant for the
                day, the closest NAV-free proxy to "yesterday's NAV". BUY when
                ask ≤ prev_close·(1−entry), SELL when bid ≥ prev_close·(1+exit).

Execution model (shared by every strategy)
------------------------------------------
Every fill is constrained by the *real* recorded order-book depth: you lift the
ask ladder on entry and hit the bid ladder on exit, the average price walks the
ladder as size grows, and size is also capped by ``capital``. You can never
trade more than the book showed. A position still open at the last snapshot is
liquidated against the final bid ladder (any price), flagged ``eod``.

Costs (round-trip, no creation/redemption):
  buy  side: BUYER_COMMISSION
  sell side: SELLER_COMMISSION + SELLER_TAX
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, asdict

from config import (
    BUYER_COMMISSION,
    SELLER_COMMISSION,
    SELLER_TAX,
)

logger = logging.getLogger(__name__)

BUY_COST  = BUYER_COMMISSION                  # paid on buy notional
SELL_COST = SELLER_COMMISSION + SELLER_TAX    # paid on sell notional

# Human-readable catalogue surfaced to the UI.
STRATEGIES = {
    "vwap":       "بازگشت به VWAP — مرجع: میانگین وزنی قیمت معاملات همان روز",
    "sma":        "بازگشت به میانگین متحرک — مرجع: میانگین متحرک قیمت میانی اردربوک",
    "prev_close": "لنگر قیمت پایانی دیروز — مرجع: قیمت پایانی روز قبل",
}


# --------------------------------------------------------------------------- #
#  Parameters & results                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class BacktestParams:
    capital: float = 1_000_000_000      # max Rials deployed per open position
    strategy: str = "vwap"              # vwap | sma | prev_close
    entry_discount_pct: float = 0.15    # enter when ask ≤ ref·(1−this/100)
    exit_premium_pct: float = 0.15      # exit  when bid ≥ ref·(1+this/100)
    ma_window: int = 20                 # snapshots, for the "sma" strategy
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
    ref_price: float         # fair-value reference at entry (NAV-free)
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


def _mid(row: dict) -> float:
    """Order-book mid price = (best_bid + best_ask) / 2, with graceful fallback."""
    b = row.get("bid1_price", 0) or 0
    a = row.get("ask1_price", 0) or 0
    if b > 0 and a > 0:
        return (a + b) / 2.0
    return float(a or b or 0)


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
#  Reference-price series builders (one value per snapshot, NAV-free)           #
# --------------------------------------------------------------------------- #

def _refs_sma(snaps: list[dict], window: int) -> list[float]:
    """Trailing simple moving average of the mid price, aligned to *snaps*.

    Pure order-book — available for every back-filled day. The reference for
    snapshot *i* is the mean of the last ``window`` valid mids up to and
    including *i* (expanding until the window fills).
    """
    window = max(1, int(window))
    dq: deque[float] = deque()
    run = 0.0
    refs: list[float] = []
    for s in snaps:
        m = _mid(s)
        if m > 0:
            dq.append(m)
            run += m
            if len(dq) > window:
                run -= dq.popleft()
            refs.append(run / len(dq))
        else:
            refs.append(refs[-1] if refs else 0.0)
    return refs


def _refs_vwap(db, symbol: str, date_int: int, snaps: list[dict]) -> list[float]:
    """Running session VWAP aligned to each snapshot's time.

    Source order: intraday_price_history (cum_value/cum_volume) → tick trades.
    For each snapshot we use the most recent VWAP at-or-before its time, so the
    reference never peeks into the future.
    """
    series: list[tuple[int, float]] = []

    for r in db.get_intraday_price_history(symbol, date_int):
        cv = r.get("cum_volume", 0) or 0
        cval = r.get("cum_value", 0) or 0
        if cv > 0 and cval > 0:
            series.append((int(r["time"]), cval / cv))

    if not series:  # fall back to reconstructing VWAP from raw ticks
        cum_v = 0
        cum_pv = 0.0
        for t in db.get_intraday_trades(symbol, date_int):
            if t.get("canceled"):
                continue
            v = t.get("volume", 0) or 0
            pr = t.get("price", 0) or 0
            if v <= 0 or pr <= 0:
                continue
            cum_v += v
            cum_pv += v * pr
            series.append((int(t["time"]), cum_pv / cum_v))

    if not series:
        return [0.0] * len(snaps)

    series.sort(key=lambda x: x[0])
    refs: list[float] = []
    j = 0
    last = 0.0
    for s in snaps:
        st = int(s.get("time", 0))
        while j < len(series) and series[j][0] <= st:
            last = series[j][1]
            j += 1
        refs.append(last)
    return refs


def _build_refs(db, symbol: str, date_int: int, snaps: list[dict],
                p: BacktestParams, prev_close: float) -> list[float]:
    """Dispatch to the chosen strategy's reference-price series."""
    if p.strategy == "sma":
        return _refs_sma(snaps, p.ma_window)
    if p.strategy == "prev_close":
        return [float(prev_close)] * len(snaps) if prev_close > 0 else [0.0] * len(snaps)
    # default: vwap
    return _refs_vwap(db, symbol, date_int, snaps)


# --------------------------------------------------------------------------- #
#  Per-day simulation                                                           #
# --------------------------------------------------------------------------- #

def _simulate_day(symbol: str, date_int: int,
                  snaps: list[dict], refs: list[float],
                  p: BacktestParams) -> list[Trade]:
    """Long-only round-trip state machine against a per-snapshot reference."""
    if not snaps:
        return []

    trades: list[Trade] = []
    position = 0            # units held
    buy_notional = 0.0      # cost basis of the held units
    entry_time = 0
    entry_ref = 0.0         # reference price captured at entry (for reporting)

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
            ref_price=round(entry_ref, 2),
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

    for idx, snap in enumerate(snaps):
        ref = refs[idx] if idx < len(refs) else 0.0
        if ref <= 0:
            continue  # no fair-value reference yet — cannot evaluate

        entry_ceiling = ref * (1 - p.entry_discount_pct / 100.0)
        exit_floor    = ref * (1 + p.exit_premium_pct / 100.0)

        asks = _ladder(snap, "ask")
        bids = _ladder(snap, "bid")
        best_ask = asks[0][0] if asks else 0
        best_bid = bids[0][0] if bids else 0
        t = int(snap.get("time", 0))

        if position == 0:
            # ── Entry: best offer sits below fair value by the entry margin ──
            if best_ask > 0 and best_ask <= entry_ceiling:
                units, notional = _buy_against_asks(asks, entry_ceiling, p.capital)
                if units > 0:
                    position = units
                    buy_notional = notional
                    entry_time = t
                    entry_ref = ref
        else:
            # ── Exit: best bid sits above fair value by the exit margin ──────
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
    """Backtest a NAV-free reversion strategy for *symbol* over recorded history.

    Parameters
    ----------
    db          : Database — provides OB history, tick/VWAP data and daily close
    symbol      : fund symbol
    start_date  : YYYYMMDD (inclusive) or None for all available
    end_date    : YYYYMMDD (inclusive) or None for all available
    params      : BacktestParams (defaults applied if None)

    Returns a dict:
        {symbol, strategy, strategy_label, params, days_tested,
         days_skipped, trades:[…], summary:{…}}.
    """
    p = params or BacktestParams()
    if p.strategy not in STRATEGIES:
        p.strategy = "vwap"

    # Previous-day published close per date (only needed for prev_close strategy,
    # but cheap to build and useful for reporting). yesterday_price on day D is
    # the close of the trading day before D — exactly the NAV-free "prior close".
    prev_close_by_date: dict[int, float] = {}
    for row in db.get_daily_history(symbol, days=400):
        d = row["date"]
        yc = row.get("yesterday_price") or 0
        if yc > 0:
            prev_close_by_date[d] = float(yc)

    dates = db.get_ob_dates(symbol)
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]

    all_trades: list[Trade] = []
    days_tested = 0
    days_skipped = 0
    for date_int in dates:
        snaps = db.get_orderbook_history(symbol, date_int, limit=5000)
        if not snaps:
            continue
        prev_close = prev_close_by_date.get(date_int, 0.0)
        refs = _build_refs(db, symbol, date_int, snaps, p, prev_close)
        if not any(r > 0 for r in refs):
            # strategy has no usable reference for this day (e.g. vwap with no
            # tick/price history, or prev_close missing) — skip honestly
            days_skipped += 1
            logger.debug("backtest %s %d: no '%s' reference — skipping",
                         symbol, date_int, p.strategy)
            continue
        days_tested += 1
        all_trades.extend(_simulate_day(symbol, date_int, snaps, refs, p))

    return {
        "symbol": symbol,
        "strategy": p.strategy,
        "strategy_label": STRATEGIES.get(p.strategy, p.strategy),
        "params": asdict(p),
        "days_tested": days_tested,
        "days_skipped": days_skipped,
        "trades": [asdict(t) for t in all_trades],
        "summary": _summarize(all_trades),
    }
