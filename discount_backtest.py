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
* fees:    0.12% on the buy and 0.12% on the sell (DiscountParams.buy_fee / sell_fee, editable in the UI)
* size:    position_pct % of the CURRENT capital (cash + cost of open positions),
           limited by cash, and by participation% of that day's total volume
* a quote is only used when the cumulative volume grew since the previous
  snapshot (``require_fresh``) so a stale "last" price never fills an order.

NAV only decides *when* to trade.  For gold / equity funds the published NAV can
be stale (the underlying moved after it was computed), so a "discount" may just
be market movement; ``max_nav_age_days`` and the per-fund NAV-age statistics in
``discount_stats`` exist to expose that.

Rules (all parameters)
----------------------
"fair value" F = NAV · (1 + baseline), where baseline is THIS fund's own typical
discount/premium (median of its end-of-day price/NAV−1 over the previous
``baseline_days`` trading days — past data only).  A fund that always trades 3 %
below NAV therefore only signals when it is cheaper than its own norm, and a fund
with a permanent +2 % premium can still signal on dips.  ``baseline_days = 0``
turns this off (F = NAV).

Bubble index B(t) = simple average, over the active funds, of each fund's current bubble
last/F − 1 (so each fund's own permanent premium/discount is already removed; with
baseline_days = 0 it is the raw price/NAV − 1).  Only data up to time t is used, funds
without a fresh quote today are ignored, and B is undefined until at least
``index_min_share`` of the funds have quoted that day.

Mean-reversion eligibility filter (optional, ``mr_center`` != "off"): a fund may be bought on a
day only if its bubble has been mean-reverting over the PREVIOUS ``mr_window_days`` trading days.
Score = expected share (0-100) of a deviation from the centre that closes within
``mr_horizon_days``, from the AR(1) coefficient of the intraday bubble series (lag ``mr_lag``
snapshots).  Centre: "zero" (raw price/NAV−1), "category" (simple average bubble of all funds
of the same category at that moment), "self" (the fund's own mean over the window).  A fund
below ``mr_min_score`` is excluded until its score recovers; open positions are not touched.

entry_mode = "fund"  (default) : the fund itself is cheap (rule below)
           = "index"           : B(t) ≤ −index_entry_pct/100  → buy EVERY active fund
           = "both"            : the fund is cheap AND B(t) ≤ −index_entry_pct/100
entry : last ≤ F·(1 − entry_discount_pct/100)              → buy   (mode "fund"/"both")
exit  : last ≥ F·(1 − exit_discount_pct/100)               → sell  ("signal"; mode "fund"/"both")
        B(t) ≥ −index_exit_pct/100                          → sell  ("signal"; mode "index")
        held ≥ max_hold_days calendar days                  → sell  ("time")
        NAV-based stop (stop_mode, stop_loss_pct = S):                 → sell  ("stop")
          nav_widen : last/F − 1 ≤ (last/F − 1 at entry) − S/100   (discount widened by S points)
          nav_level : last ≤ F·(1 − S/100)                          (discount reached S %)
          price     : bid ≤ entry_price·(1 − S/100)                 (classic price stop)
        data ends while holding                             → sell  ("end")
After any exit the same fund is not re-entered on the same calendar day.
One shared capital pool: every fund is simulated at its maximum liquidity, then all
trades are replayed in time order against a single account that starts with
``initial_capital`` and sizes each new position at ``position_pct`` % of the capital
at that moment (compounding).  If cash is short the position is scaled down (or
skipped when less than a quarter of the intended size is affordable).
"""

from __future__ import annotations

import bisect
import datetime as _dt
import itertools
import logging
import math
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
    initial_capital: float = 10_000_000_000   # rials, one shared pool
    position_pct: float = 10.0          # max % of the CURRENT capital in one position
    entry_discount_pct: float = 0.50    # buy when price ≤ NAV·(1−this/100)
    exit_discount_pct: float = 0.0      # sell when price ≥ NAV·(1−this/100); <0 = wait for premium
    max_hold_days: int = 10             # calendar days; 0 = no limit
    entry_mode: str = "fund"            # fund | index | both
    index_entry_pct: float = 0.30       # enter (index modes) when the bubble index ≤ −this %
    index_exit_pct: float = 0.0         # exit  (mode "index") when the bubble index ≥ −this %
    index_min_share: float = 0.5        # share of the funds that must have quoted today
    mr_center: str = "off"              # off | zero | category | self  (mean-reversion eligibility filter)
    mr_window_days: int = 20            # trading days of history the score is computed from
    mr_min_score: float = 70.0          # 0-100; below this the fund is not tradable that day
    mr_horizon_days: int = 5            # score = share of the gap expected to close within this
    mr_lag: int = 4                     # snapshots between the AR(1) pairs (≈ 1 hour)
    stop_loss_pct: float = 0.0          # 0 = off; meaning depends on stop_mode
    stop_mode: str = "nav_widen"        # nav_widen | nav_level | price
    baseline_days: int = 20             # per-fund typical discount window (trading days); 0 = off
    half_spread_pct: float = 0.05       # assumed half bid-ask spread (each side)
    participation_pct: float = 5.0      # max share of the day's volume we can trade; 0 = unlimited
    require_fresh: bool = True          # only trade on snapshots where volume grew
    max_nav_age_days: int = 3           # ignore snapshots whose NAV is older than this
    session_mode: str = "auto"          # auto = per fund & day from the volume | fixed = clock window below
    session_start: int = 90000          # HHMMSS (Tehran) — only for session_mode "fixed"
    session_end: int = 123000
    buy_fee: float = 0.0012             # 0.12% each side (all-in broker + exchange fee for these ETFs)
    sell_fee: float = 0.0012


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
    scale: float = 1.0          # share of the maximum-liquidity position actually taken
    rel_entry_pct: float = 0.0  # entry price vs the fund's own fair value (NAV·(1+baseline))
    rel_exit_pct: float = 0.0
    base_pct: float = 0.0       # the fund's typical discount used at entry (baseline, %)
    mr_score: float | None = None        # mean-reversion score of the fund at entry (None = filter off)
    idx_entry_pct: float | None = None   # bubble index at entry / exit (None = undefined)
    idx_exit_pct: float | None = None


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

_ORD: dict[int, int] = {}
_SIM_CAPITAL = 1e15      # per-fund simulation is liquidity-limited only; sizing happens in _replay


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
    dups = db.get_nav_dup_ids()
    all_ids = db.get_nav_intraday_ids()
    ids = [i for i in all_ids if i not in dups] if not symbols else all_ids
    label = {i: names.get(i, f"#{i}") for i in all_ids}
    if symbols:
        want = {s.strip() for s in symbols if s.strip()}
        return [(i, label[i]) for i in ids if label[i] in want or str(i) in want]
    want_c = {c for c in (cats or []) if c in CATEGORIES}
    if not want_c:
        return [(i, label[i]) for i in ids]
    return [(i, label[i]) for i in ids if kinds.get(i, "other") in want_c]


def _warmup_start(start: int | None, p: DiscountParams) -> int | None:
    """Date to start LOADING from so the per-fund baseline already has history on
    ``start`` (≈ 2 calendar days per trading day + a week of slack)."""
    need = max(p.baseline_days, (p.mr_window_days * (2 if p.mr_center == "self" else 1))
               if p.mr_center != "off" else 0)
    if not start or need <= 0:
        return start
    o = _ord(start) - (need * 2 + 7)
    try:
        d = _dt.date.fromordinal(max(o, 1))
    except ValueError:
        return start
    return d.year * 10000 + d.month * 100 + d.day


def _load_ex(db, sid: int, start: int | None, end: int | None, p: DiscountParams) -> dict:
    """Like _load but also returns the warm-up rows and the offset of the first kept row."""
    raw_all = db.get_nav_intraday(sid, _warmup_start(start, p), end)
    rows_all, day_vol = _prep(raw_all, p)
    if start:
        off = next((i for i, r in enumerate(rows_all) if r[1] >= start), len(rows_all))
        raw = [r for r in raw_all if r[0] >= start]
    else:
        off, raw = 0, raw_all
    return {"sid": sid, "raw": raw, "rows": rows_all[off:], "day_vol": day_vol,
            "rows_all": rows_all, "off": off}


def _load(db, sid: int, start: int | None, end: int | None, p: DiscountParams):
    """(raw_in_range, rows, day_vol): rows are built from the warm-up-extended raw data
    (so the baseline is ready on day one) but only rows with date >= start are kept."""
    d = _load_ex(db, sid, start, end, p)
    return d["raw"], d["rows"], d["day_vol"]


# --------------------------------------------------------------------------- #
#  Mean-reversion eligibility score                                            #
# --------------------------------------------------------------------------- #

def _mr_scores(rows_all: list[tuple], dev: list, p: DiscountParams, centered: bool) -> list:
    """Score (0-100) for every row = score of that row's DAY, computed only from the
    previous ``mr_window_days`` trading days of this fund (causal).

    dev[i] is the deviation of the bubble from its centre at row i (None = unusable).
    AR(1) coefficient phi of consecutive-by-``mr_lag`` snapshots of the same day; the
    score is the share of a deviation expected to close within ``mr_horizon_days``:
        100·(1 − phi_day^horizon),  phi_day = phi^(snapshots_per_day / lag)
    (phi <= 0 → 100, phi >= 1 → 0).  ``centered`` ("self") measures the deviation from the
    fund's mean bubble over the N days before the scoring window."""
    L = max(1, int(p.mr_lag))
    by_day: dict[int, list[float]] = {}
    for i, r in enumerate(rows_all):
        if r[5] and dev[i] is not None:
            by_day.setdefault(r[1], []).append(dev[i])
    days = sorted(by_day)
    st = []
    for d in days:
        v = by_day[d]
        n = 0
        sa = sb = saa = sbb = sab = 0.0
        for k in range(len(v) - L):
            a, b = v[k], v[k + L]
            n += 1
            sa += a
            sb += b
            saa += a * a
            sbb += b * b
            sab += a * b
        st.append((n, sa, sb, saa, sbb, sab, len(v), sum(v)))
    N = max(3, int(p.mr_window_days))
    H = max(1, int(p.mr_horizon_days))
    score_of: dict[int, float] = {}
    for j, d in enumerate(days):
        lo = max(0, j - N)
        win = st[lo:j]                                   # previous days only
        if len(win) < max(3, N // 3):
            continue
        n = sum(w[0] for w in win)
        if n < 8:
            continue
        sa = sum(w[1] for w in win)
        sb = sum(w[2] for w in win)
        saa = sum(w[3] for w in win)
        sbb = sum(w[4] for w in win)
        sab = sum(w[5] for w in win)
        m = 0.0
        if centered:
            # the centre is the fund's mean bubble over the N days BEFORE the scoring window.
            # (Centering on the scoring window itself makes any short random walk look
            #  mean-reverting — measured score 57-65 for pure random walks.)
            older = st[max(0, lo - N):lo]
            if len(older) < max(3, N // 3):
                continue
            cnt = sum(w[6] for w in older)
            if cnt < 8:
                continue
            m = sum(w[7] for w in older) / cnt
        num = sab - m * (sa + sb) + n * m * m
        den = 0.5 * ((saa - 2 * m * sa + n * m * m) + (sbb - 2 * m * sb + n * m * m))
        if den <= 1e-18:
            continue
        phi = num / den
        spd = sum(w[6] for w in win) / len(win)
        if phi <= 0:
            score = 100.0
        elif phi >= 1:
            score = 0.0
        else:
            score = 100.0 * (1.0 - math.exp((spd * H / L) * math.log(phi)))
        score_of[d] = max(0.0, min(100.0, score))
    return [score_of.get(r[1]) for r in rows_all]


def _timeline(rows_by_fund: list[list[tuple]], min_share: float):
    """Simple-average RAW bubble (price/NAV−1) of the funds at every instant, causal
    (same-day quotes up to that instant, carried forward).  Returns (keys, values)."""
    n = len(rows_by_fund)
    need = max(1, min(n, int(-(-n * min_share // 1)))) if n else 1
    events = []
    for k, rows in enumerate(rows_by_fund):
        for r in rows:
            if r[5]:
                events.append((r[1], r[2], k, r[4] / r[6] - 1.0))
    events.sort()
    keys, vals = [], []
    cur: dict[int, float] = {}
    total = 0.0
    day = None
    j = 0
    while j < len(events):
        d, t = events[j][0], events[j][1]
        if d != day:
            day, cur, total = d, {}, 0.0
        while j < len(events) and events[j][0] == d and events[j][1] == t:
            _d, _t, k, b = events[j]
            total += b - cur.get(k, 0.0)
            cur[k] = b
            j += 1
        keys.append((d, t))
        vals.append(total / len(cur) if len(cur) >= need else None)
    return keys, vals


def _cat_at(keys: list, vals: list, d: int, t: int):
    i = bisect.bisect_right(keys, (d, t)) - 1
    if i < 0 or keys[i][0] != d:
        return None
    return vals[i]


def _category_of(db) -> dict[int, str]:
    kinds = db.get_nav_symbol_category()
    return {i: kinds.get(i, "other") for i in db.get_nav_intraday_ids()}


def _compute_mr(db, loaded: list[dict], p: DiscountParams, start, end) -> None:
    """Fill ``l["mr"]`` (scores aligned to ``l["rows"]``) for every loaded fund."""
    if p.mr_center == "off":
        return
    cat_tl: dict[str, tuple] = {}
    if p.mr_center == "category":
        cats_of = _category_of(db)
        for c in {cats_of.get(l["sid"], "other") for l in loaded}:
            rows_by = []
            for sid, _lab in _universe(db, [c], None):
                raw_all = db.get_nav_intraday(sid, _warmup_start(start, p), end)
                rr, _ = _prep(raw_all, replace(p, baseline_days=0))
                if rr:
                    rows_by.append(rr)
            cat_tl[c] = _timeline(rows_by, 0.3)
        for l in loaded:
            l["cat"] = cats_of.get(l["sid"], "other")
    for l in loaded:
        rows_all = l["rows_all"]
        if p.mr_center == "category":
            keys, vals = cat_tl.get(l["cat"], ([], []))
            dev = []
            for r in rows_all:
                cv = _cat_at(keys, vals, r[1], r[2])
                dev.append(None if cv is None else r[4] / r[6] - 1.0 - cv)
        else:
            dev = [r[4] / r[6] - 1.0 for r in rows_all]
        sc = _mr_scores(rows_all, dev, p, centered=(p.mr_center == "self"))
        l["mr"] = sc[l["off"]:]


def _mr_summary(label: str, rows: list[tuple], mr: list, p: DiscountParams) -> dict:
    by_day: dict[int, float] = {}
    for r, sc in zip(rows, mr):
        if sc is not None:
            by_day[r[1]] = sc
    days = sorted(by_day)
    if not days:
        return {"symbol": label, "scored_days": 0, "eligible_days_pct": 0.0, "avg_score": None,
                "last_score": None, "eligible_now": False}
    elig = sum(1 for d in days if by_day[d] >= p.mr_min_score)
    return {"symbol": label, "scored_days": len(days),
            "eligible_days_pct": round(elig / len(days) * 100, 1),
            "avg_score": round(sum(by_day.values()) / len(days), 1),
            "last_score": round(by_day[days[-1]], 1), "last_date": days[-1],
            "eligible_now": by_day[days[-1]] >= p.mr_min_score}


def _day_windows(raw: list[tuple]) -> dict[int, tuple[int, int]]:
    """{date: (t_first, t_last)} — first / last snapshot of the day at which the
    cumulative volume GREW.  That is the real continuous-trading window of this fund
    on this day: before it the quote is a pre-open indication (volume still 0, orders
    cannot execute), after it the quote is stale.  Gold funds, for example, trade
    11:00-15:00 on some days and 12:00-18:00 on others, so a fixed clock window is wrong."""
    win: dict[int, list[int]] = {}
    day, prev = None, 0
    for r in raw:
        d, t, vol = r[0], r[1], (r[5] or 0)
        if d != day:
            day, prev = d, 0
        if vol > prev:
            w = win.get(d)
            if w is None:
                win[d] = [t, t]
            else:
                w[1] = t
        prev = max(prev, vol)
    return {d: (w[0], w[1]) for d, w in win.items()}


def _in_session(p: DiscountParams, win: tuple[int, int] | None, t: int) -> bool:
    if p.session_mode == "fixed":
        return p.session_start <= t <= p.session_end
    return win is not None and win[0] <= t <= win[1]


def _prep(raw: list[tuple], p: DiscountParams):
    """Raw DB rows -> (rows, day_vol).

    rows: [(ordinal, date, time, fair, last, fresh, nav_raw)] — only in-session
    snapshots with a positive price/NAV and a NAV no older than ``max_nav_age_days``.
    ``fair`` = NAV·(1+baseline) where baseline is the fund's median discount over the
    previous ``baseline_days`` trading days (past data only; snapshots during the
    warm-up, before enough history exists, are dropped).  baseline_days = 0 → fair = NAV.
    day_vol: {date: total volume traded that day}.
    """
    base_rows = []
    day_vol: dict[int, int] = {}
    prev_vol = 0
    prev_date = 0
    windows = _day_windows(raw) if p.session_mode != "fixed" else {}
    for d, t, nav, nav_d, last, vol in raw:
        if d != prev_date:
            prev_date, prev_vol = d, 0
        vol = vol or 0
        if vol > day_vol.get(d, 0):
            day_vol[d] = vol
        fresh = vol > prev_vol
        prev_vol = max(prev_vol, vol)
        if not _in_session(p, windows.get(d), t):
            continue
        if not last or last <= 0 or not nav or nav <= 0:
            continue
        o = _ord(d)
        if nav_d and p.max_nav_age_days >= 0 and o - _ord(nav_d) > p.max_nav_age_days:
            continue
        base_rows.append((o, d, t, nav, last, fresh))

    if p.baseline_days <= 0:
        return [(o, d, t, nav, last, fr, nav) for o, d, t, nav, last, fr in base_rows], day_vol

    # per-day typical discount (median of the day's fresh snapshots), then a causal
    # rolling median over the previous baseline_days trading days
    per_day: dict[int, list[float]] = {}
    for o, d, t, nav, last, fr in base_rows:
        if fr:
            per_day.setdefault(d, []).append(last / nav - 1.0)
    days = sorted(per_day)
    daily = [statistics.median(per_day[d]) for d in days]
    min_hist = max(3, p.baseline_days // 3)
    base_of: dict[int, float] = {}
    for i, d in enumerate(days):
        window = daily[max(0, i - p.baseline_days):i]
        if len(window) >= min_hist:
            base_of[d] = statistics.median(window)
    rows = []
    for o, d, t, nav, last, fr in base_rows:
        b = base_of.get(d)
        if b is None:
            continue
        rows.append((o, d, t, nav * (1.0 + b), last, fr, nav))
    return rows, day_vol


# --------------------------------------------------------------------------- #
#  Bubble index (simple average of the funds' bubbles)                         #
# --------------------------------------------------------------------------- #

def _bubble_index(rows_by_fund: list[list[tuple]], p: DiscountParams):
    """Index value at every snapshot of every fund.

    Returns (idx, curve, share_below): idx[k][i] is B at fund k's row i (None while
    fewer than ``index_min_share`` of the funds have quoted that day); curve is
    [[date, mean B in %]] per day; share_below = share of snapshots with
    B <= −index_entry_pct.
    """
    n_funds = sum(1 for r in rows_by_fund if r)
    need = max(1, min(n_funds, int(-(-n_funds * p.index_min_share // 1)))) if n_funds else 1
    events = []
    for k, rows in enumerate(rows_by_fund):
        for i, (o, d, t, nav, last, fresh, _raw) in enumerate(rows):
            events.append((d, t, k, i, last / nav - 1.0, fresh))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    idx = [[None] * len(r) for r in rows_by_fund]
    cur: dict[int, float] = {}
    total = 0.0
    day = None
    per_day: dict[int, list[float]] = {}
    n_all = n_below = 0
    thr = -p.index_entry_pct / 100.0
    j = 0
    while j < len(events):
        d, t = events[j][0], events[j][1]
        if d != day:                                   # new day: forget yesterday's quotes
            day, cur, total = d, {}, 0.0
        group = []
        while j < len(events) and events[j][0] == d and events[j][1] == t:
            group.append(events[j]); j += 1
        for _d, _t, k, i, rel, fresh in group:         # all quotes up to and incl. this instant
            if fresh:
                total += rel - cur.get(k, 0.0)
                cur[k] = rel
        val = total / len(cur) if len(cur) >= need else None
        for _d, _t, k, i, rel, fresh in group:
            idx[k][i] = val
        if val is not None:
            per_day.setdefault(d, []).append(val)
            n_all += 1
            n_below += 1 if val <= thr else 0
    curve = [[d, round(sum(v) / len(v) * 100, 4)] for d, v in sorted(per_day.items())]
    return idx, curve, (n_below / n_all if n_all else 0.0)


# --------------------------------------------------------------------------- #
#  Simulation (one fund)                                                       #
# --------------------------------------------------------------------------- #

def _simulate(label: str, rows: list[tuple], day_vol: dict, p: DiscountParams,
              idx: list | None = None, rel: list | None = None,
              mr: list | None = None) -> list[Trade]:
    """``rel`` (optional) replaces last/fair − 1 as the fund-level signal — used by the
    validation placebo test to feed the same rules a signal that is unrelated to prices."""
    trades: list[Trade] = []
    if not rows:
        return trades
    hs = p.half_spread_pct / 100.0
    ent_thr = -p.entry_discount_pct / 100.0
    ex_thr = -p.exit_discount_pct / 100.0
    stop_mult = 1.0 - p.stop_loss_pct / 100.0
    big = 1 << 60

    pos = 0
    cost = 0.0           # cost basis (excl. fees) of the units held
    e_ord = e_date = e_time = 0
    e_nav = 0.0          # raw NAV at entry
    e_fair = 0.0         # fair value (NAV·(1+baseline)) at entry
    e_rel = 0.0          # last/fair − 1 at the entry signal
    e_px = 0.0
    blocked_date = 0
    stop_s = p.stop_loss_pct / 100.0
    mode = p.entry_mode if p.entry_mode in ("fund", "index", "both") else "fund"
    if mode != "fund" and idx is None:
        mode = "fund"                      # no index available (caller did not build it)
    ix_entry = -p.index_entry_pct / 100.0
    ix_exit = -p.index_exit_pct / 100.0
    e_idx = None
    mr_on = p.mr_center != "off" and mr is not None
    e_mr = None

    def _cap(date_int: int) -> int:
        if p.participation_pct <= 0:
            return big
        return int(day_vol.get(date_int, 0) * p.participation_pct / 100.0)

    def _close(units: int, px: float, d: int, t: int, nav: float, fair: float, reason: str,
               x_idx: float | None = None):
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
            rel_entry_pct=round((cost_part / units / e_fair - 1) * 100, 4) if e_fair else 0,
            rel_exit_pct=round((px / fair - 1) * 100, 4) if fair else 0,
            base_pct=round((e_fair / e_nav - 1) * 100, 4) if e_nav else 0,
            mr_score=round(e_mr, 1) if e_mr is not None else None,
            idx_entry_pct=round(e_idx * 100, 4) if e_idx is not None else None,
            idx_exit_pct=round(x_idx * 100, 4) if x_idx is not None else None,
            buy_notional=round(cost_part, 0), sell_notional=round(sell_notional, 0),
            fees=round(buy_fee + sell_fee, 0), net_pnl=round(net, 0),
            net_pct=round(net / invested * 100, 4) if invested else 0,
            hold_days=max(0, _ord(d) - e_ord), exit_reason=reason))
        pos -= units
        cost -= cost_part

    last_i = len(rows) - 1
    for i, (o, d, t, nav, last, fresh, nav_raw) in enumerate(rows):
        if p.require_fresh and not fresh:
            continue
        r = rel[i] if rel is not None else last / nav - 1.0       # fund-level signal
        if pos == 0:
            if i == last_i:
                continue                  # never open a position on the final snapshot
            if d == blocked_date:
                continue
            if mr_on and (mr[i] is None or mr[i] < p.mr_min_score):
                continue                      # not mean-reverting enough lately: not tradable today
            ix = idx[i] if idx is not None else None
            fund_ok = r <= ent_thr
            idx_ok = ix is not None and ix <= ix_entry
            if not ((mode == "fund" and fund_ok) or (mode == "index" and idx_ok)
                    or (mode == "both" and fund_ok and idx_ok)):
                continue
            ask = last * (1 + hs)
            units = min(int(_SIM_CAPITAL // ask), _cap(d))
            if units <= 0:
                continue
            pos, cost = units, units * ask
            e_ord, e_date, e_time, e_nav, e_px = o, d, t, nav_raw, ask
            e_fair, e_rel = nav, r
            e_idx = ix
            e_mr = mr[i] if mr_on else None
        else:
            bid = last * (1 - hs)
            reason = None
            if p.max_hold_days > 0 and o - e_ord >= p.max_hold_days:
                reason = "time"
            elif stop_s > 0 and (
                    (p.stop_mode == "price" and bid <= e_px * stop_mult)
                    or (p.stop_mode == "nav_level" and r <= -stop_s)
                    or (p.stop_mode not in ("price", "nav_level")
                        and r <= e_rel - stop_s)):
                reason = "stop"
            elif ((mode == "index" and idx is not None and idx[i] is not None and idx[i] >= ix_exit)
                  or (mode != "index" and r >= ex_thr)):
                reason = "signal"
            if reason:
                units = min(pos, _cap(d))
                if units > 0:
                    _close(units, bid, d, t, nav_raw, nav, reason,
                           idx[i] if idx is not None else None)
                    if pos == 0:
                        blocked_date = d

    if pos > 0:                       # data ended while holding
        o, d, t, nav, last, _, nav_raw = rows[-1]
        _close(pos, last * (1 - hs), d, t, nav_raw, nav, "end",
               idx[-1] if idx is not None else None)
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


def _sec(t: int) -> int:
    t = int(t)
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _replay(trades: list[Trade], p: DiscountParams, d0: int, d1: int, want_exposure: bool = False):
    """Replay maximum-liquidity trades against ONE capital pool, in time order.

    Returns (accepted_trades, portfolio_dict, equity_curve, skipped_positions).
    A position (all its exit chunks) is scaled by f = min(1, affordable/intended):
    intended = position_pct % of book capital (cash + open cost); affordable = cash.
    P&L is linear in size, so scaling a trade is exact (no market impact modelled).
    """
    bf, sf = p.buy_fee, p.sell_fee
    groups: dict[tuple, list[Trade]] = {}
    for t in trades:
        groups.setdefault((t.symbol, t.entry_date, t.entry_time), []).append(t)
    events = []
    for key, chunks in groups.items():
        events.append((key[1], key[2], 1, groups[key][0].rel_entry_pct, key[0], key, -1))
        for i, t in enumerate(chunks):
            same = (t.exit_date, t.exit_time) == (key[1], key[2])
            events.append((t.exit_date, t.exit_time, 2 if same else 0, 0.0, key[0], key, i))
    # at a tie: other exits, then entries (deepest discount vs own norm first), then own-instant exits
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3], e[4]))

    cash = float(p.initial_capital)
    invested = 0.0                         # book cost of open positions
    factor: dict[tuple, float] = {}
    accepted: list[Trade] = []
    skipped = 0
    curve: dict[int, float] = {d0: cash}
    peak = cash
    max_dd = 0.0
    # Exposure = invested book cost / book capital, integrated over REAL clock time (seconds), so a
    # position held for two hours counts for two hours and a same-day round trip is not ignored.
    exp_area = 0.0                         # ∑ (invested/equity) · seconds
    inv_area = 0.0                         # ∑ invested rial · seconds (for the independent cross-check)
    peak_exp = 0.0
    first_ord = _ord(d0)
    start_ts = first_ord * 86400
    end_ts = (_ord(d1) + 1) * 86400
    last_ts = start_ts
    exp_curve = [[d0, 0, 0.0]] if want_exposure else None
    buckets = [0.0, 0.0, 0.0, 0.0]         # seconds flat / <25% / 25-50% / >=50% engaged
    for d, t, kind, _tie, _sym, key, i in events:
        ts = _ord(d) * 86400 + _sec(t)
        eq = cash + invested
        if ts > last_ts:
            frac = (invested / eq) if eq > 0 else 0.0
            exp_area += frac * (ts - last_ts)
            inv_area += invested * (ts - last_ts)
            if want_exposure:
                buckets[0 if frac < 1e-9 else 1 if frac < 0.25 else 2 if frac < 0.5 else 3] += ts - last_ts
            last_ts = ts
        o = _ord(d)
        if kind == 1:                                            # entry (kind 0/2 = exit chunk)
            chunks = groups[key]
            full_cost = sum(c.buy_notional for c in chunks)
            if full_cost <= 0:
                continue
            intended = p.position_pct / 100.0 * eq * (1 + bf)    # cash needed incl. fee
            amount = min(intended, cash)
            if intended <= 0 or amount < 0.25 * intended:
                skipped += 1
                continue
            f = min(1.0, amount / (full_cost * (1 + bf)))
            if f * chunks[0].volume < 1 and len(chunks) == 1:
                skipped += 1
                continue
            factor[key] = f
            cash -= f * full_cost * (1 + bf)
            invested += f * full_cost
            if eq > 0:
                peak_exp = max(peak_exp, invested / (cash + invested))
        else:                                                    # exit chunk
            f = factor.get(key)
            if f is None:
                continue
            c = groups[key][i]
            buy_n, sell_n = f * c.buy_notional, f * c.sell_notional
            cash += sell_n * (1 - sf)
            invested -= buy_n
            net = sell_n * (1 - sf) - buy_n * (1 + bf)
            accepted.append(replace(
                c, volume=int(c.volume * f), buy_notional=round(buy_n, 0),
                sell_notional=round(sell_n, 0), fees=round(buy_n * bf + sell_n * sf, 0),
                net_pnl=round(net, 0), scale=round(f, 4)))
            eq2 = cash + invested
            curve[d] = eq2
            peak = max(peak, eq2)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq2) / peak)
        if want_exposure:
            e_now = cash + invested
            exp_curve.append([d, t, round(invested / e_now * 100, 3) if e_now > 0 else 0.0])

    final = cash + invested
    if end_ts > last_ts:                                          # tail up to the end of the last day
        eq = cash + invested
        frac = (invested / eq) if eq > 0 else 0.0
        exp_area += frac * (end_ts - last_ts)
        inv_area += invested * (end_ts - last_ts)
        if want_exposure:
            buckets[0 if frac < 1e-9 else 1 if frac < 0.25 else 2 if frac < 0.5 else 3] += end_ts - last_ts
            exp_curve.append([d1, 235959, round(frac * 100, 3)])
    total_s = max(1, end_ts - start_ts)
    span_days = max(1, _ord(d1) - first_ord)
    years = span_days / 365.0
    cagr = None
    if final > 0 and p.initial_capital > 0 and years >= 30 / 365.0:
        cagr = ((final / p.initial_capital) ** (1 / years) - 1) * 100
    pf = {
        "initial_capital": round(p.initial_capital, 0),
        "final_capital": round(final, 0),
        "net_profit": round(final - p.initial_capital, 0),
        "portfolio_return_pct": round((final / p.initial_capital - 1) * 100, 3) if p.initial_capital else 0,
        "cagr_pct": round(cagr, 2) if cagr is not None else None,
        "max_equity_drawdown_pct": round(max_dd * 100, 3),
        "avg_exposure_pct": round(exp_area / total_s * 100, 1),
        "peak_exposure_pct": round(peak_exp * 100, 1),
        "skipped_positions": skipped,
        "accepted_positions": len(factor),
        "period_days": span_days,
    }
    if want_exposure:
        pf["exposure"] = {"curve": exp_curve, "flat_time_pct": round(buckets[0] / total_s * 100, 1),
                          "low_time_pct": round(buckets[1] / total_s * 100, 1),
                          "mid_time_pct": round(buckets[2] / total_s * 100, 1),
                          "high_time_pct": round(buckets[3] / total_s * 100, 1),
                          "exposure_seconds_area": exp_area, "invested_rial_seconds": inv_area, "total_seconds": total_s}
    ds = sorted(curve)
    curve_list = [[d, round(curve[d], 0)] for d in ds]
    return accepted, pf, curve_list, skipped


def _portfolio_summary(trades: list[Trade], p: DiscountParams, d0: int, d1: int, want_exposure: bool = False):
    accepted, pf, curve, skipped = _replay(trades, p, d0, d1, want_exposure)
    s = _summarize(accepted, p.initial_capital)
    s.update(pf)
    s["max_drawdown_pct"] = pf["max_equity_drawdown_pct"]
    return accepted, s, curve


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
#  Passive benchmark: equal-weight index of the selected funds                 #
# --------------------------------------------------------------------------- #

TRADING_DAYS = 240          # Tehran market: ~240 trading days a year


def _daily_series(raw: list[tuple], p: DiscountParams) -> tuple[dict, dict]:
    """Raw snapshots -> ({date: end-of-day traded price}, {date: end-of-day NAV})."""
    price: dict[int, float] = {}
    nav: dict[int, float] = {}
    windows = _day_windows(raw) if p.session_mode != "fixed" else {}
    for d, t, n, nav_d, last, vol in raw:             # rows arrive in time order
        if n and n > 0:
            nav[d] = n
        if last and last > 0 and (vol or 0) > 0 and _in_session(p, windows.get(d), t):
            price[d] = last
    return price, nav


def _returns_by_date(series: list[dict]) -> tuple[int | None, dict[int, dict[int, float]]]:
    """{date: {fund_index: return}} from per-fund price series.

    A return is vs the fund's own previous observation; returns beyond ±30% or
    across >10-day gaps are treated as data errors and dropped.
    """
    by_date: dict[int, dict[int, float]] = {}
    first = None
    for k, ser in enumerate(series):
        ds = sorted(ser)
        if not ds:
            continue
        first = ds[0] if first is None else min(first, ds[0])
        for a, b in zip(ds, ds[1:]):
            r = ser[b] / ser[a] - 1.0
            if abs(r) <= 0.30 and (_ord(b) - _ord(a)) <= 10:
                by_date.setdefault(b, {})[k] = r
    return first, by_date


def _equal_weight_index(series: list[dict]) -> tuple[list[tuple[int, float]], list[int]]:
    """Theoretical daily-rebalanced equal-weight index (level 1.0 at the first
    observation, NO trading costs): each day's return is the mean return of every
    fund observed that day.  Returns ([(date, level)], [n_funds per day])."""
    first, by_date = _returns_by_date(series)
    if first is None:
        return [], []
    level = 1.0
    out = [(first, 1.0)]
    counts = []
    for d in sorted(by_date):
        rs = list(by_date[d].values())
        level *= 1.0 + sum(rs) / len(rs)
        out.append((d, level))
        counts.append(len(rs))
    return out, counts


def _rebalanced_with_fees(series: list[dict], initial: float, bf: float, sf: float) -> dict:
    """Passive account that REALLY rebalances to equal weights every day and pays
    the buy fee on every purchase and the sell fee on every sale.

    Day-1: ``initial`` is invested (buy fee).  Each day the funds observed that day
    grow by their return, then are trimmed/topped-up to equal weight (sell fee on
    what is sold, buy fee on what is bought).  Funds with no observation that day
    are left untouched.  On the last date everything is sold (sell fee).
    """
    first, by_date = _returns_by_date(series)
    if first is None or not by_date:
        return {}
    hold: dict[int, float] = {}
    fees = 0.0
    turnover: list[float] = []                 # traded value / portfolio value, per day
    curve: list[tuple[int, float]] = [(first, float(initial))]
    # day 1: spend the initial cash equally on the funds that already trade
    start_pool = [k for k, ser in enumerate(series) if first in ser]
    if not start_pool:
        return {}
    spend = initial / (1.0 + bf)
    for k in start_pool:
        hold[k] = spend / len(start_pool)
    fees += spend * bf
    for d in sorted(by_date):
        rets = by_date[d]
        for k, r in rets.items():              # grow what we hold
            if k in hold:
                hold[k] *= 1.0 + r
        pool = list(rets)                      # funds we equalise today
        base = sum(hold.get(k, 0.0) for k in pool)
        n = len(pool)
        if n == 0 or base <= 0:
            continue
        tgt = base / n
        buys = sum(max(tgt - hold.get(k, 0.0), 0.0) for k in pool)
        sells = sum(max(hold.get(k, 0.0) - tgt, 0.0) for k in pool)
        fee = buys * bf + sells * sf
        tgt = (base - fee) / n                 # fees are paid out of the pool
        for k in pool:
            hold[k] = tgt
        fees += fee
        total = sum(hold.values())
        if total > 0:
            turnover.append((buys + sells) / total)
        curve.append((d, total))
    last_d = curve[-1][0]
    total = sum(hold.values())
    exit_fee = total * sf                      # sell everything at the end
    fees += exit_fee
    curve[-1] = (last_d, total - exit_fee)
    st = _level_stats(curve)
    st.update({
        "final_capital": round(total - exit_fee, 0),
        "fees_total": round(fees, 0),
        "avg_daily_turnover_pct": round(sum(turnover) / len(turnover) * 100, 2) if turnover else 0,
    })
    return {"stats": st, "curve": curve}


def _level_stats(levels: list[tuple[int, float]], daily: bool = True) -> dict:
    if len(levels) < 2:
        return {}
    d0, d1 = levels[0][0], levels[-1][0]
    total = levels[-1][1] / levels[0][1] - 1.0
    years = max(1, _ord(d1) - _ord(d0)) / 365.0
    cagr = ((1 + total) ** (1 / years) - 1) * 100 if years >= 30 / 365.0 and total > -1 else None
    rets = [b[1] / a[1] - 1.0 for a, b in zip(levels, levels[1:]) if a[1] > 0]
    vol = statistics.pstdev(rets) * (TRADING_DAYS ** 0.5) * 100 if len(rets) > 2 else 0.0
    peak, dd = levels[0][1], 0.0
    for _, v in levels:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0.0)
    return {"total_return_pct": round(total * 100, 3),
            "cagr_pct": round(cagr, 2) if cagr is not None else None,
            "vol_pct": round(vol, 2), "max_dd_pct": round(dd * 100, 3),
            "up_days_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1) if rets else 0}


def _buy_and_hold(series: list[dict], initial: float, bf: float, sf: float) -> dict:
    """Equal-weight buy-and-hold of the funds that already trade on the first date.

    ``initial`` capital buys equally (incl. buy fee) on day 1; a fund whose data
    ends is held at its last price; everything is sold (sell fee) at the end.
    """
    series = [s for s in series if s]
    if not series:
        return {}
    d_first = min(min(s) for s in series)
    start = [s for s in series if d_first in s]
    if not start:
        return {}
    all_dates = sorted({d for s in series for d in s})
    per = initial / (1 + bf) / len(start)
    units = [per / s[d_first] for s in start]
    ptr = [d_first] * len(start)                       # last known price per fund
    last_px = [s[d_first] for s in start]
    curve = []
    for d in all_dates:
        tot = 0.0
        for k, s in enumerate(start):
            if d in s:
                last_px[k] = s[d]
            tot += units[k] * last_px[k]
        curve.append((d, tot))
    final = curve[-1][1] * (1 - sf)
    d1 = curve[-1][0]
    years = max(1, _ord(d1) - _ord(d_first)) / 365.0
    ret = final / initial - 1.0
    cagr = ((1 + ret) ** (1 / years) - 1) * 100 if years >= 30 / 365.0 and ret > -1 else None
    peak, dd = curve[0][1], 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0.0)
    return {"funds": len(start), "final_capital": round(final, 0),
            "return_pct": round(ret * 100, 3),
            "cagr_pct": round(cagr, 2) if cagr is not None else None,
            "max_dd_pct": round(dd * 100, 3)}


def build_benchmark(price_series: list[dict], nav_series: list[dict],
                    p: DiscountParams) -> dict | None:
    """Passive alternatives over the same funds and period (see module docstring)."""
    levels, counts = _equal_weight_index(price_series)
    if len(levels) < 2:
        return None
    init = p.initial_capital
    st = _level_stats(levels)                                  # theoretical, no costs
    st["final_capital"] = round(init * levels[-1][1], 0)
    st["avg_funds"] = round(sum(counts) / len(counts), 1) if counts else 0
    nav_levels, _ = _equal_weight_index(nav_series)
    nav_st = _level_stats(nav_levels)
    reb = _rebalanced_with_fees(price_series, init, p.buy_fee, p.sell_fee)
    if reb.get("stats") is not None:
        reb["stats"]["avg_funds"] = st["avg_funds"]
    curve_src = reb.get("curve") or [(d, init * lv) for d, lv in levels]
    curve = [[d, round(v, 0)] for d, v in curve_src]
    if len(curve) > 400:
        step = len(curve) / 400.0
        curve = [curve[int(i * step)] for i in range(400)] + [curve[-1]]
    return {
        "first": levels[0][0], "last": levels[-1][0],
        "funds": sum(1 for s in price_series if len(s) >= 2),
        "rebalanced": reb.get("stats", {}),                    # daily rebalance WITH fees
        "buyhold": _buy_and_hold(price_series, init, p.buy_fee, p.sell_fee),
        "nav_index": nav_st,
        "curve": curve,                                        # = the fee-paying account
    }


# --------------------------------------------------------------------------- #
#  Public: single backtest                                                     #
# --------------------------------------------------------------------------- #

def run_discount_backtest(db, cats: list[str] | None = None, symbols: list[str] | None = None,
                          start: int | None = None, end: int | None = None,
                          params: DiscountParams | None = None) -> dict:
    p = params or DiscountParams()
    funds = _universe(db, cats, symbols)
    use_index = p.entry_mode in ("index", "both")
    raw_trades: list[Trade] = []
    tested = skipped_funds = 0
    price_series: list[dict] = []
    nav_series: list[dict] = []
    loaded: list[dict] = []
    for sid, label in funds:
        d = _load_ex(db, sid, start, end, p)
        ps, ns = _daily_series(d["raw"], p)
        price_series.append(ps)
        nav_series.append(ns)
        if not d["rows"]:
            skipped_funds += 1
            continue
        d["label"] = label
        loaded.append(d)
    tested = len(loaded)
    _compute_mr(db, loaded, p, start, end)
    mr_info = None
    if p.mr_center != "off":
        mr_info = sorted((_mr_summary(l["label"], l["rows"], l["mr"], p) for l in loaded),
                         key=lambda x: -(x["last_score"] if x["last_score"] is not None else -1))
    bubble = None
    if use_index and loaded:
        idx, curve_b, share = _bubble_index([l["rows"] for l in loaded], p)
        for l, ix in zip(loaded, idx):
            raw_trades.extend(_simulate(l["label"], l["rows"], l["day_vol"], p, ix, None, l.get("mr")))
        bubble = {"curve": curve_b, "share_below_entry_pct": round(share * 100, 1),
                  "entry_pct": p.index_entry_pct, "exit_pct": p.index_exit_pct}
    else:
        for l in loaded:
            raw_trades.extend(_simulate(l["label"], l["rows"], l["day_vol"], p, None, None, l.get("mr")))
    dates = db.get_nav_intraday_dates(start, end)
    d0 = dates[0] if dates else (start or 0)
    d1 = dates[-1] if dates else (end or 0)
    accepted, summary, curve = _portfolio_summary(raw_trades, p, d0, d1, want_exposure=True)
    exposure = summary.pop("exposure", None)
    if exposure is not None:
        ec = exposure["curve"]
        if len(ec) > 3000:
            step = len(ec) / 3000.0
            ec = [ec[int(i * step)] for i in range(3000)] + [ec[-1]]
        exposure["curve"] = ec
        exposure["funds"] = tested
        exposure["position_pct"] = p.position_pct
        exposure["ceiling_pct"] = round(min(100.0, tested * p.position_pct), 1)
        exposure["avg_pct"] = summary.get("avg_exposure_pct")
        exposure["peak_pct"] = summary.get("peak_exposure_pct")
    accepted.sort(key=lambda t: (t.entry_date, t.entry_time))
    if len(curve) > 400:                       # keep the payload small
        step = len(curve) / 400.0
        curve = [curve[int(i * step)] for i in range(400)] + [curve[-1]]
    trade_dicts = [asdict(t) for t in accepted]
    from discount_explain import explain_trades
    loss_causes = explain_trades(trade_dicts, loaded, p)        # adds "why" to every losing trade
    return {
        "cats": cats or [],
        "funds_tested": tested, "funds_skipped": skipped_funds,
        "params": asdict(p),
        "period": [d0, d1],
        "signals": len({(t.symbol, t.entry_date, t.entry_time) for t in raw_trades}),
        "trades": trade_dicts,
        "loss_causes": loss_causes,
        "exposure": exposure,
        "per_symbol": _per_symbol(accepted, p.initial_capital),
        "equity_curve": curve,
        "benchmark": build_benchmark(price_series, nav_series, p),
        "bubble_index": bubble,
        "mr_info": mr_info,
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
#  Public: discount statistics (calibrate thresholds, spot stale NAVs)         #
# --------------------------------------------------------------------------- #

def bubble_series(db, cats: list[str] | None = None, symbols: list[str] | None = None,
                  start: int | None = None, end: int | None = None,
                  show: list[str] | None = None, min_share: float = 0.5,
                  max_show: int = 12, params: DiscountParams | None = None) -> dict:
    """Intraday bubble (price vs NAV, %) for charting.

    ``symbols`` / ``cats`` pick the universe that the simple-average index is built
    from (same selection as the backtest); ``show`` lists funds whose own series is
    returned as well (labels or "#id").  Bubble = last / NAV − 1 against the raw NAV;
    only snapshots with a fresh price and a usable NAV inside the session count.
    The index at an instant is the plain mean of the latest same-day bubble of every
    fund that has quoted so far (needs ``min_share`` of the funds, like the backtest).
    Points are [date, HHMMSS, bubble %].
    """
    base = params or DiscountParams()
    p = replace(base, baseline_days=0, require_fresh=True)
    funds = _universe(db, cats, symbols)
    want = {s.strip() for s in (show or []) if s.strip()}
    rows_by_fund, labels = [], []
    for sid, label in funds:
        raw = db.get_nav_intraday(sid, start, end)
        rows, _vol = _prep(raw, p)
        rows = [r for r in rows if r[5]]
        if rows:
            rows_by_fund.append(rows)
            labels.append((sid, label))
    out_funds = []
    if rows_by_fund:
        idx, curve, _share = _bubble_index(rows_by_fund, DiscountParams(
            baseline_days=0, index_min_share=min_share))
    else:
        idx, curve = [], []
    # the index as a time series (one point per distinct instant)
    pts: dict[tuple, float] = {}
    for rows, ix in zip(rows_by_fund, idx):
        for r, v in zip(rows, ix):
            if v is not None:
                pts[(r[1], r[2])] = v
    index_pts = [[d, t, round(v * 100, 4)] for (d, t), v in sorted(pts.items())]
    for (sid, label), rows in zip(labels, rows_by_fund):
        if label in want or str(sid) in want:
            out_funds.append({"symbol": label, "symbol_id": sid, "points": [
                [r[1], r[2], round((r[4] / r[6] - 1.0) * 100, 4)] for r in rows]})
    return {"funds_in_index": len(rows_by_fund), "index": index_pts,
            "daily_index": curve, "funds": out_funds[:max_show],
            "period": [min((r[0][1] for r in rows_by_fund), default=None),
                       max((r[-1][1] for r in rows_by_fund), default=None)]}


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
        rows, _ = _prep(raw, replace(p, max_nav_age_days=-1, baseline_days=0))
        rows = [r for r in rows if r[5]]                      # fresh only
        if len(rows) < 20:
            continue
        disc = sorted((last / nav - 1) * 100 for _, _, _, nav, last, _, _ in rows)
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

GRID_INDEX = {
    "index_entry_pct":    [0.1, 0.2, 0.3, 0.5, 1.0],
    "index_exit_pct":     [0.2, 0.1, 0.0, -0.1],
    "max_hold_days":      [3, 7, 15, 30],
    "stop_loss_pct":      [0.0, 1.5, 3.0],
}

METRICS = {
    "portfolio_return_pct": "بازده کل پرتفوی ٪ (سرمایهٔ نهایی)",
    "cagr_pct":             "بازده سالانهٔ مرکب ٪",
    "net_profit":           "سود خالص (ریال)",
    "annualized_pct":       "بازده سالانهٔ سرمایهٔ درگیر ٪",
    "profit_factor":        "Profit Factor",
}


def _combos(base: DiscountParams, grid: dict) -> list[DiscountParams]:
    keys = list(grid)
    out = []
    for vals in itertools.product(*(grid[k] for k in keys)):
        c = replace(base, **dict(zip(keys, vals)))
        if c.entry_mode == "index":
            if c.index_exit_pct >= c.index_entry_pct - 0.02:
                continue                  # index exit must be meaningfully above its entry
        elif c.exit_discount_pct >= c.entry_discount_pct - 0.05:
            continue                      # exit must be meaningfully above entry
        out.append(c)
    return out


def optimize_discount(db, cats: list[str] | None = None, symbols: list[str] | None = None,
                      start: int | None = None, end: int | None = None,
                      base: DiscountParams | None = None, opt_metric: str = "portfolio_return_pct",
                      min_trades: int = 30, test_frac: float = 0.3, top_n: int = 15,
                      grid: dict | None = None, progress: dict | None = None,
                      progress_lock: threading.Lock | None = None) -> dict:
    """Grid-search entry/exit/hold/stop on the first (1−test_frac) of the dates,
    then report each top combo's result on the untouched last ``test_frac``."""
    base = base or DiscountParams()
    if opt_metric not in METRICS:
        opt_metric = "portfolio_return_pct"
    use_index = base.entry_mode in ("index", "both")
    default_grid = GRID_INDEX if base.entry_mode == "index" else GRID_DEFAULT
    combos = _combos(base, grid or default_grid)
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

    def _run_fund(label, rows, day_vol, idx, mr=None):
        sel_tr = [i for i, r in enumerate(rows) if r[1] <= cut]
        sel_te = [i for i, r in enumerate(rows) if r[1] > cut]
        pick = lambda lst, sel: ([lst[i] for i in sel] if lst is not None else None)   # noqa: E731
        tr_rows, te_rows = pick(rows, sel_tr), pick(rows, sel_te)
        tr_idx, te_idx = pick(idx, sel_tr), pick(idx, sel_te)
        tr_mr, te_mr = pick(mr, sel_tr), pick(mr, sel_te)
        for ci, c in enumerate(combos):
            train_tr[ci].extend(_simulate(label, tr_rows, day_vol, c, tr_idx, None, tr_mr))
            if te_rows and test_frac > 0:
                test_tr[ci].extend(_simulate(label, te_rows, day_vol, c, te_idx, None, te_mr))

    loaded = []
    for k, (sid, label) in enumerate(funds):
        d = _load_ex(db, sid, start, end, base)
        if d["rows"]:
            d["label"] = label
            loaded.append(d)
        if progress is not None:
            with (progress_lock or threading.Lock()):
                progress["done"] = (k + 1) // 2                      # first half: loading
    _compute_mr(db, loaded, base, start, end)                         # independent of the grid
    idx_all = _bubble_index([l["rows"] for l in loaded], base)[0] if use_index else [None] * len(loaded)
    for k, (l, ix) in enumerate(zip(loaded, idx_all)):
        _run_fund(l["label"], l["rows"], l["day_vol"], ix, l.get("mr"))
        if progress is not None:
            with (progress_lock or threading.Lock()):
                progress["done"] = len(funds) // 2 + (k + 1) * (len(funds) - len(funds) // 2) // max(len(loaded), 1)

    nxt = next((d for d in dates if d > cut), dates[-1])
    scored = []
    for ci, c in enumerate(combos):
        _, s_tr, _ = _portfolio_summary(train_tr[ci], c, dates[0], cut)
        if s_tr["trade_count"] < min_trades:
            continue
        _, s_te, _ = _portfolio_summary(test_tr[ci], c, nxt, dates[-1])
        sc = s_tr.get(opt_metric)
        scored.append({"params": asdict(c), "train": s_tr, "test": s_te,
                       "_score": sc if sc is not None else -1e18})
    scored.sort(key=lambda r: -r["_score"])
    for r in scored:
        r.pop("_score", None)
    return {
        "cats": cats or [], "funds": len(funds), "opt_metric": opt_metric,
        "entry_mode": base.entry_mode,
        "tested_combos": len(combos), "qualified_combos": len(scored),
        "min_trades": min_trades, "test_frac": test_frac,
        "train_range": [dates[0], cut], "test_range": [nxt, dates[-1]],
        "best": scored[0] if scored else None,
        "top": scored[:top_n],
    }
