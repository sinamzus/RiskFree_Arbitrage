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

import logging
from dataclasses import dataclass, asdict

from config import BUYER_COMMISSION, SELLER_COMMISSION, SELLER_TAX
from bonds import ytm_zero_coupon, fit_yield_curve, eval_curve, days_to_maturity
# Reuse the fund engine's validated execution primitives.
from backtest import _ladder, _buy_against_asks, _sell_against_bids, _mid, _secs

logger = logging.getLogger(__name__)

BUY_COST  = BUYER_COMMISSION
SELL_COST = SELLER_COMMISSION + SELLER_TAX


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
    force_eod: bool = True           # liquidate any open position at day end


@dataclass
class BondTrade:
    symbol: str
    date: int
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
    exit_reason: str          # "signal" | "eod"


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
#  Per-date simulation                                                          #
# --------------------------------------------------------------------------- #

def _simulate_bond_day(date_int: int,
                       day_series: dict[str, _SeriesDay],
                       p: BondBacktestParams) -> list[BondTrade]:
    """Cross-sectional z-spread state machine for one trading date.

    *day_series* maps symbol → _SeriesDay (each with that series' OB snapshots
    for *date_int* and its days-to-maturity).
    """
    # Build the merged, de-duplicated, sorted timeline of all snapshot times.
    times: set[int] = set()
    for sd in day_series.values():
        for s in sd.snaps:
            times.add(int(s.get("time", 0)))
    timeline = sorted(t for t in times if t > 0)
    if not timeline:
        return []

    if p.step_secs > 0:
        # Downsample: keep one time per step_secs bucket.
        kept, last_bucket = [], -1
        for t in timeline:
            b = _secs(t) // p.step_secs
            if b != last_bucket:
                kept.append(t)
                last_bucket = b
        timeline = kept

    trades: list[BondTrade] = []
    # Per-symbol open position state.
    pos: dict[str, dict] = {}   # symbol → {units, buy_notional, entry_time, entry_ytm, entry_curve, entry_z}

    def _close(sym: str, sd: _SeriesDay, exit_units: int, sell_notional: float,
               t_exit: int, exit_z: float, reason: str):
        st = pos[sym]
        frac = exit_units / st["units"] if st["units"] else 0
        cost_part = st["buy_notional"] * frac
        buy_fee  = cost_part * BUY_COST
        sell_fee = sell_notional * SELL_COST
        invested = cost_part + buy_fee
        net = (sell_notional - sell_fee) - invested
        trades.append(BondTrade(
            symbol=sym, date=date_int,
            entry_time=st["entry_time"], exit_time=t_exit,
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
            hold_secs=max(0, _secs(t_exit) - _secs(st["entry_time"])),
            exit_reason=reason,
        ))
        st["units"] -= exit_units
        st["buy_notional"] -= cost_part
        if st["units"] <= 0:
            pos.pop(sym, None)

    for t in timeline:
        # Advance every series' cursor to time t.
        for sd in day_series.values():
            sd.advance_to(t)

        # Fit the curve across all series that currently have a valid YTM.
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

        # Evaluate each series' z-spread and act.
        for sym, sd in day_series.items():
            y = sd.ytm()
            if y <= 0 or sd.dtm <= 0 or sd.cur is None:
                continue
            curve_y = eval_curve(coeffs, float(sd.dtm))
            z_bps = (y - curve_y) * 10_000.0

            if sym not in pos:
                # Entry: cheap bond (yields above the curve by entry_bps).
                if z_bps >= p.entry_bps:
                    asks = _ladder(sd.cur, "ask")
                    if not asks:
                        continue
                    # Buy any offer at-or-below the current price level; the
                    # signal already says the whole series is cheap, so accept
                    # the visible ask ladder up to capital.
                    ceiling = asks[-1][0]
                    units, notional = _buy_against_asks(asks, ceiling, p.capital)
                    if units > 0:
                        pos[sym] = {
                            "units": units, "buy_notional": notional,
                            "entry_time": t, "entry_ytm": y,
                            "entry_curve": curve_y, "entry_z": z_bps,
                        }
            else:
                # Exit: z-spread has reverted to/below exit threshold.
                if z_bps <= p.exit_bps:
                    bids = _ladder(sd.cur, "bid")
                    if not bids:
                        continue
                    floor = bids[-1][0]
                    units, notional = _sell_against_bids(bids, floor, pos[sym]["units"])
                    if units > 0:
                        _close(sym, sd, units, notional, t, z_bps, "signal")

    # ── Force-close any still-open positions at each series' last snapshot ──
    if p.force_eod:
        for sym in list(pos.keys()):
            sd = day_series[sym]
            if not sd.snaps:
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
            # exit z unknown at eod — report 0
            _close(sym, sd, units, notional, t, 0.0, "eod")

    return trades


# --------------------------------------------------------------------------- #
#  Summary + public API                                                        #
# --------------------------------------------------------------------------- #

def _summarize(trades: list[BondTrade]) -> dict:
    if not trades:
        return {"trade_count": 0, "win_count": 0, "loss_count": 0, "win_rate": 0,
                "total_net_pnl": 0, "total_invested": 0, "total_return_pct": 0,
                "avg_net_pct": 0, "best_pct": 0, "worst_pct": 0, "eod_count": 0,
                "avg_hold_min": 0, "total_fees": 0, "avg_entry_z": 0}
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
        "avg_entry_z": round(sum(t.entry_z_bps for t in trades) / len(trades), 1),
    }


def _resolve_universe(db, symbols):
    """Return (universe, meta) — symbol list + per-symbol face value & maturity."""
    from bonds import AKHZA_SERIES
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
    """Run the simulation over a pre-loaded day cache. Returns (trades, tested, skipped)."""
    trades: list[BondTrade] = []
    tested = skipped = 0
    for date_int in dates:
        rows = cache.get(date_int)
        if not rows:
            continue
        if len(rows) < p.min_curve_points:
            skipped += 1
            continue
        day_series = {sym: _SeriesDay(sym, face, dtm, snaps)
                      for (sym, face, dtm, snaps) in rows}
        tested += 1
        trades.extend(_simulate_bond_day(date_int, day_series, p))
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
    universe, meta = _resolve_universe(db, symbols)
    dates, cache = _load_day_cache(db, universe, meta, start_date, end_date)
    all_trades, days_tested, days_skipped = _simulate_cache(dates, cache, p)

    return {
        "symbols": universe,
        "params": asdict(p),
        "days_tested": days_tested,
        "days_skipped": days_skipped,
        "trades": [asdict(t) for t in all_trades],
        "summary": _summarize(all_trades),
    }


# --------------------------------------------------------------------------- #
#  Parameter optimizer                                                         #
# --------------------------------------------------------------------------- #

DEFAULT_GRID = {
    "degree":           [1, 2],          # yield-curve polynomial degree
    "min_curve_points": [3, 4],          # min simultaneous series to fit a curve
    "entry_bps":        [25, 40, 55, 70, 90],
    "exit_bps":         [-10, 0, 10, 20],
}


def _objective(summary: dict, min_trades: int) -> float:
    """Score a backtest summary. Higher is better.

    Combos with fewer than *min_trades* trades are pushed below everything
    statistically meaningful (but still ordered by trade count so an empty grid
    degrades gracefully).  Otherwise reward total return, lightly tie-break on
    win-rate, and nudge away from results that are mostly forced EOD exits
    (i.e. the signal never actually reverted).
    """
    tc = summary.get("trade_count", 0)
    if tc < min_trades:
        return -1000.0 + tc
    ret = summary.get("total_return_pct", 0.0)
    win = summary.get("win_rate", 0.0)
    eod = summary.get("eod_count", 0)
    eod_frac = eod / tc if tc else 1.0
    return ret + 0.01 * win - 0.5 * eod_frac


def optimize_bond_backtest(db, symbols: list[str] | None = None,
                           start_date: int | None = None,
                           end_date: int | None = None,
                           base: BondBacktestParams | None = None,
                           grid: dict | None = None,
                           min_trades: int = 3,
                           top_n: int = 10,
                           progress: dict | None = None,
                           progress_lock=None) -> dict:
    """Grid-search اخزا backtest parameters and rank by risk-adjusted return.

    The expensive OB history is loaded once; every parameter combination then
    re-simulates in memory.  Returns the best combination plus the *top_n*
    ranked results so the UI can show what was tried and let the user apply
    the winner.
    """
    import threading
    base = base or BondBacktestParams()
    grid = grid or DEFAULT_GRID
    universe, meta = _resolve_universe(db, symbols)
    dates, cache = _load_day_cache(db, universe, meta, start_date, end_date)

    combos = []
    for degree in grid["degree"]:
        for minpts in grid["min_curve_points"]:
            for entry in grid["entry_bps"]:
                for exit_ in grid["exit_bps"]:
                    if exit_ > entry:        # nonsensical: exit above entry
                        continue
                    combos.append((degree, minpts, entry, exit_))

    if progress is not None:
        with (progress_lock or threading.Lock()):
            progress.update({"done": 0, "total": len(combos)})

    results = []
    for (degree, minpts, entry, exit_) in combos:
        p = BondBacktestParams(
            capital=base.capital, entry_bps=float(entry), exit_bps=float(exit_),
            degree=int(degree), min_curve_points=int(minpts),
            step_secs=base.step_secs, force_eod=base.force_eod)
        trades, tested, skipped = _simulate_cache(dates, cache, p)
        summary = _summarize(trades)
        results.append({
            "params": asdict(p),
            "summary": summary,
            "score": round(_objective(summary, min_trades), 4),
            "days_tested": tested,
        })
        if progress is not None:
            with (progress_lock or threading.Lock()):
                progress["done"] += 1

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0] if results else None

    return {
        "symbols":       universe,
        "tested_combos": len(results),
        "min_trades":    min_trades,
        "days_available": len(dates),
        "best":          best,
        "top":           results[:top_n],
    }
