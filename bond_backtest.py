"""Tick-by-tick backtest engine for اخزا (Islamic Treasury Bill) arbitrage.

How اخزا backtesting differs from the fund backtest
---------------------------------------------------
The fund engine (``backtest.py``) tests *single-instrument* mean reversion: a
fund's price reverts to its own short-term VWAP/SMA. That logic is wrong for a
zero-coupon اخزا, whose price has a deterministic upward drift ("pull to par")
— a plain price-reversion rule would just fight that drift.

The correct اخزا signal is **cross-sectional / relative-value**: at any instant
all اخزا across maturities should price on one yield curve (same sovereign
issuer). A series whose yield sits *above* the fitted curve is **cheap** (price
too low) → BUY; when its yield reverts back onto the curve → SELL. The drift is
absorbed by the curve, so the z-spread (yield minus curve-yield) is stationary
and tradeable.

Tick-by-tick reconstruction
---------------------------
To know the curve at an instant we need every series' price at that instant.
For each date we:

  1. Load each series' recorded order-book snapshots (``intraday_orderbook``).
  2. Build a merged, sorted timeline of every snapshot time across all series.
  3. Walk the timeline. At each time *t* take each series' most-recent OB mid
     at-or-before *t* (carry-forward, never peeking ahead), convert price→YTM
     using that series' exact days-to-maturity on that date, and fit the curve
     across all series that have a price.
  4. Per series compute z-spread = (ytm − curve_ytm)·10⁴ bps and run a long-only
     state machine: enter when z ≥ ``entry_bps`` (cheap), exit when z ≤
     ``exit_bps`` (reverted). Fills walk that series' real OB ladder at *t*.

Execution model & costs are shared with the fund engine (see backtest.py):
buy lifts the ask ladder, sell hits the bid ladder, size capped by capital and
by the depth actually recorded, and an open position is liquidated at the final
snapshot (``eod``).
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, asdict

from bonds import (ytm_zero_coupon, fit_yield_curve, eval_curve,
                   days_to_maturity, _solve_3x3)
# Reuse the fund engine's validated execution primitives.
from backtest import _ladder, _buy_against_asks, _sell_against_bids, _mid, _secs

logger = logging.getLogger(__name__)

BUY_COST  = 0.00075   # bond buy commission per side
SELL_COST = 0.00075   # bond sell commission per side

# اخزا (and TSE equities) trade in a single continuous session 09:00–12:30
# Tehran time.  TSETMC's bestLimitsHistory stream also carries pre-opening
# auction quotes (پیش‌گشایش) and other out-of-session noise — sometimes stamped
# as early as 06:00 — whose prices are NOT executable.  Snapshots outside this
# window are dropped so the backtest can never enter/exit off-session.
SESSION_OPEN_HHMMSS  = 90000     # 09:00:00
SESSION_CLOSE_HHMMSS = 123000    # 12:30:00


def _in_session(hhmmss: int) -> bool:
    """True if a HHMMSS time falls within the اخزا trading session."""
    return SESSION_OPEN_HHMMSS <= int(hhmmss) <= SESSION_CLOSE_HHMMSS


# --------------------------------------------------------------------------- #
#  Parameters & results                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class BondBacktestParams:
    capital: float = 1_000_000_000   # max Rials per open position
    entry_bps: float = 50.0          # BUY when z-spread ≥ this (bond is cheap)
    exit_bps: float = 10.0           # SELL when z-spread ≤ this (reverted)
    degree: int = 2                  # yield-curve polynomial degree (1 or 2 only)
    min_curve_points: int = 3        # min series needed to fit a curve at an instant
    step_secs: int = 0               # 0 = every snapshot time; >0 = downsample grid
    force_eod: bool = False          # True = liquidate at day end; False = carry overnight
    include_matured: bool = True     # include اخزا already matured (as of today)
                                     # — on each date they only trade while alive
    buy_fee: float = BUY_COST        # buy-side commission (fraction, e.g. 0.00145)
    sell_fee: float = SELL_COST      # sell-side commission + tax (fraction)
    strategy: str = "zspread"        # "zspread" = mid-based mean-reversion (all-in);
                                     # "outlier" = grab mispriced individual OB orders
    signal_price: str = "exec"       # z-spread signal basis:
                                     # "exec" = decide on EXECUTABLE touch prices
                                     #   (entry vs best ask, exit vs best bid) so a
                                     #   trade only fires when the edge survives the
                                     #   bid-ask spread you must cross — the realistic
                                     #   default;
                                     # "mid" = legacy: decide on the mid price (will
                                     #   happily take trades whose edge is smaller than
                                     #   the spread → structural losses).
    min_exit_profit_bps: float = -1.0  # signal-exit guard. <0 disables it (default,
                                     # bit-identical to legacy). ≥0 = only take a
                                     # *signal* exit when the realised round-trip
                                     # net return (after both fees) clears this many
                                     # bps; otherwise HOLD for a better price.
                                     # 0 = break-even (never sell a signal at a loss).
                                     # Forced exits (eod/final) are never guarded.
    total_capital: float = 0.0       # portfolio cash management. 0 = OFF (legacy:
                                     # every position independently capped by
                                     # `capital`, unlimited concurrent, bit-exact).
                                     # >0 = one shared cash pool of this size: buys
                                     # consume cash, sells return it, and the engine
                                     # can't deploy more than it holds.
    max_position_pct: float = 1.0    # money-management: max fraction of total_capital
                                     # a single position may deploy (0..1). Only used
                                     # when total_capital > 0.


@dataclass
class BondTrade:
    symbol: str
    date: int          # entry date (YYYYMMDD)
    exit_date: int     # exit date (YYYYMMDD) — same as date for intraday
    entry_time: int
    exit_time: int
    volume: int
    entry_price: float
    exit_price: float
    entry_ytm: float          # bond YTM at entry (decimal)
    entry_curve_ytm: float    # fitted curve YTM at entry
    entry_z_bps: float        # z-spread at entry (bps)
    exit_z_bps: float         # z-spread at exit (bps)
    buy_notional: float
    sell_notional: float
    fees: float
    net_pnl: float
    net_pct: float
    hold_secs: int
    exit_reason: str          # "signal" | "eod" | "final"
    cash_out: float = 0.0     # cash actually spent at entry (notional + buy fee)
    cash_in: float = 0.0      # cash actually received at exit (notional − sell fee)


# --------------------------------------------------------------------------- #
#  Per-series carry-forward price cursor                                        #
# --------------------------------------------------------------------------- #

class _SeriesDay:
    """Holds one series' OB snapshots for a date and a carry-forward cursor.

    ``advance_to(t)`` moves the cursor to the latest snapshot whose time is
    ≤ *t* (so we never peek into the future).  Because days-to-maturity is
    constant within a day, the powers x, x², x³, x⁴ are precomputed once, and
    the YTM is **cached** — recomputed only when the order-book mid actually
    changes (most OB updates touch only quantities, not best bid/ask), which
    is the single biggest intraday speed lever.

    The fitted curve always uses the **mid** YTM (``_ytm``).  When
    *signal_exec* is set the cursor ALSO tracks the executable touch prices and
    caches their YTMs (``_ytm_ask`` = what a BUY would actually pay,
    ``_ytm_bid`` = what a SELL would actually receive).  Trading on these
    instead of the mid is what stops the engine entering/exiting trades whose
    z-spread edge is smaller than the bid-ask spread it must cross.
    """

    __slots__ = ("symbol", "face_value", "dtm", "snaps", "_i", "cur",
                 "x", "x2", "x3", "x4", "_ytm", "_price",
                 "_exec", "_bidp", "_askp", "_ytm_ask", "_ytm_bid")

    def __init__(self, symbol: str, face_value: float, dtm: int,
                 snaps: list[dict], signal_exec: bool = False):
        self.symbol = symbol
        self.face_value = face_value
        self.dtm = dtm
        self.snaps = snaps          # sorted ascending by time
        self._i = -1
        self.cur: dict | None = None
        # Precompute powers once (dtm is constant intraday).  Use ** to stay
        # bit-identical with fit_yield_curve / eval_curve which use d**k.
        x = float(dtm)
        self.x  = x
        self.x2 = x ** 2
        self.x3 = x ** 3
        self.x4 = x ** 4
        self._ytm   = 0.0
        self._price = 0.0
        self._exec  = bool(signal_exec)
        self._bidp  = 0.0
        self._askp  = 0.0
        self._ytm_ask = 0.0
        self._ytm_bid = 0.0

    def advance_to(self, t: int) -> bool:
        """Move cursor to latest snapshot ≤ t. Returns True if the YTM changed."""
        moved = False
        snaps = self.snaps
        i = self._i
        n = len(snaps)
        while i + 1 < n and int(snaps[i + 1].get("time", 0)) <= t:
            i += 1
            moved = True
        if not moved:
            return False
        self._i = i
        self.cur = snaps[i]
        p = _mid(self.cur)
        if not self._exec:
            # ── Legacy mid-mode (bit-identical to the original engine) ──
            if p == self._price:
                return False        # mid unchanged → YTM unchanged
            self._price = p
            if p > 0 and self.dtm > 0:
                self._ytm = ytm_zero_coupon(p, self.face_value, self.dtm)
            else:
                self._ytm = 0.0
            return True
        # ── Execution-aware mode: also react to touch-price moves ──
        bids = _ladder(self.cur, "bid")
        asks = _ladder(self.cur, "ask")
        bp = bids[0][0] if bids else 0.0
        ap = asks[0][0] if asks else 0.0
        if p == self._price and bp == self._bidp and ap == self._askp:
            return False            # nothing relevant moved
        self._price, self._bidp, self._askp = p, bp, ap
        fv, dtm = self.face_value, self.dtm
        self._ytm     = ytm_zero_coupon(p,  fv, dtm) if (p  > 0 and dtm > 0) else 0.0
        self._ytm_ask = ytm_zero_coupon(ap, fv, dtm) if (ap > 0 and dtm > 0) else 0.0
        self._ytm_bid = ytm_zero_coupon(bp, fv, dtm) if (bp > 0 and dtm > 0) else 0.0
        return True

    def price(self) -> float:
        return self._price

    def ytm(self) -> float:
        return self._ytm


def _fit_from_sums(n, sx, sx2, sx3, sx4, sy, sxy, sx2y, degree):
    """Solve the polynomial least-squares curve from precomputed sufficient
    statistics — numerically identical to fit_yield_curve but without rebuilding
    the power-sums each call. Returns (a0, a1, a2) with a2=0 for degree-1, or None."""
    if n < 2:
        return None
    deg = degree if degree <= 2 else 2
    if n < deg + 1:
        deg = n - 1
    if deg == 1:
        denom = n * sx2 - sx * sx
        if abs(denom) < 1e-15:
            return (sy / n, 0.0, 0.0)
        return ((sy * sx2 - sxy * sx) / denom,
                (n * sxy - sx * sy) / denom, 0.0)
    A = [[float(n), sx, sx2], [sx, sx2, sx3], [sx2, sx3, sx4]]
    c = _solve_3x3(A, [sy, sxy, sx2y])
    if c[0] == 0.0 and c[1] == 0.0 and c[2] == 0.0:
        # singular degree-2 → fall back to degree-1 (matches fit_yield_curve)
        denom = n * sx2 - sx * sx
        if abs(denom) < 1e-15:
            return (sy / n, 0.0, 0.0)
        return ((sy * sx2 - sxy * sx) / denom,
                (n * sxy - sx * sy) / denom, 0.0)
    return (c[0], c[1], c[2])


# --------------------------------------------------------------------------- #
#  Shared close helpers                                                         #
# --------------------------------------------------------------------------- #

def _calc_hold_secs(entry_date: int, entry_time: int,
                    exit_date: int, exit_time: int) -> int:
    """Calendar seconds from entry to exit (cross-day aware)."""
    if entry_date == exit_date:
        return max(0, _secs(exit_time) - _secs(entry_time))
    from datetime import date as _date
    d1 = _date(entry_date // 10000, (entry_date % 10000) // 100, entry_date % 100)
    d2 = _date(exit_date  // 10000, (exit_date  % 10000) // 100, exit_date  % 100)
    return (d2 - d1).days * 86400 + _secs(exit_time)


def _record_close(sym: str, st: dict,
                  exit_units: int, sell_notional: float,
                  t_exit: int, exit_date: int,
                  exit_z: float, reason: str,
                  p: "BondBacktestParams",
                  trades_out: list,
                  portfolio: dict | None = None) -> None:
    """Build a BondTrade and mutate the position state dict in-place.

    *portfolio* (when given) is the shared cash ledger: this close credits it
    with the realised proceeds (sell notional − sell fee).
    """
    frac = exit_units / st["units"] if st["units"] else 0
    cost_part = st["buy_notional"] * frac
    buy_fee  = cost_part * p.buy_fee
    sell_fee = sell_notional * p.sell_fee
    invested = cost_part + buy_fee
    cash_in  = sell_notional - sell_fee
    net = cash_in - invested
    hold_secs = _calc_hold_secs(st["entry_date"], st["entry_time"], exit_date, t_exit)
    trades_out.append(BondTrade(
        symbol=sym,
        date=st["entry_date"],
        exit_date=exit_date,
        entry_time=st["entry_time"],
        exit_time=t_exit,
        volume=int(exit_units),
        entry_price=round(cost_part / exit_units, 2) if exit_units else 0,
        exit_price=round(sell_notional / exit_units, 2) if exit_units else 0,
        entry_ytm=round(st["entry_ytm"], 6),
        entry_curve_ytm=round(st["entry_curve"], 6),
        entry_z_bps=round(st["entry_z"], 1),
        exit_z_bps=round(exit_z, 1),
        buy_notional=round(cost_part, 0),
        sell_notional=round(sell_notional, 0),
        fees=round(buy_fee + sell_fee, 0),
        net_pnl=round(net, 0),
        net_pct=round(net / invested * 100, 4) if invested else 0,
        hold_secs=hold_secs,
        exit_reason=reason,
        cash_out=round(invested, 0),
        cash_in=round(cash_in, 0),
    ))
    st["units"] -= exit_units
    st["buy_notional"] -= cost_part
    if portfolio is not None:
        portfolio["cash"] += cash_in


def _exit_clears_min(st: dict, exit_units: int, sell_notional: float,
                     p: "BondBacktestParams") -> bool:
    """True if selling *exit_units* for *sell_notional* realises a net return
    (after both buy- and sell-side fees) of at least ``p.min_exit_profit_bps``.

    Always True when the guard is disabled (``min_exit_profit_bps`` < 0), so the
    legacy code path is bit-identical.  Used to make *signal* exits stricter:
    hold the position rather than sell into a price that doesn't clear costs.
    """
    thr = p.min_exit_profit_bps
    if thr < 0:
        return True
    units = st["units"]
    if not units:
        return True
    frac = exit_units / units
    cost_part = st["buy_notional"] * frac
    invested = cost_part + cost_part * p.buy_fee
    if invested <= 0:
        return True
    net = (sell_notional - sell_notional * p.sell_fee) - invested
    return (net / invested) * 10_000.0 >= thr


def _buy_budget(p: "BondBacktestParams", portfolio: dict | None,
                sym: str, pos: dict) -> float:
    """Max *notional* (principal, excl. buy fee) this buy may spend.

    Legacy (portfolio is None): the per-position cap ``capital`` minus whatever
    is already deployed in this symbol — exactly the old behaviour, so results
    stay bit-identical when capital management is off.

    Portfolio mode: the per-position cap is the tighter of ``capital`` and
    ``total_capital × max_position_pct``; on top of that, the buy is bounded by
    the cash actually on hand (reserving the buy fee so cash never goes
    negative).
    """
    cur = pos[sym]["buy_notional"] if sym in pos else 0.0
    if portfolio is None:
        return p.capital - cur
    cap = min(p.capital, p.total_capital * p.max_position_pct) - cur
    cash_cap = portfolio["cash"] / (1.0 + p.buy_fee)   # leave room for the fee
    return min(cap, cash_cap)


# --------------------------------------------------------------------------- #
#  Outlier-order strategy                                                       #
# --------------------------------------------------------------------------- #
#
# Distinct from the z-spread mean-reversion engine: instead of trading on the
# bond's *mid* drifting off the curve (and committing the whole capital), this
# scans the live order book for individual mispriced orders.  When a single ask
# sits far CHEAP of fair value (its implied YTM is above the curve by
# ≥ entry_bps) we lift just that order's quantity (capped by capital) — the size
# is whatever the opportunity offers, large or small.  Symmetrically, while we
# hold a series, a single bid sitting far RICH of fair value (implied YTM below
# the curve by ≤ exit_bps) is hit for its quantity.  Any leftover position is
# still subject to force_eod / end-of-range final close.

def _outlier_step(sym, sd, curve_y, t, date_int, pos,
                  entry_bps, exit_bps, capital, close_fn, p, portfolio=None):
    face, dtm, snap = sd.face_value, sd.dtm, sd.cur

    # ── SELL first: realise rich-bid opportunities on an existing holding ──
    if sym in pos:
        bids = _ladder(snap, "bid")
        if bids:
            held = pos[sym]["units"]
            su = 0
            sn = 0.0
            for price, vol in bids:        # best-first = descending price
                z = (ytm_zero_coupon(price, face, dtm) - curve_y) * 10_000.0
                if z <= exit_bps:          # bid overpays vs fair → sell into it
                    take = vol if vol < (held - su) else (held - su)
                    if take > 0:
                        su += take
                        sn += take * price
                    if su >= held:
                        break
                else:
                    break                  # deeper bids are even less rich
            if su > 0 and _exit_clears_min(pos[sym], su, sn, p):
                ep = sn / su
                ez = (ytm_zero_coupon(ep, face, dtm) - curve_y) * 10_000.0
                close_fn(sym, su, sn, t, ez, "outlier")

    # ── BUY: grab cheap-ask outliers, sized by what's offered (≤ budget) ──
    asks = _ladder(snap, "ask")
    if not asks:
        return
    budget = _buy_budget(p, portfolio, sym, pos)
    if budget <= 0:
        return
    bu = 0
    bn = 0.0
    for price, vol in asks:                # best-first = ascending price
        z = (ytm_zero_coupon(price, face, dtm) - curve_y) * 10_000.0
        if z >= entry_bps:                 # ask is cheap vs fair → lift it
            affordable = int((budget - bn) // price)
            take = vol if vol < affordable else affordable
            if take > 0:
                bu += take
                bn += take * price
        else:
            break                          # deeper asks are even less cheap
    if bu > 0:
        if portfolio is not None:
            portfolio["cash"] -= bn * (1.0 + p.buy_fee)
        if sym in pos:
            pos[sym]["units"] += bu
            pos[sym]["buy_notional"] += bn
        else:
            ep = bn / bu
            ey = ytm_zero_coupon(ep, face, dtm)
            pos[sym] = {
                "units": bu, "buy_notional": bn,
                "entry_date": date_int, "entry_time": t,
                "entry_ytm": ey, "entry_curve": curve_y,
                "entry_z": (ey - curve_y) * 10_000.0,
            }


# --------------------------------------------------------------------------- #
#  Per-date simulation                                                          #
# --------------------------------------------------------------------------- #

def _simulate_bond_day(date_int: int,
                       day_series: dict[str, _SeriesDay],
                       p: BondBacktestParams,
                       carry_pos: dict | None = None,
                       portfolio: dict | None = None,
                       ) -> tuple[list[BondTrade], dict]:
    """Cross-sectional z-spread state machine for one trading date.

    *carry_pos* maps symbol → position dict carried forward from a prior day.
    *portfolio* (when given) is the shared cash ledger threaded across days.
    Returns (trades_closed_today, positions_still_open_after_today).
    Positions still open are passed as carry_pos into the next day's call.
    """
    times: set[int] = set()
    for sd in day_series.values():
        for s in sd.snaps:
            times.add(int(s.get("time", 0)))
    timeline = sorted(t for t in times if t > 0)
    if not timeline:
        # No usable data today — carry all positions unchanged.
        return [], dict(carry_pos) if carry_pos else {}

    if p.step_secs > 0:
        kept, last_bucket = [], -1
        for t in timeline:
            b = _secs(t) // p.step_secs
            if b != last_bucket:
                kept.append(t)
                last_bucket = b
        timeline = kept

    trades: list[BondTrade] = []
    # Initialise from yesterday's carry (shallow copy so mutations stay local).
    pos: dict[str, dict] = {}
    if carry_pos:
        for sym, st in carry_pos.items():
            pos[sym] = dict(st)

    def _close(sym: str, exit_units: int, sell_notional: float,
               t_exit: int, exit_z: float, reason: str) -> None:
        st = pos[sym]
        _record_close(sym, st, exit_units, sell_notional,
                      t_exit, date_int, exit_z, reason, p, trades, portfolio)
        if st["units"] <= 0:
            pos.pop(sym, None)

    # Stable iteration order (== fit_yield_curve's point order) for bit parity.
    series_items = list(day_series.items())
    series_list  = [sd for _, sd in series_items]
    min_pts   = p.min_curve_points
    degree    = p.degree
    entry_bps = p.entry_bps
    exit_bps  = p.exit_bps
    capital   = p.capital
    outlier   = (p.strategy == "outlier")
    exec_mode = (p.signal_price == "exec")

    for t in timeline:
        # Advance cursors; track whether any series' YTM actually moved.
        changed = False
        for sd in series_list:
            if sd.advance_to(t):
                changed = True
        # If no mid changed, the curve and every z-spread are identical to the
        # last processed tick — no new threshold can be crossed → skip entirely.
        if not changed:
            continue

        # Build sufficient statistics in one pass (no pow / ** in the loop).
        n = 0
        sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
        for sd in series_list:
            y = sd._ytm
            if y > 0.0:
                x, x2 = sd.x, sd.x2
                n += 1
                sx += x; sx2 += x2; sx3 += sd.x3; sx4 += sd.x4
                sy += y; sxy += x * y; sx2y += x2 * y
        if n < min_pts:
            continue
        coeffs = _fit_from_sums(n, sx, sx2, sx3, sx4, sy, sxy, sx2y, degree)
        if coeffs is None:
            continue
        a0, a1, a2 = coeffs

        for sym, sd in series_items:
            y = sd._ytm
            if y <= 0.0 or sd.cur is None:
                continue
            curve_y = a0 + a1 * sd.x + a2 * sd.x2

            if outlier:
                _outlier_step(sym, sd, curve_y, t, date_int, pos,
                              entry_bps, exit_bps, capital, _close, p, portfolio)
                continue

            if sym not in pos:
                # Entry signal: in exec mode judge the price a BUY would actually
                # pay (best ask); in mid mode the legacy mid z-spread.
                if exec_mode:
                    ya = sd._ytm_ask
                    if ya <= 0.0:
                        continue
                    z_sig = (ya - curve_y) * 10_000.0
                else:
                    z_sig = (y - curve_y) * 10_000.0
                if z_sig >= entry_bps:
                    asks = _ladder(sd.cur, "ask")
                    if not asks:
                        continue
                    ceiling = asks[-1][0]
                    budget = _buy_budget(p, portfolio, sym, pos)
                    if budget <= 0:
                        continue
                    units, notional = _buy_against_asks(asks, ceiling, budget)
                    if units > 0:
                        if portfolio is not None:
                            portfolio["cash"] -= notional * (1.0 + p.buy_fee)
                        pos[sym] = {
                            "units": units, "buy_notional": notional,
                            "entry_date": date_int, "entry_time": t,
                            "entry_ytm": y, "entry_curve": curve_y, "entry_z": z_sig,
                        }
            else:
                # Exit signal: in exec mode judge the price a SELL would actually
                # receive (best bid); in mid mode the legacy mid z-spread.
                if exec_mode:
                    yb = sd._ytm_bid
                    if yb <= 0.0:
                        continue
                    z_sig = (yb - curve_y) * 10_000.0
                else:
                    z_sig = (y - curve_y) * 10_000.0
                if z_sig <= exit_bps:
                    bids = _ladder(sd.cur, "bid")
                    if not bids:
                        continue
                    floor = bids[-1][0]
                    units, notional = _sell_against_bids(bids, floor, pos[sym]["units"])
                    if units > 0 and _exit_clears_min(pos[sym], units, notional, p):
                        _close(sym, units, notional, t, z_sig, "signal")

    # force_eod: close all positions at each series' last snapshot of the day.
    if p.force_eod:
        for sym in list(pos.keys()):
            sd = day_series.get(sym)
            if sd is None or not sd.snaps:
                continue
            last = sd.snaps[-1]
            bids = _ladder(last, "bid")
            t = int(last.get("time", 0))
            if not bids:
                continue
            held = pos[sym]["units"]
            units, notional = _sell_against_bids(bids, 0, held)
            if units < held:
                notional += (held - units) * bids[-1][0]
                units = held
            _close(sym, units, notional, t, 0.0, "eod")

    # Return (closed trades today, positions that survive into tomorrow)
    return trades, pos


# --------------------------------------------------------------------------- #
#  Capital management & money-weighted return                                  #
# --------------------------------------------------------------------------- #

def _days_between(d0: int, d1: int) -> int:
    """Calendar days between two YYYYMMDD ints (d1 − d0)."""
    from datetime import date
    a = date(d0 // 10000, (d0 % 10000) // 100, d0 % 100)
    b = date(d1 // 10000, (d1 % 10000) // 100, d1 % 100)
    return (b - a).days


def _xirr(flows: list[tuple[int, float]]) -> float:
    """Money-weighted annualised return (XIRR) for dated cash flows.

    *flows* = list of (YYYYMMDD, amount); sign convention is investor-cash:
    money leaving the pocket is negative (a buy), money coming back positive
    (a sell).  Returns the annual rate r solving  Σ aᵢ·(1+r)^(−daysᵢ/365) = 0,
    found by robust bisection.  Returns 0.0 when there is no sign change (no
    well-defined IRR).
    """
    if len(flows) < 2:
        return 0.0
    base = min(d for d, _ in flows)
    ts = [(_days_between(base, d) / 365.0, a) for d, a in flows]

    def npv(r: float) -> float:
        return sum(a / ((1.0 + r) ** y) for y, a in ts)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        hi = 1_000.0
        fhi = npv(hi)
        if flo * fhi > 0:
            return 0.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def _capital_report(trades: list[BondTrade], p: "BondBacktestParams") -> dict:
    """Whole-period capital view: starting → ending capital, MWRR, peak deployed.

    *MWRR* (money-weighted) is the XIRR of every buy (cash out at entry date)
    and sell (cash in at exit date) — the dollar- and time-weighted return on
    capital actually put to work.  *Return on total capital* is the period P&L
    over the configured ``total_capital`` (or, if capital management is off, over
    the peak simultaneous deployment, which is the most cash ever at risk).
    """
    empty = {
        "total_capital": round(p.total_capital, 0),
        "peak_deployed": 0.0, "ending_capital": round(p.total_capital, 0),
        "capital_change": 0.0, "capital_change_pct": 0.0,
        "mwrr_annual_pct": 0.0, "return_on_capital_pct": 0.0,
        "period_days": 0, "capital_managed": p.total_capital > 0,
    }
    if not trades:
        return empty

    net = sum(t.net_pnl for t in trades)

    # Peak simultaneous *deployment* (principal at risk) from the cash-out/-in
    # timeline.  Release the same principal (cash_out) the position consumed.
    evts: list[tuple[int, int, float]] = []
    for t in trades:
        evts.append((t.date,      t.entry_time, t.cash_out))
        evts.append((t.exit_date, t.exit_time, -t.cash_out))
    evts.sort(key=lambda e: (e[0], e[1]))
    deployed = peak = 0.0
    for _d, _t, amt in evts:
        deployed += amt
        if deployed > peak:
            peak = deployed

    base = p.total_capital if p.total_capital > 0 else peak
    flows = []
    for t in trades:
        flows.append((t.date,      -t.cash_out))
        flows.append((t.exit_date,  t.cash_in))
    mwrr = _xirr(flows)

    d0 = min(t.date for t in trades)
    d1 = max(t.exit_date for t in trades)
    period_days = max(_days_between(d0, d1), 1)

    return {
        "total_capital":         round(base, 0),
        "peak_deployed":         round(peak, 0),
        "ending_capital":        round(base + net, 0),
        "capital_change":        round(net, 0),
        "capital_change_pct":    round(net / base * 100, 4) if base else 0.0,
        "return_on_capital_pct": round(net / base * 100, 4) if base else 0.0,
        "mwrr_annual_pct":       round(mwrr * 100, 4),
        "period_days":           period_days,
        "capital_managed":       p.total_capital > 0,
    }


# --------------------------------------------------------------------------- #
#  Summary + public API                                                        #
# --------------------------------------------------------------------------- #

def _summarize(trades: list[BondTrade], buy_fee: float = BUY_COST) -> dict:
    if not trades:
        return {"trade_count": 0, "win_count": 0, "loss_count": 0, "win_rate": 0,
                "total_net_pnl": 0, "total_invested": 0, "total_return_pct": 0,
                "avg_net_pct": 0, "best_pct": 0, "worst_pct": 0, "eod_count": 0,
                "overnight_count": 0, "avg_hold_min": 0, "total_fees": 0, "avg_entry_z": 0}
    invested = sum(t.buy_notional * (1 + buy_fee) for t in trades)
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
        "eod_count": sum(1 for t in trades if t.exit_reason in ("eod", "final")),
        "overnight_count": sum(1 for t in trades if t.exit_date != t.date),
        "avg_hold_min": round(sum(t.hold_secs for t in trades) / len(trades) / 60, 1),
        "total_fees": round(sum(t.fees for t in trades), 0),
        "avg_entry_z": round(sum(t.entry_z_bps for t in trades) / len(trades), 1),
    }


def _resolve_universe(db, symbols, include_matured: bool = True):
    """Return (universe, meta) — symbol list + per-symbol face value & maturity.

    When *include_matured* is False, اخزا whose maturity date is on/before today
    (already matured as of the run) are dropped from the universe entirely.
    Note that even when included, a series only contributes to a given backtest
    date while it was still alive on that date (see _load_day_cache).
    """
    from bonds import AKHZA_SERIES, _today_int
    try:
        registry = db.get_bond_series(active_only=False)
    except AttributeError:
        registry = []
    if not registry:
        registry = AKHZA_SERIES

    meta: dict[str, dict] = {}
    for s in registry:
        sym = s.get("symbol", "")
        if not sym:
            continue
        meta[sym] = {
            "face_value": float(s.get("face_value", 1_000_000) or 1_000_000),
            "maturity_date": int(s.get("maturity_date", 0) or 0),
        }
    universe = [s for s in (symbols or list(meta)) if s in meta]

    if not include_matured:
        today = _today_int()
        universe = [s for s in universe
                    if not meta[s]["maturity_date"]
                    or meta[s]["maturity_date"] > today]
    return universe, meta


def _load_day_cache(db, universe, meta,
                    start_date: int | None, end_date: int | None):
    """Load every series' OB snapshots per date ONCE (the expensive DB work).

    Returns (dates, cache) where cache maps date → list of
    ``(symbol, face_value, days_to_mat, snaps)`` tuples — fresh ``_SeriesDay``
    objects can be rebuilt cheaply from these for each parameter combination,
    so the optimizer never re-reads SQLite.
    """
    date_set: set[int] = set()
    sym_dates: dict[str, set[int]] = {}
    for sym in universe:
        ds = set(db.get_ob_dates(sym))
        sym_dates[sym] = ds
        date_set |= ds
    dates = sorted(date_set)
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]

    cache: dict[int, list] = {}
    for date_int in dates:
        rows = []
        for sym in universe:
            if date_int not in sym_dates.get(sym, ()):
                continue
            snaps = db.get_orderbook_history(sym, date_int, limit=20000)
            # Keep only in-session snapshots — pre-opening auction and other
            # off-hours quotes (e.g. 06:00) are not executable and would
            # otherwise let the engine trade outside the 09:00–12:30 window.
            snaps = [s for s in snaps if _in_session(s.get("time", 0))]
            if not snaps:
                continue
            mat = meta[sym]["maturity_date"]
            dtm = days_to_maturity(mat, date_int) if mat else 0
            if dtm <= 0:
                continue  # matured on/before this date — skip
            rows.append((sym, meta[sym]["face_value"], dtm, snaps))
        if rows:
            cache[date_int] = rows
    return dates, cache


def _simulate_cache(dates, cache, p: BondBacktestParams):
    """Run the simulation over a pre-loaded day cache. Returns (trades, tested, skipped).

    When force_eod=False positions are carried overnight: carry_pos threads
    through every date.  Any position still open after the last date is
    force-closed at its last available OB snapshot (exit_reason='final').
    """
    trades: list[BondTrade] = []
    tested = skipped = 0
    carry_pos: dict = {}        # positions held across nights
    last_snap: dict = {}        # {sym: (face, dtm, snap)} for end-of-run forced close
    # Shared cash ledger — only active when capital management is on (>0); else
    # None preserves the legacy per-position-cap behaviour bit-for-bit.
    portfolio = {"cash": p.total_capital} if p.total_capital > 0 else None

    for date_int in dates:
        rows = cache.get(date_int)
        if not rows:
            continue
        # Update last known snapshot for every symbol seen today.
        for sym, face, dtm, snaps in rows:
            if snaps:
                last_snap[sym] = (face, dtm, snaps[-1])
        if len(rows) < p.min_curve_points:
            skipped += 1
            # Positions carry over silently on skipped days (no curve → no action).
            continue
        sig_exec = (p.signal_price == "exec")
        day_series = {sym: _SeriesDay(sym, face, dtm, snaps, sig_exec)
                      for (sym, face, dtm, snaps) in rows}
        tested += 1
        new_trades, carry_pos = _simulate_bond_day(
            date_int, day_series, p, carry_pos, portfolio)
        trades.extend(new_trades)

    # End-of-backtest: force-close any remaining open positions.
    if carry_pos:
        final_date = dates[-1] if dates else 0
        for sym, st in list(carry_pos.items()):
            if sym not in last_snap:
                continue
            _face, _dtm, snap = last_snap[sym]
            bids = _ladder(snap, "bid")
            if not bids:
                continue
            t = int(snap.get("time", 0))
            # Exit on the date of the snapshot actually used (the symbol's last
            # traded day), NOT the global last backtest date — otherwise the
            # recorded exit_date points at a day with no matching order book and
            # the OB popup can't show the executed price.
            exit_d = int(snap.get("date", 0)) or final_date
            held = st["units"]
            units, notional = _sell_against_bids(bids, 0, held)
            if units < held:
                notional += (held - units) * bids[-1][0]
                units = held
            _record_close(sym, st, units, notional, t, exit_d, 0.0, "final",
                          p, trades, portfolio)

    return trades, tested, skipped


# --------------------------------------------------------------------------- #
#  Decision-stream cache (optimizer fast path)                                 #
# --------------------------------------------------------------------------- #
#
# In the optimizer, every combo that shares the same (degree, min_curve_points,
# step_secs) produces IDENTICAL curve fits and z-spreads at every tick — only
# the entry/exit thresholds change the buy/sell decisions.  So we walk the
# expensive tick timeline ONCE per such group, emit a compact "decision stream"
# of (tick, [(sym, z, ytm, curve, snapshot), …]) events, and then replay a very
# cheap state machine for each (entry_bps, exit_bps) pair.  This collapses the
# dominant work from O(combos × ticks × series) to O(groups × ticks × series).

def _day_events(date_int: int, day_series: dict, degree: int,
                min_pts: int, step: int):
    """Emit the per-tick z-spread decision stream for one day (no buy/sell).

    Returns (events, eod_snaps) where events is a list of
    ``(t, [(sym, z_bps, ytm, curve_y, snap), …])`` and eod_snaps maps
    sym → that series' last snapshot of the day (for force_eod replay).
    The z-spreads are computed exactly as the live engine does, so replaying
    them is bit-identical to running _simulate_bond_day per combo.
    """
    times: set[int] = set()
    for sd in day_series.values():
        for s in sd.snaps:
            times.add(int(s.get("time", 0)))
    timeline = sorted(t for t in times if t > 0)
    eod_snaps = {sym: sd.snaps[-1] for sym, sd in day_series.items() if sd.snaps}
    if not timeline:
        return [], eod_snaps

    if step > 0:
        kept, last_bucket = [], -1
        for t in timeline:
            b = _secs(t) // step
            if b != last_bucket:
                kept.append(t)
                last_bucket = b
        timeline = kept

    series_items = list(day_series.items())
    series_list = [sd for _, sd in series_items]
    events = []
    for t in timeline:
        changed = False
        for sd in series_list:
            if sd.advance_to(t):
                changed = True
        if not changed:
            continue
        n = 0
        sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
        for sd in series_list:
            y = sd._ytm
            if y > 0.0:
                x, x2 = sd.x, sd.x2
                n += 1
                sx += x; sx2 += x2; sx3 += sd.x3; sx4 += sd.x4
                sy += y; sxy += x * y; sx2y += x2 * y
        if n < min_pts:
            continue
        coeffs = _fit_from_sums(n, sx, sx2, sx3, sx4, sy, sxy, sx2y, degree)
        if coeffs is None:
            continue
        a0, a1, a2 = coeffs
        items = []
        for sym, sd in series_items:
            y = sd._ytm
            if y <= 0.0 or sd.cur is None:
                continue
            curve_y = a0 + a1 * sd.x + a2 * sd.x2
            z_bps = (y - curve_y) * 10_000.0
            # Carry the executable touch YTMs so the replay can reproduce the
            # exec-mode entry (vs best ask) / exit (vs best bid) decisions.
            items.append((sym, z_bps, y, curve_y, sd.cur, sd._ytm_ask, sd._ytm_bid))
        if items:
            events.append((t, items))
    return events, eod_snaps


def _build_decision_stream(dates, cache, tmpl: BondBacktestParams):
    """Precompute the day-by-day decision stream for one (degree, min_pts, step)
    group.  Reused across every (entry, exit) combo in that group."""
    degree, min_pts, step = tmpl.degree, tmpl.min_curve_points, tmpl.step_secs
    days = []
    last_snap: dict = {}
    tested = skipped = 0
    for date_int in dates:
        rows = cache.get(date_int)
        if not rows:
            continue
        for sym, face, dtm, snaps in rows:
            if snaps:
                last_snap[sym] = (face, dtm, snaps[-1])
        if len(rows) < min_pts:
            skipped += 1
            days.append({"date": date_int, "skipped": True})
            continue
        tested += 1
        sig_exec = (tmpl.signal_price == "exec")
        day_series = {sym: _SeriesDay(sym, face, dtm, snaps, sig_exec)
                      for (sym, face, dtm, snaps) in rows}
        events, eod_snaps = _day_events(date_int, day_series, degree, min_pts, step)
        days.append({"date": date_int, "skipped": False,
                     "events": events, "eod_snaps": eod_snaps})
    return {"days": days, "last_snap": last_snap,
            "final_date": dates[-1] if dates else 0,
            "tested": tested, "skipped": skipped}


def _replay_stream(stream, p: BondBacktestParams) -> list[BondTrade]:
    """Replay a precomputed decision stream under one (entry_bps, exit_bps)
    pair.  Produces exactly the trades _simulate_cache would for the same p."""
    entry_bps, exit_bps, capital = p.entry_bps, p.exit_bps, p.capital
    force_eod = p.force_eod
    exec_mode = (p.signal_price == "exec")
    trades: list[BondTrade] = []
    pos: dict[str, dict] = {}
    portfolio = {"cash": p.total_capital} if p.total_capital > 0 else None

    def _close(sym, st, units, notional, t, exit_date, z, reason):
        _record_close(sym, st, units, notional, t, exit_date, z, reason,
                      p, trades, portfolio)
        if st["units"] <= 0:
            pos.pop(sym, None)

    for day in stream["days"]:
        if day["skipped"]:
            continue                       # positions carry silently
        date_int = day["date"]
        for t, items in day["events"]:
            for sym, z_bps, y, curve_y, snap, ya, yb in items:
                if sym not in pos:
                    # Entry on executable ask in exec mode, else mid z-spread.
                    if exec_mode:
                        if ya <= 0.0:
                            continue
                        z_sig = (ya - curve_y) * 10_000.0
                    else:
                        z_sig = z_bps
                    if z_sig >= entry_bps:
                        asks = _ladder(snap, "ask")
                        if not asks:
                            continue
                        budget = _buy_budget(p, portfolio, sym, pos)
                        if budget <= 0:
                            continue
                        units, notional = _buy_against_asks(asks, asks[-1][0], budget)
                        if units > 0:
                            if portfolio is not None:
                                portfolio["cash"] -= notional * (1.0 + p.buy_fee)
                            pos[sym] = {
                                "units": units, "buy_notional": notional,
                                "entry_date": date_int, "entry_time": t,
                                "entry_ytm": y, "entry_curve": curve_y, "entry_z": z_sig,
                            }
                else:
                    # Exit on executable bid in exec mode, else mid z-spread.
                    if exec_mode:
                        if yb <= 0.0:
                            continue
                        z_sig = (yb - curve_y) * 10_000.0
                    else:
                        z_sig = z_bps
                    if z_sig <= exit_bps:
                        bids = _ladder(snap, "bid")
                        if not bids:
                            continue
                        units, notional = _sell_against_bids(bids, bids[-1][0], pos[sym]["units"])
                        if units > 0 and _exit_clears_min(pos[sym], units, notional, p):
                            _close(sym, pos[sym], units, notional, t, date_int, z_sig, "signal")
        if force_eod:
            for sym in list(pos.keys()):
                snap = day["eod_snaps"].get(sym)
                if snap is None:
                    continue
                bids = _ladder(snap, "bid")
                if not bids:
                    continue
                t = int(snap.get("time", 0))
                held = pos[sym]["units"]
                units, notional = _sell_against_bids(bids, 0, held)
                if units < held:
                    notional += (held - units) * bids[-1][0]
                    units = held
                _close(sym, pos[sym], units, notional, t, date_int, 0.0, "eod")

    # End-of-backtest forced close (matches _simulate_cache).
    if pos:
        final_date = stream["final_date"]
        for sym, st in list(pos.items()):
            ls = stream["last_snap"].get(sym)
            if ls is None:
                continue
            snap = ls[2]
            bids = _ladder(snap, "bid")
            if not bids:
                continue
            t = int(snap.get("time", 0))
            # Use the snapshot's own date (the symbol's last traded day) so the
            # recorded exit_date matches a day that actually has an order book.
            exit_d = int(snap.get("date", 0)) or final_date
            held = st["units"]
            units, notional = _sell_against_bids(bids, 0, held)
            if units < held:
                notional += (held - units) * bids[-1][0]
                units = held
            _record_close(sym, st, units, notional, t, exit_d, 0.0, "final",
                          p, trades, portfolio)

    return trades


def run_bond_backtest(db, symbols: list[str] | None = None,
                      start_date: int | None = None,
                      end_date: int | None = None,
                      params: BondBacktestParams | None = None) -> dict:
    """Tick-by-tick cross-sectional z-spread backtest over اخزا history.

    Parameters
    ----------
    db          : Database — provides bond registry + OB history
    symbols     : restrict to these اخزا symbols (default: all active w/ ins_code)
    start_date  : YYYYMMDD inclusive, or None for all recorded dates
    end_date    : YYYYMMDD inclusive, or None
    params      : BondBacktestParams

    Returns
    -------
    dict: {symbols, params, days_tested, days_skipped, trades:[…], summary:{…}}
    """
    p = params or BondBacktestParams()
    universe, meta = _resolve_universe(db, symbols, include_matured=p.include_matured)
    dates, cache = _load_day_cache(db, universe, meta, start_date, end_date)
    all_trades, days_tested, days_skipped = _simulate_cache(dates, cache, p)

    return {
        "symbols": universe,
        "params": asdict(p),
        "days_tested": days_tested,
        "days_skipped": days_skipped,
        "trades": [asdict(t) for t in all_trades],
        "summary": _summarize(all_trades, p.buy_fee),
        "capital": _capital_report(all_trades, p),
    }


# --------------------------------------------------------------------------- #
#  Parameter optimizer                                                         #
# --------------------------------------------------------------------------- #

# ── Risk-adjusted metrics ─────────────────────────────────────────────────

def _risk_metrics(trades: list[BondTrade], buy_fee: float = BUY_COST) -> dict:
    """Compute per-trade risk-adjusted performance: Sharpe, Sortino, PF, MDD, Calmar, Expectancy."""
    empty = {"sharpe": 0.0, "sortino": 0.0, "profit_factor": 0.0,
             "max_drawdown_pct": 0.0, "calmar": 0.0, "expectancy": 0.0}
    if not trades:
        return empty
    rets = [t.net_pct for t in trades]
    n = len(rets)
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / max(n - 1, 1)
    std = math.sqrt(var) if var > 0 else 0.0
    sharpe = mu / std if std > 0 else (10.0 if mu > 0 else 0.0)
    down_sq = sum((r - mu) ** 2 for r in rets if r < mu)
    dstd = math.sqrt(down_sq / max(n - 1, 1)) if down_sq > 0 else 0.0
    sortino = mu / dstd if dstd > 0 else (10.0 if mu > 0 else 0.0)
    gross_w = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_l = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
    pf = gross_w / gross_l if gross_l > 0 else (99.0 if gross_w > 0 else 0.0)
    ordered = sorted(trades, key=lambda x: (x.exit_date, x.exit_time))
    cum = peak = max_dd = 0.0
    for t in ordered:
        cum += t.net_pnl
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak
            if dd > max_dd:
                max_dd = dd
    max_dd_pct = max_dd * 100
    total_ret = sum(rets)
    calmar = total_ret / max_dd_pct if max_dd_pct > 0 else (total_ret if total_ret > 0 else 0.0)
    return {
        "sharpe":           round(sharpe, 4),
        "sortino":          round(sortino, 4),
        "profit_factor":    round(min(pf, 99.0), 4),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "calmar":           round(calmar, 4),
        "expectancy":       round(mu, 6),
    }


def _objective_v2(summary: dict, metrics: dict, min_trades: int,
                  opt_metric: str = "sharpe") -> float:
    """Risk-adjusted objective. Higher is better. Penalises EOD-forced exits."""
    tc = summary.get("trade_count", 0)
    if tc < min_trades:
        return -1000.0 + tc
    eod_frac = summary.get("eod_count", 0) / tc if tc else 1.0
    scores = {
        "sharpe":        metrics.get("sharpe", 0.0),
        "sortino":       metrics.get("sortino", 0.0),
        "calmar":        metrics.get("calmar", 0.0),
        "profit_factor": min(metrics.get("profit_factor", 0.0), 10.0),
        "expectancy":    metrics.get("expectancy", 0.0),
        "total_return":  summary.get("total_return_pct", 0.0),
    }
    return scores.get(opt_metric, scores["sharpe"]) - 0.2 * eod_frac


# ── Coarse-to-Fine search ─────────────────────────────────────────────────

# Phase-1 coarse grid: wide spacing over expanded parameter space
COARSE_GRID = {
    "degree":           [1, 2],
    "min_curve_points": [3, 4, 5],
    "entry_bps":        [15, 30, 45, 65, 85, 110],
    "exit_bps":         [-20, -10, 0, 10, 20],
    "step_secs":        [0, 30, 60],
}
# Phase-2 refinement: ±step around best coarse results
_FINE_STEP = {"entry_bps": 8, "exit_bps": 4}

# Legacy grid kept for reference (no longer used by default)
DEFAULT_GRID = {
    "degree":           [1, 2],
    "min_curve_points": [3, 4],
    "entry_bps":        [25, 40, 55, 70, 90],
    "exit_bps":         [-10, 0, 10, 20],
}


def _c2f_optimize(dates: list, cache: dict, base: BondBacktestParams,
                  opt_metric: str, min_trades: int,
                  top_k: int = 5,
                  progress: dict | None = None,
                  lock=None) -> list[dict]:
    """Two-phase coarse-to-fine search. Returns all evaluated combos sorted by score.

    Combos are grouped by (degree, min_curve_points, step_secs); the expensive
    z-spread decision stream is built once per group (and cached across phases),
    then each (entry, exit) combo is replayed cheaply on top of it.
    """
    import threading
    _lock = lock or threading.Lock()
    stream_cache: dict = {}     # (degree, minpts, step) → decision stream

    def _get_stream(degree, minpts, step):
        key = (degree, minpts, step)
        st = stream_cache.get(key)
        if st is None:
            tmpl = BondBacktestParams(
                capital=base.capital, degree=int(degree),
                min_curve_points=int(minpts), step_secs=int(step),
                force_eod=base.force_eod, include_matured=base.include_matured,
                buy_fee=base.buy_fee, sell_fee=base.sell_fee,
                signal_price=base.signal_price)
            st = _build_decision_stream(dates, cache, tmpl)
            stream_cache[key] = st
        return st

    def _eval(degree, minpts, entry, exit_, step):
        p = BondBacktestParams(
            capital=base.capital, entry_bps=float(entry), exit_bps=float(exit_),
            degree=int(degree), min_curve_points=int(minpts),
            step_secs=int(step), force_eod=base.force_eod,
            include_matured=base.include_matured,
            buy_fee=base.buy_fee, sell_fee=base.sell_fee,
            strategy=base.strategy,
            min_exit_profit_bps=base.min_exit_profit_bps,
            total_capital=base.total_capital,
            max_position_pct=base.max_position_pct,
            signal_price=base.signal_price)
        if base.strategy == "outlier":
            # Per-order outlier z depends on the OB ladders, not just the mid,
            # so the mid-based decision-stream cache doesn't apply — simulate.
            trades, tested, _ = _simulate_cache(dates, cache, p)
        else:
            st = _get_stream(degree, minpts, step)
            trades = _replay_stream(st, p)
            tested = st["tested"]
        summary = _summarize(trades, base.buy_fee)
        metrics = _risk_metrics(trades, base.buy_fee)
        return {
            "params":      asdict(p),
            "summary":     summary,
            "metrics":     metrics,
            "score":       round(_objective_v2(summary, metrics, min_trades, opt_metric), 4),
            "days_tested": tested,
        }

    def _run_combos(combos):
        """Evaluate combos, building each group's stream once (grouped first)."""
        # Sort by group so a group's stream is built once then reused, and can
        # be dropped right after to bound memory.
        combos = sorted(combos, key=lambda c: (c[0], c[1], c[4]))
        out = []
        for combo in combos:
            out.append(_eval(*combo))
            if progress is not None:
                with _lock:
                    progress["done"] += 1
        return out

    # Phase 1 — coarse
    coarse = [
        (deg, minp, ent, ex, stp)
        for deg, minp, ent, ex, stp in itertools.product(
            COARSE_GRID["degree"], COARSE_GRID["min_curve_points"],
            COARSE_GRID["entry_bps"], COARSE_GRID["exit_bps"],
            COARSE_GRID["step_secs"],
        )
        if ex < ent
    ]
    if progress is not None:
        with _lock:
            progress.update({"done": 0, "total": len(coarse), "phase": "coarse"})

    seen: set = set(coarse)
    results: list[dict] = _run_combos(coarse)
    results.sort(key=lambda r: r["score"], reverse=True)

    # Phase 2 — fine-grid zoom around top-K coarse winners
    fine_set: set = set()
    for r in results[:top_k]:
        p = r["params"]
        for de in [-_FINE_STEP["entry_bps"], _FINE_STEP["entry_bps"]]:
            for dx in [-_FINE_STEP["exit_bps"], _FINE_STEP["exit_bps"]]:
                ne = p["entry_bps"] + de
                nx = p["exit_bps"] + dx
                if nx < ne and ne > 5:
                    fine_set.add((p["degree"], p["min_curve_points"], ne, nx, p["step_secs"]))
    fine_new = [c for c in fine_set if c not in seen]

    if fine_new and progress is not None:
        with _lock:
            progress["total"] = progress.get("total", 0) + len(fine_new)
            progress["phase"] = "fine"

    if fine_new:
        results.extend(_run_combos(fine_new))

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ── Parameter stability ───────────────────────────────────────────────────

def _param_stability(dates: list, cache: dict, best_params: dict,
                     base: BondBacktestParams,
                     min_trades: int, opt_metric: str) -> float:
    """Measure robustness: how stable is the score across ±1 step neighbours? 0=knife-edge, 1=plateau."""
    bp = best_params
    scores = []
    for de, dx in itertools.product([-_FINE_STEP["entry_bps"], 0, _FINE_STEP["entry_bps"]],
                                     [-_FINE_STEP["exit_bps"],  0, _FINE_STEP["exit_bps"]]):
        if de == 0 and dx == 0:
            continue
        ne, nx = bp["entry_bps"] + de, bp["exit_bps"] + dx
        if nx >= ne or ne <= 0:
            continue
        p = BondBacktestParams(
            capital=base.capital, entry_bps=float(ne), exit_bps=float(nx),
            degree=int(bp["degree"]), min_curve_points=int(bp["min_curve_points"]),
            step_secs=int(bp.get("step_secs", 0)), force_eod=base.force_eod,
            include_matured=base.include_matured,
            buy_fee=base.buy_fee, sell_fee=base.sell_fee, strategy=base.strategy)
        trades, _, _ = _simulate_cache(dates, cache, p)
        s = _summarize(trades, base.buy_fee)
        m = _risk_metrics(trades, base.buy_fee)
        sc = _objective_v2(s, m, min_trades, opt_metric)
        if sc > -900:
            scores.append(sc)
    if not scores:
        return 0.0
    mu = sum(scores) / len(scores)
    if abs(mu) < 1e-9:
        return 0.0
    std = math.sqrt(sum((s - mu) ** 2 for s in scores) / max(len(scores) - 1, 1))
    cv = std / abs(mu)
    return round(max(0.0, 1.0 - min(cv, 1.0)), 3)


# ── Walk-forward validation ───────────────────────────────────────────────

def _walk_forward(dates: list, cache: dict, params: dict,
                  base: BondBacktestParams,
                  n_windows: int = 4, oos_frac: float = 0.3,
                  min_trades: int = 3) -> dict:
    """Rolling walk-forward: split history into n_windows × (IS + OOS). Measures out-of-sample consistency."""
    if len(dates) < 8:
        return {"valid": False, "windows": []}
    wsize = len(dates) // n_windows
    if wsize < 3:
        return {"valid": False, "windows": []}

    def _make_params(p_dict):
        return BondBacktestParams(
            capital=base.capital,
            entry_bps=float(p_dict["entry_bps"]),
            exit_bps=float(p_dict["exit_bps"]),
            degree=int(p_dict["degree"]),
            min_curve_points=int(p_dict["min_curve_points"]),
            step_secs=int(p_dict.get("step_secs", 0)),
            force_eod=base.force_eod,
            include_matured=base.include_matured,
            buy_fee=base.buy_fee,
            sell_fee=base.sell_fee,
            strategy=base.strategy,
            min_exit_profit_bps=base.min_exit_profit_bps,
            total_capital=base.total_capital,
            max_position_pct=base.max_position_pct,
            signal_price=base.signal_price)

    windows = []
    for i in range(n_windows):
        s = i * wsize
        e = s + wsize if i < n_windows - 1 else len(dates)
        wd = dates[s:e]
        split = max(1, int(len(wd) * (1 - oos_frac)))
        is_d, oos_d = wd[:split], wd[split:]
        if not oos_d:
            continue
        p = _make_params(params)
        is_t, _, _  = _simulate_cache(is_d,  cache, p)
        oos_t, _, _ = _simulate_cache(oos_d, cache, p)
        is_s  = _summarize(is_t,  base.buy_fee)
        oos_s = _summarize(oos_t, base.buy_fee)
        is_m  = _risk_metrics(is_t,  base.buy_fee)
        oos_m = _risk_metrics(oos_t, base.buy_fee)
        windows.append({
            "window": i + 1,
            "is_days":        len(is_d),
            "oos_days":       len(oos_d),
            "is_trades":      is_s["trade_count"],
            "oos_trades":     oos_s["trade_count"],
            "is_return_pct":  is_s["total_return_pct"],
            "oos_return_pct": oos_s["total_return_pct"],
            "is_sharpe":      is_m["sharpe"],
            "oos_sharpe":     oos_m["sharpe"],
            "is_win_rate":    is_s["win_rate"],
            "oos_win_rate":   oos_s["win_rate"],
        })
    if not windows:
        return {"valid": False, "windows": []}
    oos_win = sum(1 for w in windows if w["oos_return_pct"] > 0)
    return {
        "valid":               True,
        "n_windows":           len(windows),
        "windows":             windows,
        "oos_win_rate_pct":    round(oos_win / len(windows) * 100, 1),
        "avg_oos_return_pct":  round(sum(w["oos_return_pct"] for w in windows) / len(windows), 4),
        "avg_oos_sharpe":      round(sum(w["oos_sharpe"]     for w in windows) / len(windows), 4),
    }


def optimize_bond_backtest(db, symbols: list[str] | None = None,
                           start_date: int | None = None,
                           end_date: int | None = None,
                           base: BondBacktestParams | None = None,
                           grid: dict | None = None,      # ignored (kept for compat)
                           min_trades: int = 3,
                           top_n: int = 10,
                           opt_metric: str = "sharpe",
                           walk_forward: bool = True,
                           progress: dict | None = None,
                           progress_lock=None) -> dict:
    """Advanced coarse-to-fine اخزا parameter optimizer.

    Phases
    ------
    1. **Coarse** — evaluate all combos in COARSE_GRID (~270–350 combos after
       validity filter).
    2. **Fine** — zoom into top-5 coarse winners with ±8 bps / ±4 bps steps.
    3. **Stability** — evaluate 8 neighbours of the best combo; compute a
       0–1 robustness score.
    4. **Walk-Forward** — split history into 4 rolling windows (30% OOS each);
       measure out-of-sample consistency.
    """
    import threading
    base = base or BondBacktestParams()
    universe, meta = _resolve_universe(db, symbols, include_matured=base.include_matured)
    dates, cache   = _load_day_cache(db, universe, meta, start_date, end_date)

    # Phases 1 + 2: coarse-to-fine
    all_results = _c2f_optimize(
        dates, cache, base,
        opt_metric=opt_metric, min_trades=min_trades,
        top_k=5, progress=progress, lock=progress_lock)

    best = all_results[0] if all_results else None

    # Phase 3: stability around best
    if best and best["score"] > -900:
        if progress is not None:
            with (progress_lock or threading.Lock()):
                progress["phase"] = "stability"
        best["stability"] = _param_stability(
            dates, cache, best["params"], base, min_trades, opt_metric)
    else:
        if best:
            best["stability"] = 0.0

    # Phase 4: walk-forward validation
    wf = {"valid": False, "windows": []}
    if best and walk_forward and best["score"] > -900 and len(dates) >= 8:
        if progress is not None:
            with (progress_lock or threading.Lock()):
                progress["phase"] = "walk_forward"
        wf = _walk_forward(dates, cache, best["params"], base,
                           n_windows=4, oos_frac=0.3, min_trades=min_trades)
        best["walk_forward"] = wf

    return {
        "symbols":        universe,
        "tested_combos":  len(all_results),
        "min_trades":     min_trades,
        "opt_metric":     opt_metric,
        "days_available": len(dates),
        "best":           best,
        "top":            all_results[:top_n],
    }
