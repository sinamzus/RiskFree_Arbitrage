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

from bonds import ytm_zero_coupon, fit_yield_curve, eval_curve, days_to_maturity
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
    degree: int = 2                  # yield-curve polynomial degree
    min_curve_points: int = 3        # min series needed to fit a curve at an instant
    step_secs: int = 0               # 0 = every snapshot time; >0 = downsample grid
    force_eod: bool = False          # True = liquidate at day end; False = carry overnight
    include_matured: bool = True     # include اخزا already matured (as of today)
                                     # — on each date they only trade while alive
    buy_fee: float = BUY_COST        # buy-side commission (fraction, e.g. 0.00145)
    sell_fee: float = SELL_COST      # sell-side commission + tax (fraction)


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


# --------------------------------------------------------------------------- #
#  Per-series carry-forward price cursor                                        #
# --------------------------------------------------------------------------- #

class _SeriesDay:
    """Holds one series' OB snapshots for a date and a carry-forward cursor.

    ``advance_to(t)`` moves the cursor to the latest snapshot whose time is
    ≤ *t* (so we never peek into the future) and exposes that snapshot.
    """

    __slots__ = ("symbol", "face_value", "dtm", "snaps", "_i", "cur")

    def __init__(self, symbol: str, face_value: float, dtm: int,
                 snaps: list[dict]):
        self.symbol = symbol
        self.face_value = face_value
        self.dtm = dtm
        self.snaps = snaps          # sorted ascending by time
        self._i = -1
        self.cur: dict | None = None

    def advance_to(self, t: int) -> None:
        while self._i + 1 < len(self.snaps) and int(self.snaps[self._i + 1].get("time", 0)) <= t:
            self._i += 1
            self.cur = self.snaps[self._i]

    def price(self) -> float:
        return _mid(self.cur) if self.cur else 0.0

    def ytm(self) -> float:
        p = self.price()
        if p <= 0 or self.dtm <= 0:
            return 0.0
        return ytm_zero_coupon(p, self.face_value, self.dtm)


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
                  trades_out: list) -> None:
    """Build a BondTrade and mutate the position state dict in-place."""
    frac = exit_units / st["units"] if st["units"] else 0
    cost_part = st["buy_notional"] * frac
    buy_fee  = cost_part * p.buy_fee
    sell_fee = sell_notional * p.sell_fee
    invested = cost_part + buy_fee
    net = (sell_notional - sell_fee) - invested
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
    ))
    st["units"] -= exit_units
    st["buy_notional"] -= cost_part


# --------------------------------------------------------------------------- #
#  Per-date simulation                                                          #
# --------------------------------------------------------------------------- #

def _simulate_bond_day(date_int: int,
                       day_series: dict[str, _SeriesDay],
                       p: BondBacktestParams,
                       carry_pos: dict | None = None,
                       ) -> tuple[list[BondTrade], dict]:
    """Cross-sectional z-spread state machine for one trading date.

    *carry_pos* maps symbol → position dict carried forward from a prior day.
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
                      t_exit, date_int, exit_z, reason, p, trades)
        if st["units"] <= 0:
            pos.pop(sym, None)

    for t in timeline:
        for sd in day_series.values():
            sd.advance_to(t)

        pts: list[tuple[float, float]] = []
        for sd in day_series.values():
            y = sd.ytm()
            if y > 0 and sd.dtm > 0:
                pts.append((float(sd.dtm), y))
        if len(pts) < p.min_curve_points:
            continue
        coeffs = fit_yield_curve(pts, degree=p.degree)
        if not coeffs:
            continue

        for sym, sd in day_series.items():
            y = sd.ytm()
            if y <= 0 or sd.dtm <= 0 or sd.cur is None:
                continue
            curve_y = eval_curve(coeffs, float(sd.dtm))
            z_bps = (y - curve_y) * 10_000.0

            if sym not in pos:
                if z_bps >= p.entry_bps:
                    asks = _ladder(sd.cur, "ask")
                    if not asks:
                        continue
                    ceiling = asks[-1][0]
                    units, notional = _buy_against_asks(asks, ceiling, p.capital)
                    if units > 0:
                        pos[sym] = {
                            "units": units, "buy_notional": notional,
                            "entry_date": date_int, "entry_time": t,
                            "entry_ytm": y, "entry_curve": curve_y, "entry_z": z_bps,
                        }
            else:
                if z_bps <= p.exit_bps:
                    bids = _ladder(sd.cur, "bid")
                    if not bids:
                        continue
                    floor = bids[-1][0]
                    units, notional = _sell_against_bids(bids, floor, pos[sym]["units"])
                    if units > 0:
                        _close(sym, units, notional, t, z_bps, "signal")

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
        day_series = {sym: _SeriesDay(sym, face, dtm, snaps)
                      for (sym, face, dtm, snaps) in rows}
        tested += 1
        new_trades, carry_pos = _simulate_bond_day(date_int, day_series, p, carry_pos)
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
            held = st["units"]
            units, notional = _sell_against_bids(bids, 0, held)
            if units < held:
                notional += (held - units) * bids[-1][0]
                units = held
            _record_close(sym, st, units, notional, t, final_date, 0.0, "final", p, trades)

    return trades, tested, skipped


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
    "degree":           [1, 2, 3],
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
    """Two-phase coarse-to-fine search. Returns all evaluated combos sorted by score."""
    import threading
    _lock = lock or threading.Lock()

    def _eval(degree, minpts, entry, exit_, step):
        p = BondBacktestParams(
            capital=base.capital, entry_bps=float(entry), exit_bps=float(exit_),
            degree=int(degree), min_curve_points=int(minpts),
            step_secs=int(step), force_eod=base.force_eod,
            include_matured=base.include_matured,
            buy_fee=base.buy_fee, sell_fee=base.sell_fee)
        trades, tested, _ = _simulate_cache(dates, cache, p)
        summary = _summarize(trades, base.buy_fee)
        metrics = _risk_metrics(trades, base.buy_fee)
        return {
            "params":      asdict(p),
            "summary":     summary,
            "metrics":     metrics,
            "score":       round(_objective_v2(summary, metrics, min_trades, opt_metric), 4),
            "days_tested": tested,
        }

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

    seen: set = set()
    results: list[dict] = []
    for combo in coarse:
        seen.add(combo)
        results.append(_eval(*combo))
        if progress is not None:
            with _lock:
                progress["done"] += 1

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

    for combo in fine_new:
        seen.add(combo)
        results.append(_eval(*combo))
        if progress is not None:
            with _lock:
                progress["done"] += 1

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
            buy_fee=base.buy_fee, sell_fee=base.sell_fee)
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
            sell_fee=base.sell_fee)

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
