"""Backtest: buy ETFs trading BELOW NAV, hold, exit when the discount closes.

Idea
----
When an exchange-traded fund trades below its NAV, buy it on the secondary
market (no creation/redemption) and later change position — sell — once the
price has moved back toward NAV, a time limit is hit, or a stop-loss triggers.

Data
----
``nav_intraday`` (imported from the PostgreSQL dump by
``tools/import_nav_dump.py``): ~16 snapshots per fund per day with the NAV that
was *visible at that moment*, the last traded price and the cumulative day
volume.  Because the NAV is the one on screen at the snapshot, the signal has no
look-ahead: at every snapshot we only use that row.

Honest P&L
----------
Profit is ALWAYS computed from traded prices, never from NAV:

* buy  at  last · (1 + half_spread)      (assumed spread — no order book here)
* sell at  last · (1 − half_spread)
* fees:    BUYER_COMMISSION on the buy, SELLER_COMMISSION + SELLER_TAX on the sell
* size:    min(capital, participation% of that day's total traded volume)
* a quote is only used when the cumulative volume grew since the previous
  snapshot (``require_fresh``) so a stale "last" price never fills an order.

NAV only decides *when* to trade.  For gold / equity funds the published NAV can
be stale (the underlying moved after it was computed), so a "discount" may just
be market movement; ``max_nav_age_days`` and the per-fund NAV-age statistics in
``discount_stats`` exist to expose that.

Rules (all parameters)
----------------------
entry : last ≤ NAV·(1 − entry_discount_pct/100)            → buy
exit  : last ≥ NAV·(1 − exit_discount_pct/100)             → sell  ("signal")
        held ≥ max_hold_days calendar days                  → sell  ("time")
        bid  ≤ entry_price·(1 − stop_loss_pct/100)          → sell  ("stop")
        data ends while holding                             → sell  ("end")
After any exit the same fund is not re-entered on the same calendar day.
Each fund is an independent sleeve with ``capital`` rials; results are summed.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import logging
import statistics
import threading
from dataclasses import dataclass, asdict, replace

from config import BUYER_COMMISSION, SELLER_COMMISSION, SELLER_TAX

logger = logging.getLogger(__name__)

CATEGORIES = {
    "fi":     "درآمد ثابت",
    "equity": "سهامی",
    "gold":   "طلا",
    "other":  "سایر / نامشخص",
}


# --------------------------------------------------------------------------- #
#  Parameters & results                                                        #
# --------------------------------------------------------------------------- #

@dataclass
class DiscountParams:
    capital: float = 1_000_000_000      # rials per fund sleeve
    entry_discount_pct: float = 0.50    # buy when price ≤ NAV·(1−this/100)
    exit_discount_pct: float = 0.0      # sell when price ≥ NAV·(1−this/100); <0 = wait for premium
    max_hold_days: int = 10             # calendar days; 0 = no limit
    stop_loss_pct: float = 0.0          # 0 = off
    half_spread_pct: float = 0.05       # assumed half bid-ask spread (each side)
    participation_pct: float = 5.0      # max share of the day's volume we can trade; 0 = unlimited
    require_fresh: bool = True          # only trade on snapshots where volume grew
    max_nav_age_days: int = 3           # ignore snapshots whose NAV is older than this
    session_start: int = 90000          # HHMMSS (Tehran)
    session_end: int = 123000
    buy_fee: float = BUYER_COMMISSION
    sell_fee: float = SELLER_COMMISSION + SELLER_TAX


@dataclass
class Trade:
    symbol: str
    entry_date: int
    entry_time: int
    exit_date: int
    exit_time: int
    volume: int
    entry_price: float
    exit_price: float
    nav_entry: float
    nav_exit: float
    disc_entry_pct: float       # (entry_price/NAV − 1)·100, negative = discount
    disc_exit_pct: float
    buy_notional: float
    sell_notional: float
    fees: float
    net_pnl: float
    net_pct: float
    hold_days: int
    exit_reason: str            # signal | time | stop | end


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

_ORD: dict[int, int] = {}


def _ord(date_int: int) -> int:
    o = _ORD.get(date_int)
    if o is None:
        try:
            o = _dt.date(date_int // 10000, (date_int // 100) % 100,
                         date_int % 100).toordinal()
        except ValueError:
            o = 0
        _ORD[date_int] = o
    return o


def _universe(db, cats: list[str] | None, symbols: list[str] | None) -> list[tuple[int, str]]:
    """Resolve the fund selection to [(symbol_id, label)].

    cats    : subset of {fi, equity, gold, other}; None/[] = every category.
              Funds that were never classified count as "other".
    symbols : optional explicit tickers / "#id" / ids — overrides ``cats``.
    """
    if not db.nav_intraday_available():
        raise ValueError("جدول nav_intraday وجود ندارد — اول tools/import_nav_dump.py را اجرا کنید.")
    names = db.get_nav_symbol_map()
    kinds = db.get_nav_symbol_category()
    ids = db.get_nav_intraday_ids()
    label = {i: names.get(i, f"#{i}") for i in ids}
    if symbols:
        want = {s.strip() for s in symbols if s.strip()}
        return [(i, label[i]) for i in ids if label[i] in want or str(i) in want]
    want_c = {c for c in (cats or []) if c in CATEGORIES}
    if not want_c:
        return [(i, label[i]) for i in ids]
    return [(i, label[i]) for i in ids if kinds.get(i, "other") in want_c]


def _prep(raw: list[tuple], p: DiscountParams):
    """Raw DB rows -> (rows, day_vol).

    rows: [(ordinal, date, time, nav, last, fresh)] — only in-session snapshots
    with a positive price/NAV and a NAV no older than ``max_nav_age_days``.
    day_vol: {date: total volume traded that day}.
    """
    rows = []
    day_vol: dict[int, int] = {}
    prev_vol = 0
    prev_date = 0
    for d, t, nav, nav_d, last, vol in raw:
        if d != prev_date:
            prev_date, prev_vol = d, 0
        vol = vol or 0
        if vol > day_vol.get(d, 0):
            day_vol[d] = vol
        fresh = vol > prev_vol
        prev_vol = max(prev_vol, vol)
        if not (p.session_start <= t <= p.session_end):
            continue
        if not last or last <= 0 or not nav or nav <= 0:
            continue
        o = _ord(d)
        if nav_d and p.max_nav_age_days >= 0 and o - _ord(nav_d) > p.max_nav_age_days:
            continue
        rows.append((o, d, t, nav, last, fresh))
    return rows, day_vol


# --------------------------------------------------------------------------- #
#  Simulation (one fund)                                                       #
# --------------------------------------------------------------------------- #

def _simulate(label: str, rows: list[tuple], day_vol: dict, p: DiscountParams) -> list[Trade]:
    trades: list[Trade] = []
    if not rows:
        return trades
    hs = p.half_spread_pct / 100.0
    ent_mult = 1.0 - p.entry_discount_pct / 100.0
    ex_mult = 1.0 - p.exit_discount_pct / 100.0
    stop_mult = 1.0 - p.stop_loss_pct / 100.0
    big = 1 << 60

    pos = 0
    cost = 0.0           # cost basis (excl. fees) of the units held
    e_ord = e_date = e_time = 0
    e_nav = 0.0
    e_px = 0.0
    blocked_date = 0

    def _cap(date_int: int) -> int:
        if p.participation_pct <= 0:
            return big
        return int(day_vol.get(date_int, 0) * p.participation_pct / 100.0)

    def _close(units: int, px: float, d: int, t: int, nav: float, reason: str):
        nonlocal pos, cost
        frac = units / pos
        cost_part = cost * frac
        sell_notional = units * px
        buy_fee = cost_part * p.buy_fee
        sell_fee = sell_notional * p.sell_fee
        invested = cost_part + buy_fee
        net = sell_notional - sell_fee - invested
        trades.append(Trade(
            symbol=label, entry_date=e_date, entry_time=e_time,
            exit_date=d, exit_time=t, volume=units,
            entry_price=round(cost_part / units, 2), exit_price=round(px, 2),
            nav_entry=round(e_nav, 2), nav_exit=round(nav, 2),
            disc_entry_pct=round((cost_part / units / e_nav - 1) * 100, 4) if e_nav else 0,
            disc_exit_pct=round((px / nav - 1) * 100, 4) if nav else 0,
            buy_notional=round(cost_part, 0), sell_notional=round(sell_notional, 0),
            fees=round(buy_fee + sell_fee, 0), net_pnl=round(net, 0),
            net_pct=round(net / invested * 100, 4) if invested else 0,
            hold_days=max(0, _ord(d) - e_ord), exit_reason=reason))
        pos -= units
        cost -= cost_part

    for o, d, t, nav, last, fresh in rows:
        if p.require_fresh and not fresh:
            continue
        if pos == 0:
            if d == blocked_date or last > nav * ent_mult:
                continue
            ask = last * (1 + hs)
            units = min(int(p.capital // ask), _cap(d))
            if units <= 0:
                continue
            pos, cost = units, units * ask
            e_ord, e_date, e_time, e_nav, e_px = o, d, t, nav, ask
        else:
            bid = last * (1 - hs)
            reason = None
            if p.max_hold_days > 0 and o - e_ord >= p.max_hold_days:
                reason = "time"
            elif p.stop_loss_pct > 0 and bid <= e_px * stop_mult:
                reason = "stop"
            elif last >= nav * ex_mult:
                reason = "signal"
            if reason:
                units = min(pos, _cap(d))
                if units > 0:
                    _close(units, bid, d, t, nav, reason)
                    if pos == 0:
                        blocked_date = d

    if pos > 0:                       # data ended while holding
        o, d, t, nav, last, _ = rows[-1]
        _close(pos, last * (1 - hs), d, t, nav, "end")
    return trades


# --------------------------------------------------------------------------- #
#  Summary                                                                     #
# --------------------------------------------------------------------------- #

def _summarize(trades: list[Trade], base_capital: float) -> dict:
    if not trades:
        return {"trade_count": 0, "win_count": 0, "loss_count": 0, "win_rate": 0,
                "total_net_pnl": 0, "total_invested": 0, "total_return_pct": 0,
                "annualized_pct": 0, "avg_net_pct": 0, "best_pct": 0, "worst_pct": 0,
                "avg_hold_days": 0, "total_fees": 0, "profit_factor": 0,
                "avg_disc_entry_pct": 0, "avg_disc_exit_pct": 0,
                "max_drawdown_pct": 0, "exit_reasons": {}}
    inv_fee = lambda t: t.buy_notional + (t.fees - t.fees)   # noqa: E731 (clarity)
    invested = sum(t.buy_notional for t in trades)
    net = sum(t.net_pnl for t in trades)
    wins = [t for t in trades if t.net_pnl > 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl <= 0)
    # capital-time weighted: rial-years actually deployed (≥1 day per trade)
    rial_years = sum(t.buy_notional * max(t.hold_days, 1) / 365.0 for t in trades)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    # drawdown of cumulative P&L ordered by exit time
    cum = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x.exit_date, x.exit_time)):
        cum += t.net_pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_net_pnl": round(net, 0),
        "total_invested": round(invested, 0),
        "total_return_pct": round(net / invested * 100, 4) if invested else 0,
        "annualized_pct": round(net / rial_years * 100, 2) if rial_years else 0,
        "avg_net_pct": round(sum(t.net_pct for t in trades) / len(trades), 4),
        "best_pct": round(max(t.net_pct for t in trades), 4),
        "worst_pct": round(min(t.net_pct for t in trades), 4),
        "avg_hold_days": round(sum(t.hold_days for t in trades) / len(trades), 2),
        "total_fees": round(sum(t.fees for t in trades), 0),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0,
        "avg_disc_entry_pct": round(sum(t.disc_entry_pct for t in trades) / len(trades), 3),
        "avg_disc_exit_pct": round(sum(t.disc_exit_pct for t in trades) / len(trades), 3),
        "max_drawdown_pct": round(dd / base_capital * 100, 3) if base_capital else 0,
        "exit_reasons": reasons,
    }


def _per_symbol(trades: list[Trade], capital: float) -> list[dict]:
    by: dict[str, list[Trade]] = {}
    for t in trades:
        by.setdefault(t.symbol, []).append(t)
    out = []
    for sym, ts in by.items():
        s = _summarize(ts, capital)
        out.append({"symbol": sym, "trade_count": s["trade_count"],
                    "win_rate": s["win_rate"], "total_net_pnl": s["total_net_pnl"],
                    "total_return_pct": s["total_return_pct"],
                    "annualized_pct": s["annualized_pct"],
                    "avg_hold_days": s["avg_hold_days"]})
    out.sort(key=lambda r: -r["total_net_pnl"])
    return out


# --------------------------------------------------------------------------- #
#  Public: single backtest                                                     #
# --------------------------------------------------------------------------- #

def run_discount_backtest(db, cats: list[str] | None = None, symbols: list[str] | None = None,
                          start: int | None = None, end: int | None = None,
                          params: DiscountParams | None = None) -> dict:
    p = params or DiscountParams()
    funds = _universe(db, cats, symbols)
    all_trades: list[Trade] = []
    tested = skipped = 0
    for sid, label in funds:
        rows, day_vol = _prep(db.get_nav_intraday(sid, start, end), p)
        if not rows:
            skipped += 1
            continue
        tested += 1
        all_trades.extend(_simulate(label, rows, day_vol, p))
    all_trades.sort(key=lambda t: (t.entry_date, t.entry_time))
    return {
        "cats": cats or [],
        "funds_tested": tested, "funds_skipped": skipped,
        "params": asdict(p),
        "trades": [asdict(t) for t in all_trades],
        "per_symbol": _per_symbol(all_trades, p.capital),
        "summary": _summarize(all_trades, p.capital * max(tested, 1)),
    }


# --------------------------------------------------------------------------- #
#  Public: discount statistics (calibrate thresholds, spot stale NAVs)         #
# --------------------------------------------------------------------------- #

def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def discount_stats(db, cats: list[str] | None = None, symbols: list[str] | None = None,
                   start: int | None = None, end: int | None = None,
                   params: DiscountParams | None = None) -> dict:
    """Per-fund distribution of (price/NAV − 1)% and NAV freshness, plus the fund's category."""
    p = params or DiscountParams()
    rows_out = []
    kinds = db.get_nav_symbol_category()
    for sid, label in _universe(db, cats, symbols):
        raw = db.get_nav_intraday(sid, start, end)
        if not raw:
            continue
        rows, _ = _prep(raw, replace(p, max_nav_age_days=-1))
        rows = [r for r in rows if r[5]]                      # fresh only
        if len(rows) < 20:
            continue
        disc = sorted((last / nav - 1) * 100 for _, _, _, nav, last, _ in rows)
        # NAV freshness: age of the NAV at each raw snapshot, NAV changes per day
        ages, changes, days = [], 0, set()
        prev = None
        for d, t, nav, nav_d, last, vol in raw:
            if nav_d:
                ages.append(_ord(d) - _ord(nav_d))
            if prev is not None and nav != prev[0] and d == prev[1]:
                changes += 1
            prev = (nav, d)
            days.add(d)
        ent = -p.entry_discount_pct
        rows_out.append({
            "symbol": label, "category": kinds.get(sid, "other"),
            "snapshots": len(rows), "days": len(days),
            "median_disc_pct": round(_pct(disc, .5), 3),
            "p5_disc_pct": round(_pct(disc, .05), 3),
            "p95_disc_pct": round(_pct(disc, .95), 3),
            "share_below_entry_pct": round(sum(1 for x in disc if x <= ent) / len(disc) * 100, 1),
            "median_nav_age_days": statistics.median(ages) if ages else 0,
            "nav_changes_per_day": round(changes / max(len(days), 1), 2),
        })
    rows_out.sort(key=lambda r: -r["share_below_entry_pct"])
    return {"cats": cats or [], "entry_discount_pct": p.entry_discount_pct,
            "funds": rows_out}


# --------------------------------------------------------------------------- #
#  Public: optimizer with out-of-sample (walk-forward style) check             #
# --------------------------------------------------------------------------- #

GRID_DEFAULT = {
    "entry_discount_pct": [0.5, 0.75, 1.0, 1.5, 2.0],
    "exit_discount_pct":  [0.5, 0.25, 0.0, -0.25],
    "max_hold_days":      [3, 7, 15, 30],
    "stop_loss_pct":      [0.0, 1.5, 3.0],
}

METRICS = {
    "total_net_pnl":    "سود کل",
    "total_return_pct": "بازده کل ٪",
    "annualized_pct":   "بازده سالانهٔ سرمایهٔ درگیر ٪",
    "profit_factor":    "Profit Factor",
}


def _combos(base: DiscountParams, grid: dict) -> list[DiscountParams]:
    keys = list(grid)
    out = []
    for vals in itertools.product(*(grid[k] for k in keys)):
        c = replace(base, **dict(zip(keys, vals)))
        if c.exit_discount_pct >= c.entry_discount_pct - 0.05:
            continue                      # exit must be meaningfully above entry
        out.append(c)
    return out


def optimize_discount(db, cats: list[str] | None = None, symbols: list[str] | None = None,
                      start: int | None = None, end: int | None = None,
                      base: DiscountParams | None = None, opt_metric: str = "annualized_pct",
                      min_trades: int = 30, test_frac: float = 0.3, top_n: int = 15,
                      grid: dict | None = None, progress: dict | None = None,
                      progress_lock: threading.Lock | None = None) -> dict:
    """Grid-search entry/exit/hold/stop on the first (1−test_frac) of the dates,
    then report each top combo's result on the untouched last ``test_frac``."""
    base = base or DiscountParams()
    if opt_metric not in METRICS:
        opt_metric = "annualized_pct"
    combos = _combos(base, grid or GRID_DEFAULT)
    funds = _universe(db, cats, symbols)
    dates = db.get_nav_intraday_dates(start, end)
    if len(dates) < 10 or not funds:
        return {"error": "داده برای بهینه‌سازی کافی نیست"}
    cut = dates[int(len(dates) * (1 - test_frac))] if 0 < test_frac < 1 else dates[-1]
    train_tr = [[] for _ in combos]
    test_tr = [[] for _ in combos]
    if progress is not None:
        with (progress_lock or threading.Lock()):
            progress.update(done=0, total=len(funds), combos=len(combos), phase="grid")

    for k, (sid, label) in enumerate(funds):
        rows, day_vol = _prep(db.get_nav_intraday(sid, start, end), base)
        if rows:
            tr_rows = [r for r in rows if r[1] <= cut]
            te_rows = [r for r in rows if r[1] > cut]
            for ci, c in enumerate(combos):
                train_tr[ci].extend(_simulate(label, tr_rows, day_vol, c))
                if te_rows and test_frac > 0:
                    test_tr[ci].extend(_simulate(label, te_rows, day_vol, c))
        if progress is not None:
            with (progress_lock or threading.Lock()):
                progress["done"] = k + 1

    cap = base.capital * max(len(funds), 1)
    scored = []
    for ci, c in enumerate(combos):
        s_tr = _summarize(train_tr[ci], cap)
        if s_tr["trade_count"] < min_trades:
            continue
        scored.append({"params": asdict(c), "train": s_tr,
                       "test": _summarize(test_tr[ci], cap),
                       "_score": s_tr.get(opt_metric, 0)})
    scored.sort(key=lambda r: -r["_score"])
    for r in scored:
        r.pop("_score", None)
    return {
        "cats": cats or [], "funds": len(funds), "opt_metric": opt_metric,
        "tested_combos": len(combos), "qualified_combos": len(scored),
        "min_trades": min_trades, "test_frac": test_frac,
        "train_range": [dates[0], cut], "test_range": [cut, dates[-1]],
        "best": scored[0] if scored else None,
        "top": scored[:top_n],
    }
