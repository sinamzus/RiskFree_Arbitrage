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
import os
from dataclasses import dataclass, asdict

from bonds import (ytm_zero_coupon, price_zero_coupon, fit_yield_curve,
                   eval_curve, days_to_maturity, _solve_3x3)
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
    capital: float = 1_000_000_000   # LEGACY per-position cap (Rials). Only used
                                     # when total_capital == 0 (capital management
                                     # off); otherwise the per-position size is
                                     # total_capital × max_position_pct.
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
    total_capital: float = 10_000_000_000.0  # portfolio cash pool (Rials) — the
                                     # default capital-management model. A single
                                     # shared pool: buys consume cash, sells return
                                     # it, and the engine can't deploy more than it
                                     # holds.  0 = OFF (legacy: each position capped
                                     # independently by `capital`, unlimited concurrent).
    max_position_pct: float = 0.5    # money-management: max fraction of total_capital
                                     # a single position may deploy (0..1). Only used
                                     # when total_capital > 0.
    entry_max_bps: float = 150.0     # signal sanity band: only ENTER when the entry
                                     # z-spread is in [entry_bps, entry_max_bps]. A
                                     # z far above the curve is not "cheap", it is a
                                     # mispriced/garbage input (bad maturity date,
                                     # stale quote, curve misfit) that never reverts
                                     # → it is rejected instead of bought. 0/neg = off.
    curve_trim_bps: float = 150.0    # robust curve fit: drop any series further than
                                     # this off the first-pass curve, then re-fit, so
                                     # one broken series can't distort the curve for
                                     # everyone. 0/neg = legacy single-pass fit.
    min_dtm: int = 30                # exclude اخزا with fewer than this many days to
                                     # maturity from BOTH the curve fit and trading.
                                     # Near maturity the YTM = (fv/p)^(365/dtm)-1 math
                                     # explodes (a 7-day bill has exponent 52), so a
                                     # tiny price wiggle becomes a huge yield swing that
                                     # is pure noise and distorts the whole curve.
                                     # 0 = off (include everything down to 1 day).
    exit_needs_replacement: bool = True  # اخزا has a deterministic pull-to-par drift,
                                     # so sitting in cash forgoes the risk-free yield —
                                     # cash IS the loss.  When True, a reverted position
                                     # (z ≤ exit_bps) is sold ONLY if there is a fresh
                                     # buy candidate that same tick to redeploy the
                                     # freed capital into; otherwise it keeps holding
                                     # and riding the drift.  Forced exits (eod/final/
                                     # maturity) still always fire.  False = legacy
                                     # (always sell to cash on reversion).
    min_hold_days: int = 1           # minimum calendar days a position must be held
                                     # before a *signal* exit is allowed.  Blocks
                                     # same-day (intraday) exits which suffer from
                                     # intraday curve-shift noise even when the
                                     # z-spread has reverted.  Forced exits (eod/
                                     # final/maturity) are never blocked.  0 = off.


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


def _fit_curve_from_series(series_list, degree: int, min_pts: int,
                           trim_bps: float = 0.0):
    """Fit the yield curve across a tick's series (returns (a0,a1,a2) or None).

    With *trim_bps* > 0 the fit is made ROBUST: an initial least-squares curve is
    fit over all priced series, then any series sitting further than *trim_bps*
    off that curve is treated as an outlier and dropped, and the curve is re-fit
    on the survivors.  This stops a single broken series (e.g. one with a wrong
    maturity_date, so its YTM is systematically off) from dragging the whole
    curve toward itself and manufacturing false signals for everyone else.

    trim_bps == 0 is bit-identical to the original single least-squares fit, so
    leaving it off preserves legacy results exactly.
    """
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
        return None
    coeffs = _fit_from_sums(n, sx, sx2, sx3, sx4, sy, sxy, sx2y, degree)
    if coeffs is None or not (trim_bps and trim_bps > 0.0):
        return coeffs

    # ── Robust second pass: drop |residual| > trim_bps and re-fit ──
    a0, a1, a2 = coeffs
    trim = trim_bps / 10_000.0
    n2 = 0
    sx = sx2 = sx3 = sx4 = sy = sxy = sx2y = 0.0
    for sd in series_list:
        y = sd._ytm
        if y > 0.0:
            cy = a0 + a1 * sd.x + a2 * sd.x2
            if abs(y - cy) <= trim:
                x, x2 = sd.x, sd.x2
                n2 += 1
                sx += x; sx2 += x2; sx3 += sd.x3; sx4 += sd.x4
                sy += y; sxy += x * y; sx2y += x2 * y
    if n2 >= min_pts and n2 < n:
        c2 = _fit_from_sums(n2, sx, sx2, sx3, sx4, sy, sxy, sx2y, degree)
        if c2 is not None:
            return c2
    return coeffs


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


def _yyyymmdd_diff(d1: int, d2: int) -> int:
    """Calendar days between two YYYYMMDD integers (d2 − d1)."""
    import datetime as _dt
    a = _dt.date(d1 // 10000, (d1 // 100) % 100, d1 % 100)
    b = _dt.date(d2 // 10000, (d2 // 100) % 100, d2 % 100)
    return (b - a).days


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

    Portfolio mode: the per-position cap is ``total_capital × max_position_pct``;
    on top of that, the buy is bounded by the cash actually on hand (reserving
    the buy fee so cash never goes negative).
    """
    cur = pos[sym]["buy_notional"] if sym in pos else 0.0
    if portfolio is None:
        return p.capital - cur
    cap = p.total_capital * p.max_position_pct - cur
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
    emax = p.entry_max_bps
    for price, vol in asks:                # best-first = ascending price
        z = (ytm_zero_coupon(price, face, dtm) - curve_y) * 10_000.0
        if emax > 0.0 and z > emax:        # implausibly cheap → mispriced, skip
            continue                       # this level; deeper asks are less cheap
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
    trim_bps  = p.curve_trim_bps
    entry_max = p.entry_max_bps
    needs_repl = p.exit_needs_replacement
    min_hold  = p.min_hold_days

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

        # Fit the (optionally robust) yield curve across all priced series.
        coeffs = _fit_curve_from_series(series_list, degree, min_pts, trim_bps)
        if coeffs is None:
            continue
        a0, a1, a2 = coeffs

        # "Cash = loss" exit gate: is there a fresh buy candidate this tick whose
        # freed capital could be redeployed?  Computed from the tick-start state so
        # a reverted position only sells if there's somewhere better to rotate.
        has_candidate = False
        if needs_repl:
            for s2, sd2 in series_items:
                if s2 in pos:
                    continue
                y2 = sd2._ytm
                if y2 <= 0.0 or sd2.cur is None:
                    continue
                cy2 = a0 + a1 * sd2.x + a2 * sd2.x2
                if exec_mode:
                    ya2 = sd2._ytm_ask
                    if ya2 <= 0.0:
                        continue
                    zc = (ya2 - cy2) * 10_000.0
                else:
                    zc = (y2 - cy2) * 10_000.0
                if zc >= entry_bps and (entry_max <= 0.0 or zc <= entry_max):
                    has_candidate = True
                    break

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
                # Sanity band: a z far above the curve is a mispriced input, not
                # an opportunity — reject it instead of buying a guaranteed loser.
                if entry_max > 0.0 and z_sig > entry_max:
                    continue
                if z_sig >= entry_bps:
                    asks = _ladder(sd.cur, "ask")
                    if not asks:
                        continue
                    # Only fill ask levels still cheap enough to clear the entry
                    # edge.  The ceiling is the price whose yield == curve +
                    # entry_bps; any ask above it yields LESS than the threshold
                    # (rich) and must not be lifted.  The old ceiling (asks[-1])
                    # swept the whole ladder into rich deep levels and bled the
                    # entire edge away — the core source of "stupid" losses.
                    ceiling = price_zero_coupon(
                        curve_y + entry_bps / 10_000.0, sd.face_value, sd.dtm)
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
                    # "Cash = loss": only realise a reverted position if there is
                    # somewhere better to put the money this tick; else keep riding
                    # the pull-to-par drift. Forced exits below are unaffected.
                    if needs_repl and not has_candidate:
                        continue
                    # Minimum hold: intraday curve-shift noise often causes the
                    # z-spread to look "reverted" within hours of entry even when
                    # the position hasn't actually profited (the curve moved, not
                    # the bond's relative value). Block signal exits until the
                    # position has been held for at least min_hold_days calendar
                    # days.  Forced exits (eod/final/maturity) are not blocked.
                    if min_hold > 0:
                        edate = pos[sym]["entry_date"]
                        if _yyyymmdd_diff(edate, date_int) < min_hold:
                            continue
                    bids = _ladder(sd.cur, "bid")
                    if not bids:
                        continue
                    # Only sell into bid levels still rich enough to clear the
                    # exit edge.  The floor is the price whose yield == curve +
                    # exit_bps; any bid below it yields MORE than the threshold
                    # (cheap) and selling into it realises a loss.  The old floor
                    # (bids[-1]) dumped the position down the whole bid ladder.
                    floor = price_zero_coupon(
                        curve_y + exit_bps / 10_000.0, sd.face_value, sd.dtm)
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


def _loss_reason(t: "BondTrade", p: "BondBacktestParams") -> str:
    """Human-readable (Persian) attribution of WHY a single trade lost money.

    Decomposes the loss into the dominant cause from the data carried on the
    trade — z-spread move (entry→exit), price move, fees, and exit reason — and
    returns one clear sentence with the numbers.  Empty string for winners.

    Priority of causes:
      1. forced exit (final/eod) before the spread reverted to the curve;
      2. curve shifted up — the relative (z) bet won but the market level lost;
      3. the round-trip commission exceeded the gross price gain;
      4. the z-spread widened instead of reverting (bond got cheaper, not richer);
      5. generic price drop net of fees.
    """
    if t.net_pnl >= 0:
        return ""
    f = lambda n: f"{round(n):,}"
    gross = t.net_pnl + t.fees            # price P&L before fees
    ez, xz = t.entry_z_bps, t.exit_z_bps
    reverted = xz <= ez - 5.0             # z came in toward the curve (>5 bps)
    px_in, px_out = t.entry_price, t.exit_price

    # 1) Forced out before the spread reverted to the exit threshold.
    if t.exit_reason in ("final", "eod") and xz > p.exit_bps:
        tag = "پایان بازهٔ تست" if t.exit_reason == "final" else "پایان روز"
        return (f"خروج اجباری ({tag}) پیش از بازگشت اسپرد به منحنی: "
                f"z از {ez:.0f} به {xz:.0f} bps رسید (هنوز بالای آستانهٔ خروج "
                f"{p.exit_bps:.0f} bps). موقعیت زودتر از موعد بسته شد و فرصت بازگشت نیافت.")

    # 2) Relative bet won (z reverted) but the whole yield curve rose → price fell.
    if reverted and px_out < px_in:
        return (f"شیفت منحنی به بالا: اسپرد z درست برگشت ({ez:.0f}→{xz:.0f} bps) "
                f"اما چون کل منحنی بازده بالا رفت، قیمت اوراق افت کرد "
                f"({f(px_in)}→{f(px_out)} ریال). شرط نسبی برنده شد ولی سطح بازار بازنده.")

    # 3) Gross price gain was positive but smaller than the round-trip commission.
    if gross > 0:
        return (f"کارمزد رفت‌وبرگشت ({f(t.fees)} ریال) بزرگ‌تر از سود قیمتی ناخالص "
                f"({f(gross)} ریال) شد — لبهٔ z کمتر از مجموع اسپرد بید/اَسک و کارمزد بود.")

    # 4) z-spread widened instead of reverting — the bond got cheaper, not richer.
    if xz >= ez:
        return (f"اسپرد z به‌جای بازگشت، بازتر شد ({ez:.0f}→{xz:.0f} bps): "
                f"اوراق ارزان‌تر شد نه گران‌تر؛ سیگنال در جهت مخالف حرکت کرد.")

    # 5) Generic: price fell more than fees could absorb.
    return (f"قیمت فروش کمتر از قیمت خرید بود ({f(px_in)}→{f(px_out)} ریال) "
            f"به‌علاوهٔ کارمزد {f(t.fees)} ریال؛ اسپرد z از {ez:.0f} به {xz:.0f} bps رفت.")


def _trade_to_dict(t: "BondTrade", p: "BondBacktestParams") -> dict:
    """Serialise a trade for the API, attaching a loss-reason tooltip string."""
    d = asdict(t)
    d["loss_reason"] = _loss_reason(t, p)
    return d


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

    min_dtm = p.min_dtm
    for date_int in dates:
        rows = cache.get(date_int)
        if not rows:
            continue
        # Drop numerically-explosive near-maturity series from the day entirely
        # (excluded from both the curve fit and trading). r = (sym, face, dtm, snaps).
        if min_dtm > 0:
            rows = [r for r in rows if r[2] >= min_dtm]
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
                min_pts: int, step: int, trim_bps: float = 0.0):
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
        coeffs = _fit_curve_from_series(series_list, degree, min_pts, trim_bps)
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
            # Carry the executable touch YTMs (for exec-mode entry-vs-ask /
            # exit-vs-bid decisions) plus face value & dtm (so the replay can
            # recompute the price ceiling/floor that caps fills at the edge).
            items.append((sym, z_bps, y, curve_y, sd.cur, sd._ytm_ask,
                          sd._ytm_bid, sd.face_value, sd.dtm))
        if items:
            events.append((t, items))
    return events, eod_snaps


def _build_decision_stream(dates, cache, tmpl: BondBacktestParams):
    """Precompute the day-by-day decision stream for one (degree, min_pts, step)
    group.  Reused across every (entry, exit) combo in that group."""
    degree, min_pts, step = tmpl.degree, tmpl.min_curve_points, tmpl.step_secs
    trim_bps = tmpl.curve_trim_bps
    min_dtm = tmpl.min_dtm
    days = []
    last_snap: dict = {}
    tested = skipped = 0
    for date_int in dates:
        rows = cache.get(date_int)
        if not rows:
            continue
        if min_dtm > 0:
            rows = [r for r in rows if r[2] >= min_dtm]
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
        events, eod_snaps = _day_events(date_int, day_series, degree, min_pts,
                                        step, trim_bps)
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
    entry_max = p.entry_max_bps
    needs_repl = p.exit_needs_replacement
    min_hold  = p.min_hold_days
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
            # "Cash = loss" gate: any fresh buy candidate this tick to rotate into?
            # Computed from tick-start pos; mirrors _simulate_bond_day exactly.
            has_candidate = False
            if needs_repl:
                for it in items:
                    if it[0] in pos:
                        continue
                    cy2 = it[3]
                    if exec_mode:
                        ya2 = it[5]
                        if ya2 <= 0.0:
                            continue
                        zc = (ya2 - cy2) * 10_000.0
                    else:
                        zc = it[1]
                    if zc >= entry_bps and (entry_max <= 0.0 or zc <= entry_max):
                        has_candidate = True
                        break
            for sym, z_bps, y, curve_y, snap, ya, yb, fv, dtm in items:
                if sym not in pos:
                    # Entry on executable ask in exec mode, else mid z-spread.
                    if exec_mode:
                        if ya <= 0.0:
                            continue
                        z_sig = (ya - curve_y) * 10_000.0
                    else:
                        z_sig = z_bps
                    # Sanity band: reject implausibly-cheap (mispriced) signals.
                    if entry_max > 0.0 and z_sig > entry_max:
                        continue
                    if z_sig >= entry_bps:
                        asks = _ladder(snap, "ask")
                        if not asks:
                            continue
                        budget = _buy_budget(p, portfolio, sym, pos)
                        if budget <= 0:
                            continue
                        # Cap the buy at the edge price (yield == curve+entry_bps)
                        # so deep rich asks are never lifted — mirrors _simulate.
                        ceiling = price_zero_coupon(
                            curve_y + entry_bps / 10_000.0, fv, dtm)
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
                    # Exit on executable bid in exec mode, else mid z-spread.
                    if exec_mode:
                        if yb <= 0.0:
                            continue
                        z_sig = (yb - curve_y) * 10_000.0
                    else:
                        z_sig = z_bps
                    if z_sig <= exit_bps:
                        # "Cash = loss": hold unless there's a better buy this tick.
                        if needs_repl and not has_candidate:
                            continue
                        # Minimum hold: block intraday signal exits to avoid
                        # curve-shift noise losses. Mirrors _simulate_bond_day.
                        if min_hold > 0:
                            edate = pos[sym]["entry_date"]
                            if _yyyymmdd_diff(edate, date_int) < min_hold:
                                continue
                        bids = _ladder(snap, "bid")
                        if not bids:
                            continue
                        # Cap the sell at the edge price (yield == curve+exit_bps)
                        # so the position is never dumped into cheap deep bids.
                        floor = price_zero_coupon(
                            curve_y + exit_bps / 10_000.0, fv, dtm)
                        units, notional = _sell_against_bids(bids, floor, pos[sym]["units"])
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
        "trades": [_trade_to_dict(t, p) for t in all_trades],
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
    "entry_max_bps":        [80, 120, 150],  # sanity-band ceiling
    "min_dtm":              [15, 30],        # near-maturity exclusion (days)
    "min_hold_days":        [0, 1, 2],       # minimum calendar hold before signal exit
    # All three below are replay-time only → no extra stream-build cost
    "force_eod":            [False, True],   # بستن اجباری پایان روز
    "min_exit_profit_bps":  [-1.0, 0.0],    # فقط خروج سودده (-1=off, 0=break-even)
    "exit_needs_replacement":[False, True],  # خروج فقط با جایگزین (نقد نمان)
}
# Phase-2 refinement: ±step around best coarse results (entry/exit only)
_FINE_STEP = {"entry_bps": 8, "exit_bps": 4}


# ── Optimizer fast path: trigger-compressed batch replay + fork parallelism ──
#
# _replay_stream walks EVERY (tick × series) event for EVERY combo.  With the
# full COARSE_GRID that is ~75k replays over the whole stream — hours of
# pure-Python iteration.  Two observations collapse this:
#
#   1. A combo only ACTS where a signal threshold is crossed.  One pass per
#      group extracts, for each distinct entry gate (entry_bps, entry_max_bps)
#      and each distinct exit_bps, the sparse list of trigger points; replaying
#      a combo then costs O(its triggers) instead of O(all events).  Skipping
#      non-trigger items is lossless: entry and exit triggers are mutually
#      exclusive per (tick, sym) — exit_bps < entry_bps and ytm_bid ≥ ytm_ask —
#      and a non-trigger item can never change _replay_stream's state.
#
#   2. Groups (stream build + their replays) are independent → fork()ed
#      workers inherit the heavy day-cache copy-on-write (no pickling) and
#      spread the groups across CPU cores.
#
# Parity contract: _replay_triggers must produce BIT-IDENTICAL trades to
# _replay_stream for the same params — enforced by test_optimizer_parity.py.

def _combo_params(base: BondBacktestParams, c: tuple) -> BondBacktestParams:
    """Materialise an 11-dim optimizer combo tuple into full params.

    Combo layout: 0:degree 1:minpts 2:entry 3:exit 4:step 5:entry_max
    6:min_dtm 7:min_hold 8:force_eod 9:min_exit_profit 10:exit_needs_repl.
    """
    return BondBacktestParams(
        capital=base.capital, entry_bps=float(c[2]), exit_bps=float(c[3]),
        degree=int(c[0]), min_curve_points=int(c[1]),
        step_secs=int(c[4]), force_eod=bool(c[8]),
        include_matured=base.include_matured,
        buy_fee=base.buy_fee, sell_fee=base.sell_fee,
        strategy=base.strategy,
        min_exit_profit_bps=float(c[9]),
        total_capital=base.total_capital,
        max_position_pct=base.max_position_pct,
        signal_price=base.signal_price,
        entry_max_bps=float(c[5]),
        curve_trim_bps=base.curve_trim_bps,
        min_dtm=int(c[6]),
        exit_needs_replacement=bool(c[10]),
        min_hold_days=int(c[7]))


def _combo_result(p: BondBacktestParams, trades, tested: int,
                  min_trades: int, opt_metric: str, buy_fee: float) -> dict:
    summary = _summarize(trades, buy_fee)
    metrics = _risk_metrics(trades, buy_fee)
    return {
        "params":      asdict(p),
        "summary":     summary,
        "metrics":     metrics,
        "score":       round(_objective_v2(summary, metrics, min_trades, opt_metric), 4),
        "days_tested": tested,
    }


def _result_sort_key(r: dict):
    """Deterministic ranking: score desc, then params asc — so the unordered
    parallel collection always sorts identically to a sequential run."""
    p = r["params"]
    return (-r["score"], p["degree"], p["min_curve_points"], p["entry_bps"],
            p["exit_bps"], p["step_secs"], p["entry_max_bps"], p["min_dtm"],
            p["min_hold_days"], p["force_eod"], p["min_exit_profit_bps"],
            p["exit_needs_replacement"])


def _date_ordinal(d: int) -> int:
    """YYYYMMDD → proleptic ordinal; diff equals _yyyymmdd_diff exactly."""
    import datetime as _dt
    return _dt.date(d // 10000, (d // 100) % 100, d % 100).toordinal()


def _index_stream_triggers(stream: dict, gates, exits, exec_mode: bool) -> list:
    """One pass over a group's decision stream → per-day sparse trigger lists.

    *gates* is the set of (entry_bps, entry_max_bps) pairs and *exits* the set
    of exit_bps values used by this group's combos.  Returns one element per
    stream day: None for skipped days, else a dict with
      ent : gate → ([(ord, t, sym, z_sig, curve_y, y, asks), …], [ceiling, …])
      cand: gate → {t: [sym, …]}      (entry-trigger syms per tick, for the
                                       exit_needs_replacement gate)
      exi : exit_bps → ([(ord, t, sym, z_sig, bids), …], [floor, …])
    ``ord`` is the item's global ordinal in the day so a per-combo merge of its
    two lists reproduces _replay_stream's exact iteration order.

    Everything a replay would recompute per combo is hoisted here, paid once
    per (gate, trigger) instead of once per (combo, trigger):
      - the OB ladder is resolved (memoised per snapshot identity) and embedded;
        triggers whose needed ladder is empty are pure no-ops in _replay_stream
        ("if not asks/bids: continue") and are dropped outright — except that an
        empty-ask item must still register as a replacement CANDIDATE, because
        has_candidate never looks at the ladder;
      - the edge price (price_zero_coupon → pow) is precomputed per distinct
        entry/exit threshold into a parallel array.
    Trigger tuples are shared by reference across overlapping gates.
    """
    gates = sorted(set(gates))
    exits = sorted(set(exits))
    min_entry = min((g[0] for g in gates), default=math.inf)
    max_exit = max(exits, default=-math.inf)
    out = []
    lad_ask_memo: dict = {}
    lad_bid_memo: dict = {}
    for day in stream["days"]:
        if day["skipped"]:
            out.append(None)
            continue
        ent = {g: ([], []) for g in gates}
        cand = {g: {} for g in gates}
        exi = {x: ([], []) for x in exits}
        ordn = 0
        for t, items in day["events"]:
            for it in items:
                if exec_mode:
                    curve_y = it[3]
                    ya = it[5]
                    ze = (ya - curve_y) * 10_000.0 if ya > 0.0 else None
                    yb = it[6]
                    zx = (yb - curve_y) * 10_000.0 if yb > 0.0 else None
                else:
                    ze = zx = it[1]
                if ze is not None and ze >= min_entry:
                    trig = None
                    no_asks = False
                    ceil_by_e: dict = {}
                    for g in gates:
                        if ze >= g[0] and (g[1] <= 0.0 or ze <= g[1]):
                            # Candidate registration is ladder-independent.
                            cg = cand[g]
                            lst = cg.get(t)
                            if lst is None:
                                cg[t] = [it[0]]
                            else:
                                lst.append(it[0])
                            if no_asks:
                                continue
                            if trig is None:
                                snap = it[4]
                                sid = id(snap)
                                asks = lad_ask_memo.get(sid)
                                if asks is None:
                                    asks = _ladder(snap, "ask")
                                    lad_ask_memo[sid] = asks
                                if not asks:
                                    no_asks = True
                                    continue
                                trig = (ordn, t, it[0], ze, it[3], it[2], asks)
                            e = g[0]
                            c = ceil_by_e.get(e)
                            if c is None:
                                c = price_zero_coupon(
                                    it[3] + e / 10_000.0, it[7], it[8])
                                ceil_by_e[e] = c
                            tl, cl = ent[g]
                            tl.append(trig)
                            cl.append(c)
                if zx is not None and zx <= max_exit:
                    snap = it[4]
                    sid = id(snap)
                    bids = lad_bid_memo.get(sid)
                    if bids is None:
                        bids = _ladder(snap, "bid")
                        lad_bid_memo[sid] = bids
                    if bids:
                        trig = (ordn, t, it[0], zx, bids)
                        for x in exits:
                            if zx <= x:
                                f = price_zero_coupon(
                                    it[3] + x / 10_000.0, it[7], it[8])
                                tl, fl = exi[x]
                                tl.append(trig)
                                fl.append(f)
                ordn += 1
        out.append({"date": day["date"], "eod_snaps": day["eod_snaps"],
                    "ent": ent, "cand": cand, "exi": exi})
    return out


def _replay_triggers(tdays: list, stream: dict, p: BondBacktestParams,
                     dord: dict) -> list[BondTrade]:
    """Sparse replay over this combo's trigger points only.

    Bit-identical to _replay_stream for the same params (parity-tested).
    Ladders and edge prices come pre-resolved from _index_stream_triggers;
    *dord* maps date→ordinal for the min_hold_days calendar check.
    """
    entry_bps, exit_bps = p.entry_bps, p.exit_bps
    force_eod = p.force_eod
    needs_repl = p.exit_needs_replacement
    min_hold = p.min_hold_days
    gate = (entry_bps, p.entry_max_bps)
    trades: list[BondTrade] = []
    pos: dict[str, dict] = {}
    use_pf = p.total_capital > 0
    portfolio = {"cash": p.total_capital} if use_pf else None
    # Inlined _buy_budget for the sym-not-in-pos case (cur == 0.0):
    #   portfolio: min(total×maxpos − 0, cash/(1+fee));  legacy: capital − 0.
    fee1 = 1.0 + p.buy_fee
    cap0 = p.total_capital * p.max_position_pct - 0.0
    legacy_budget = p.capital - 0.0

    def _close(sym, st, units, notional, t, exit_date, z, reason):
        _record_close(sym, st, units, notional, t, exit_date, z, reason,
                      p, trades, portfolio)
        if st["units"] <= 0:
            pos.pop(sym, None)

    for td in tdays:
        if td is None:
            continue                        # skipped day: positions carry
        date_int = td["date"]
        eb = td["ent"].get(gate)
        elist, eceil = eb if eb is not None else ((), ())
        xb = td["exi"].get(exit_bps)
        xlist, xfloor = xb if xb is not None else ((), ())
        cand_by_t = td["cand"].get(gate, {})
        i, j, ne, nx = 0, 0, len(elist), len(xlist)
        cur_t = None
        # has_candidate must see the TICK-START position set (the original
        # computes it before the item loop).  pos only changes when THIS combo
        # trades, so the copy is deferred until a trade actually fires in the
        # tick; until then live pos membership IS the tick-start membership.
        tick_pos = None
        while i < ne or j < nx:
            # Exit triggers are dense (any bond at/below curve+exit_bps emits
            # one every tick) but are all no-ops while flat — bulk-skip them
            # up to the next entry trigger whenever no position is open.
            if not pos and j < nx:
                if i >= ne:
                    break
                nxt = elist[i][0]
                while j < nx and xlist[j][0] < nxt:
                    j += 1
            if j >= nx or (i < ne and elist[i][0] < xlist[j][0]):
                trg = elist[i]
                aux = eceil[i]
                i += 1
                is_entry = True
            else:
                trg = xlist[j]
                aux = xfloor[j]
                j += 1
                is_entry = False
            t = trg[1]
            if t != cur_t:
                cur_t = t
                tick_pos = None
            if is_entry:
                sym = trg[2]
                if sym in pos:
                    continue        # original runs the exit branch here: no-op
                if use_pf:
                    cash_cap = portfolio["cash"] / fee1
                    budget = cap0 if cap0 < cash_cap else cash_cap
                else:
                    budget = legacy_budget
                if budget <= 0:
                    continue
                asks = trg[6]
                # budget < best-ask price ⟺ _buy_against_asks would return
                # (0, 0) on its first level — skip the call (exact equivalent).
                if budget < asks[0][0]:
                    continue
                units, notional = _buy_against_asks(asks, aux, budget)
                if units > 0:
                    if needs_repl and tick_pos is None:
                        tick_pos = set(pos)
                    if use_pf:
                        portfolio["cash"] -= notional * (1.0 + p.buy_fee)
                    pos[sym] = {
                        "units": units, "buy_notional": notional,
                        "entry_date": date_int, "entry_time": t,
                        "entry_ytm": trg[5], "entry_curve": trg[4],
                        "entry_z": trg[3],
                    }
            else:
                sym = trg[2]
                if sym not in pos:
                    continue        # original runs the entry branch here: no-op
                if needs_repl:
                    ref = pos if tick_pos is None else tick_pos
                    has_candidate = False
                    for cs in cand_by_t.get(t, ()):
                        if cs not in ref:
                            has_candidate = True
                            break
                    if not has_candidate:
                        continue
                if min_hold > 0:
                    if dord[date_int] - dord[pos[sym]["entry_date"]] < min_hold:
                        continue
                units, notional = _sell_against_bids(trg[4], aux, pos[sym]["units"])
                if units > 0 and _exit_clears_min(pos[sym], units, notional, p):
                    if needs_repl and tick_pos is None:
                        tick_pos = set(pos)
                    _close(sym, pos[sym], units, notional, t, date_int,
                           trg[3], "signal")
        if force_eod:
            for sym in list(pos.keys()):
                snap = td["eod_snaps"].get(sym)
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

    # End-of-backtest forced close (matches _replay_stream).
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
            exit_d = int(snap.get("date", 0)) or final_date
            held = st["units"]
            units, notional = _sell_against_bids(bids, 0, held)
            if units < held:
                notional += (held - units) * bids[-1][0]
                units = held
            _record_close(sym, st, units, notional, t, exit_d, 0.0, "final",
                          p, trades, portfolio)

    return trades


# Inputs for _opt_group_worker.  Populated by _c2f_optimize before any worker
# runs (and before fork, so child processes inherit the heavy day-cache
# copy-on-write instead of pickling it).  The web layer serialises optimizer
# runs, so a single shared slot is safe.
_OPT_SHARED: dict = {}


def _spawn_init(shared: dict) -> None:
    """Pool initializer for spawn-based workers (Windows / no fork).

    Spawned children re-import this module with an empty _OPT_SHARED, so the
    inputs (including the pickled day-cache) are shipped once per worker."""
    _OPT_SHARED.clear()
    _OPT_SHARED.update(shared)


def _opt_group_worker(task):
    """Evaluate one stream-group's combos: build the group's decision stream,
    index its triggers, then sparse-replay every combo.  Runs inside worker
    processes — must not log or touch the DB."""
    gkey, combos = task
    sh = _OPT_SHARED
    dates, cache, base = sh["dates"], sh["cache"], sh["base"]
    opt_metric, min_trades = sh["opt_metric"], sh["min_trades"]
    counter = sh.get("counter")
    progress, lock = sh.get("progress"), sh.get("lock")
    buy_fee = base.buy_fee

    def _tick():
        if counter is not None:
            with counter.get_lock():
                counter.value += 1
        elif progress is not None:
            with lock:
                progress["done"] += 1

    results = []
    if base.strategy == "outlier":
        # Per-order outlier z depends on the OB ladders, not just the mid,
        # so the decision-stream shortcut doesn't apply — simulate per combo.
        for c in combos:
            p = _combo_params(base, c)
            trades, tested, _ = _simulate_cache(dates, cache, p)
            results.append(_combo_result(p, trades, tested, min_trades,
                                         opt_metric, buy_fee))
            _tick()
        return results

    degree, minpts, step, mdtm = gkey
    tmpl = BondBacktestParams(
        capital=base.capital, degree=int(degree),
        min_curve_points=int(minpts), step_secs=int(step),
        force_eod=base.force_eod, include_matured=base.include_matured,
        buy_fee=base.buy_fee, sell_fee=base.sell_fee,
        signal_price=base.signal_price, curve_trim_bps=base.curve_trim_bps,
        min_dtm=int(mdtm))
    stream = _build_decision_stream(dates, cache, tmpl)
    exec_mode = (base.signal_price == "exec")
    gates = {(float(c[2]), float(c[5])) for c in combos}
    exits = {float(c[3]) for c in combos}
    tdays = _index_stream_triggers(stream, gates, exits, exec_mode)
    dord = {td["date"]: _date_ordinal(td["date"]) for td in tdays if td is not None}
    tested = stream["tested"]
    for c in combos:
        p = _combo_params(base, c)
        trades = _replay_triggers(tdays, stream, p, dord)
        results.append(_combo_result(p, trades, tested, min_trades,
                                     opt_metric, buy_fee))
        _tick()
    return results


def _run_combo_tasks(combos: list, progress: dict | None, lock,
                     n_jobs: int) -> list[dict]:
    """Group combos by stream key (0,1,4,6) and evaluate the groups — in
    parallel via fork when available, else sequentially in-process.  Returns
    the flat, unsorted result list."""
    groups: dict = {}
    for c in combos:
        groups.setdefault((c[0], c[1], c[4], c[6]), []).append(c)
    tasks = sorted(groups.items())
    if not tasks:
        return []

    jobs = n_jobs if n_jobs > 0 else max(1, (os.cpu_count() or 2) - 1)
    jobs = min(jobs, len(tasks))
    out: list = []

    if jobs > 1:
        import multiprocessing as _mp
        import threading as _th
        methods = _mp.get_all_start_methods()
        # fork (Linux/WSL): children inherit the day-cache copy-on-write.
        # spawn (Windows): children re-import the module; the inputs are
        # pickled once per worker via the pool initializer — slower startup,
        # but the replays still run fully parallel.
        method = os.environ.get("BOND_OPT_START_METHOD") or (
            "fork" if "fork" in methods else
            "spawn" if "spawn" in methods else "")
        if method in methods:
            ctx = _mp.get_context(method)
            done0 = progress.get("done", 0) if progress is not None else 0
            counter = ctx.Value("q", done0)
            if method == "fork":
                _OPT_SHARED["counter"] = counter
                init, initargs = None, ()
            else:
                shared = {k: _OPT_SHARED[k] for k in
                          ("dates", "cache", "base", "opt_metric", "min_trades")}
                shared.update({"counter": counter, "progress": None, "lock": None})
                init, initargs = _spawn_init, (shared,)
            stop = _th.Event()

            def _poll():
                while not stop.wait(0.4):
                    if progress is not None:
                        with lock:
                            progress["done"] = counter.value

            poller = _th.Thread(target=_poll, daemon=True)
            poller.start()
            try:
                with ctx.Pool(jobs, initializer=init, initargs=initargs) as pool:
                    for res in pool.imap_unordered(_opt_group_worker, tasks,
                                                   chunksize=1):
                        out.extend(res)
                if progress is not None:
                    with lock:
                        progress["done"] = counter.value
                return out
            except Exception:
                logger.exception(
                    "parallel bond optimize failed — sequential fallback")
                out = []
                if progress is not None:
                    with lock:
                        progress["done"] = done0
            finally:
                stop.set()
                poller.join(timeout=1)
                _OPT_SHARED["counter"] = None

    for task in tasks:
        out.extend(_opt_group_worker(task))
    return out


def _c2f_optimize(dates: list, cache: dict, base: BondBacktestParams,
                  opt_metric: str, min_trades: int,
                  top_k: int = 5,
                  progress: dict | None = None,
                  lock=None, n_jobs: int = 0) -> list[dict]:
    """Two-phase coarse-to-fine search. Returns all evaluated combos sorted by score.

    Combos are grouped by (degree, min_curve_points, step_secs, min_dtm) — the
    only stream-shaping dims.  Each group builds its decision stream once,
    compresses it into per-threshold trigger lists, and sparse-replays every
    combo (see the fast-path section above).  Groups run in parallel across
    CPU cores via fork.  ``n_jobs``: 0 = auto (cores − 1), 1 = sequential.
    """
    import threading
    _lock = lock or threading.Lock()

    # Stage worker inputs BEFORE any fork so children inherit the day-cache
    # copy-on-write.  The web layer serialises optimizer runs → safe to share.
    _OPT_SHARED.clear()
    _OPT_SHARED.update({
        "dates": dates, "cache": cache, "base": base,
        "opt_metric": opt_metric, "min_trades": min_trades,
        "progress": progress, "lock": _lock, "counter": None,
    })

    def _run_combos(combos):
        return _run_combo_tasks(combos, progress, _lock, n_jobs)

    # Phase 1 — coarse grid
    # Combo tuple (11 elements):
    #   0:degree  1:minpts  2:entry  3:exit  4:step  5:entry_max  6:min_dtm
    #   7:min_hold  8:force_eod  9:min_exit_profit  10:exit_needs_replacement
    coarse = [
        (deg, minp, ent, ex, stp, emax, mdtm, mhold, feod, mep, nr)
        for deg, minp, ent, ex, stp, emax, mdtm, mhold, feod, mep, nr
        in itertools.product(
            COARSE_GRID["degree"],               COARSE_GRID["min_curve_points"],
            COARSE_GRID["entry_bps"],            COARSE_GRID["exit_bps"],
            COARSE_GRID["step_secs"],            COARSE_GRID["entry_max_bps"],
            COARSE_GRID["min_dtm"],              COARSE_GRID["min_hold_days"],
            COARSE_GRID["force_eod"],            COARSE_GRID["min_exit_profit_bps"],
            COARSE_GRID["exit_needs_replacement"],
        )
        if ex < ent
    ]
    if progress is not None:
        with _lock:
            progress.update({"done": 0, "total": len(coarse), "phase": "coarse"})

    seen: set = set(coarse)
    results: list[dict] = _run_combos(coarse)
    results.sort(key=_result_sort_key)

    # Phase 2 — fine-grid zoom around top-K coarse winners.
    # Only entry_bps/exit_bps are perturbed; entry_max/min_dtm/min_hold stay
    # fixed at the best-combo value found in phase 1.
    fine_set: set = set()
    for r in results[:top_k]:
        p = r["params"]
        for de in [-_FINE_STEP["entry_bps"], _FINE_STEP["entry_bps"]]:
            for dx in [-_FINE_STEP["exit_bps"], _FINE_STEP["exit_bps"]]:
                ne = p["entry_bps"] + de
                nx = p["exit_bps"] + dx
                if nx < ne and ne > 5:
                    fine_set.add((
                        p["degree"], p["min_curve_points"], ne, nx, p["step_secs"],
                        p["entry_max_bps"], p["min_dtm"], p["min_hold_days"],
                        p["force_eod"], p["min_exit_profit_bps"],
                        p["exit_needs_replacement"],
                    ))
    fine_new = [c for c in fine_set if c not in seen]

    if fine_new and progress is not None:
        with _lock:
            progress["total"] = progress.get("total", 0) + len(fine_new)
            progress["phase"] = "fine"

    if fine_new:
        results.extend(_run_combos(fine_new))

    _OPT_SHARED.clear()      # release the day-cache reference
    results.sort(key=_result_sort_key)
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
            step_secs=int(bp.get("step_secs", 0)),
            include_matured=base.include_matured,
            buy_fee=base.buy_fee, sell_fee=base.sell_fee, strategy=base.strategy,
            # All the following use the optimised best_params values so stability
            # is measured around the actual best point, not the UI defaults.
            force_eod=bool(bp.get("force_eod", base.force_eod)),
            min_exit_profit_bps=float(bp.get("min_exit_profit_bps", base.min_exit_profit_bps)),
            entry_max_bps=float(bp.get("entry_max_bps", base.entry_max_bps)),
            curve_trim_bps=base.curve_trim_bps,
            min_dtm=int(bp.get("min_dtm", base.min_dtm)),
            exit_needs_replacement=bool(bp.get("exit_needs_replacement", base.exit_needs_replacement)),
            min_hold_days=int(bp.get("min_hold_days", base.min_hold_days)))
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
        # Carry every optimised dimension from the best combo — walk-forward
        # must validate the actual best point, not the base UI defaults.
        return BondBacktestParams(
            capital=base.capital,
            entry_bps=float(p_dict["entry_bps"]),
            exit_bps=float(p_dict["exit_bps"]),
            degree=int(p_dict["degree"]),
            min_curve_points=int(p_dict["min_curve_points"]),
            step_secs=int(p_dict.get("step_secs", 0)),
            force_eod=bool(p_dict.get("force_eod", base.force_eod)),
            include_matured=base.include_matured,
            buy_fee=base.buy_fee,
            sell_fee=base.sell_fee,
            strategy=base.strategy,
            min_exit_profit_bps=float(p_dict.get("min_exit_profit_bps",
                                                 base.min_exit_profit_bps)),
            total_capital=base.total_capital,
            max_position_pct=base.max_position_pct,
            signal_price=base.signal_price,
            entry_max_bps=float(p_dict.get("entry_max_bps", base.entry_max_bps)),
            curve_trim_bps=base.curve_trim_bps,
            min_dtm=int(p_dict.get("min_dtm", base.min_dtm)),
            exit_needs_replacement=bool(p_dict.get("exit_needs_replacement",
                                                   base.exit_needs_replacement)),
            min_hold_days=int(p_dict.get("min_hold_days", base.min_hold_days)))

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
                           progress_lock=None,
                           n_jobs: int = 0) -> dict:
    """Advanced coarse-to-fine اخزا parameter optimizer.

    Phases
    ------
    1. **Coarse** — evaluate every COARSE_GRID combo (~75k after validity
       filter) via trigger-compressed batch replay, parallel across cores
       (``n_jobs``: 0 = auto, 1 = sequential).
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
        top_k=5, progress=progress, lock=progress_lock, n_jobs=n_jobs)

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
        "date_from":      dates[0]  if dates else None,
        "date_to":        dates[-1] if dates else None,
        "best":           best,
        "top":            all_results[:top_n],
    }
