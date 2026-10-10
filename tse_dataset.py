"""Backtest dataset built from the collected TSE data (every trade + the order-book event stream, see tse_gold.py).

For each fund and day the order book and the trade tape are replayed and sampled on a fixed time grid (``grid_sec``)
over the day's continuous-trading window (first → last trade).  At each grid instant:

  * the top-5 book (bids high→low, asks low→high) as it stood at that instant,
  * mid = (best bid + best ask)/2 (or the last trade price when one side of the book is empty),
  * the cumulative volume traded so far that day (for the "volume so far" cap and the session window),
  * the NAV known at that instant — TSETMC has no intraday NAV history, so it comes from the NAV dump: the dump's
    latest snapshot taken at or before the instant (with its nav_date / nav_time, so the stale-NAV filter applies).

The result has the very shape of ``Database.get_nav_intraday`` rows — (date, time, nav, nav_date, price, vol,
nav_time) with price = mid — so the engine's ``_prep`` (session, freshness, baseline, stale NAV) works unchanged,
plus a ``book`` dict {(date, time): (bids, asks)} the engine uses to fill orders at real prices.

The market part (book / tape on the grid) is cached per (fund, day, grid) in ``tse_grid``; the NAV join is cheap and
done on every load, so changing NAV settings never needs a rebuild.
"""

from __future__ import annotations

import bisect
import json
import zlib

import tse_gold as G

GRID_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tse_grid (
    symbol_id INTEGER NOT NULL,
    date      INTEGER NOT NULL,
    grid_sec  INTEGER NOT NULL,
    version   INTEGER NOT NULL,
    n         INTEGER DEFAULT 0,
    data      BLOB,
    PRIMARY KEY (symbol_id, date, grid_sec, version)
);
"""


def ensure_schema(db) -> None:
    G.ensure_schema(db)
    with db._conn() as conn:
        conn.executescript(_SCHEMA)


def _sec(t: int) -> int:
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _hms(s: int) -> int:
    return (s // 3600) * 10000 + (s // 60 % 60) * 100 + s % 60


def build_day(trades: list[list], book: list[list], grid_sec: int) -> list[list]:
    """[[time, mid, cum_vol, last, bids, asks]] on the grid; bids / asks = [[price, vol], ...] (best first).

    trades rows: [seq, time, price, volume, canceled]; book rows: [refID, time, level, bidP, bidV, bidC, askP, askV, askC].
    """
    tr = sorted((r for r in trades if not r[4] and r[2] > 0 and r[3] > 0), key=lambda r: r[0])
    if not tr:
        return []
    bk = sorted(book, key=lambda r: (r[0], r[2]))
    t0, t1 = min(_sec(r[1]) for r in tr), max(_sec(r[1]) for r in tr)
    g = max(1, int(grid_sec))
    first = ((t0 + g - 1) // g) * g
    grid = list(range(first, t1 + 1, g))
    if not grid or grid[-1] != t1:
        grid.append(t1)                       # the day's last trade instant (so the window end is on the grid)
    out = []
    levels: dict[int, tuple] = {}
    bi = ti = 0
    cum = 0
    last = 0.0
    for s in grid:
        while bi < len(bk) and _sec(bk[bi][1]) <= s:
            r = bk[bi]
            if 1 <= r[2] <= 5:
                levels[r[2]] = (r[3], r[4], r[6], r[7])
            bi += 1
        while ti < len(tr) and _sec(tr[ti][1]) <= s:
            cum += tr[ti][3]
            last = float(tr[ti][2])
            ti += 1
        if cum <= 0:
            continue
        bids = sorted(([p, v] for p, v, _a, _b in levels.values() if p > 0 and v > 0), key=lambda x: -x[0])
        asks = sorted(([a, b] for _p, _v, a, b in levels.values() if a > 0 and b > 0), key=lambda x: x[0])
        if bids and asks and asks[0][0] > bids[0][0]:
            mid = (bids[0][0] + asks[0][0]) / 2.0
        else:
            mid = last
        out.append([_hms(s), round(mid, 4), cum, last, bids, asks])
    return out


def _pack(obj) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def _unpack(b):
    return json.loads(zlib.decompress(b).decode("utf-8")) if b else []


def grid_day(db, symbol_id: int, date: int, grid_sec: int) -> list[list] | None:
    """Cached grid for one day; None when the TSE data of that day was not collected (both kinds needed)."""
    with db._conn() as conn:
        r = conn.execute("SELECT data FROM tse_grid WHERE symbol_id=? AND date=? AND grid_sec=? AND version=?",
                         (symbol_id, date, grid_sec, GRID_VERSION)).fetchone()
    if r is not None:
        return _unpack(r[0])
    trades = G.load_day(db, symbol_id, date, "trades")
    book = G.load_day(db, symbol_id, date, "book")
    if trades is None or book is None:
        return None
    rows = build_day(trades, book, grid_sec)
    with db._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO tse_grid VALUES (?,?,?,?,?,?)",
                     (symbol_id, date, grid_sec, GRID_VERSION, len(rows), _pack(rows)))
    return rows


def tse_days(db, symbol_id: int, start: int | None = None, end: int | None = None) -> list[int]:
    """Days for which BOTH the trades and the book of this fund were collected."""
    G.ensure_schema(db)
    with db._conn() as conn:
        return [int(r[0]) for r in conn.execute(
            "SELECT date FROM tse_raw WHERE symbol_id=? AND date>=? AND date<=? GROUP BY date "
            "HAVING COUNT(DISTINCT kind)=2 ORDER BY date", (symbol_id, start or 0, end or 99999999))]


def tse_dates(db, symbol_ids: list[int], start: int | None = None, end: int | None = None) -> list[int]:
    """Days on which at least one of these funds has both kinds of TSE data."""
    G.ensure_schema(db)
    if not symbol_ids:
        return []
    q = ",".join("?" * len(symbol_ids))
    with db._conn() as conn:
        return [int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT date FROM (SELECT symbol_id, date FROM tse_raw WHERE symbol_id IN ({q}) AND date>=? AND "
            f"date<=? GROUP BY symbol_id, date HAVING COUNT(DISTINCT kind)=2) ORDER BY date",
            (*symbol_ids, start or 0, end or 99999999))]


def load_raw(db, symbol_id: int, start: int | None, end: int | None, grid_sec: int) -> tuple[list[tuple], dict]:
    """(rows shaped like Database.get_nav_intraday, book {(date, time): (bids, asks)}) for the TSE dataset."""
    ensure_schema(db)
    days = tse_days(db, symbol_id, start, end)
    if not days:
        return [], {}
    navs = db.get_nav_intraday(symbol_id, None, end)        # earlier days too: the NAV known at the open
    nkey = [(r[0], r[1]) for r in navs]
    rows, book = [], Book(db, symbol_id)
    for d in days:
        g = grid_day(db, symbol_id, d, grid_sec)
        if not g:
            continue
        for t, mid, cum, _last, bids, asks in g:
            j = bisect.bisect_right(nkey, (d, t)) - 1          # the dump's latest snapshot at or before (d, t)
            if j < 0:
                continue
            _dd, _tt, nav, nav_d, _px, _v, nav_t = navs[j]
            if not nav or nav <= 0 or mid <= 0 or not bids or not asks:
                continue                        # one-sided book: nothing could be bought AND sold here
            rows.append((d, t, nav, nav_d or 0, mid, cum, nav_t or 0))
            book[(d, t)] = (bids, asks)
    return rows, book


class Book(dict):
    """{(date, time): (bids, asks)} at the dataset's own instants, plus ``at(date, time)``: the real book at ANY
    instant (replayed from the collected event stream) — e.g. the instant a held fund is sold to fund a switch."""

    def __init__(self, db, symbol_id: int):
        super().__init__()
        self.db, self.sid = db, symbol_id
        self._day, self._ev = None, []

    def at(self, date: int, time: int) -> tuple[list, list]:
        if self._day != date:
            self._day = date
            self._ev = sorted(G.load_day(self.db, self.sid, date, "book") or [], key=lambda r: (r[0], r[2]))
        return _book_states(self._ev, [_sec(time)])[0]


def _book_states(book: list[list], secs: list[int]) -> list[tuple[list, list]]:
    """Top-5 book (bids, asks) as it stood at each instant of ``secs`` (ascending seconds of the day)."""
    bk = sorted(book, key=lambda r: (r[0], r[2]))
    levels: dict[int, tuple] = {}
    out, bi = [], 0
    for s in secs:
        while bi < len(bk) and _sec(bk[bi][1]) <= s:
            r = bk[bi]
            if 1 <= r[2] <= 5:
                levels[r[2]] = (r[3], r[4], r[6], r[7])
            bi += 1
        bids = sorted(([p, v] for p, v, _a, _b in levels.values() if p > 0 and v > 0), key=lambda x: -x[0])
        asks = sorted(([a, b] for _p, _v, a, b in levels.values() if a > 0 and b > 0), key=lambda x: x[0])
        out.append((bids, asks))
    return out


def load_hybrid(db, symbol_id: int, start: int | None, end: int | None) -> tuple[list[tuple], dict]:
    """Signal from the NAV dump, execution against the TSE book: the dump's own rows (on the days that have TSE
    data) and the real book as it stood at each dump snapshot instant. Snapshots with no two-sided book are dropped
    (nothing could be executed there)."""
    ensure_schema(db)
    days = set(tse_days(db, symbol_id, start, end))
    if not days:
        return [], {}
    raw = [r for r in db.get_nav_intraday(symbol_id, start, end) if r[0] in days]
    by_day: dict[int, list] = {}
    for r in raw:
        by_day.setdefault(r[0], []).append(r)
    rows, book = [], Book(db, symbol_id)
    for d in sorted(by_day):
        bk = G.load_day(db, symbol_id, d, "book") or []
        rr = by_day[d]
        states = _book_states(bk, [_sec(r[1]) for r in rr])
        for r, (bids, asks) in zip(rr, states):
            if bids and asks:
                rows.append(r)
                book[(r[0], r[1])] = (bids, asks)
    return rows, book


# --------------------------------------------------------------------------- #
#  Filling orders against the book                                             #
# --------------------------------------------------------------------------- #

def fill(levels: list, units: float) -> tuple[float, float, float]:
    """Walk ``levels`` ([[price, vol], ...], best first) for ``units``.
    Returns (units filled within the visible depth, their average price, the worst price touched)."""
    left, cost, got, worst = float(units), 0.0, 0.0, 0.0
    for p, v in levels:
        if left <= 0:
            break
        q = min(left, float(v))
        cost += q * p
        got += q
        left -= q
        worst = p
    return got, (cost / got if got > 0 else 0.0), worst


# --------------------------------------------------------------------------- #
#  Clock check: are the NAV dump's times on the same clock as TSE's?           #
# --------------------------------------------------------------------------- #

CLOCK_OFFSETS_MIN = list(range(-240, 241, 30))


def _tape(db, symbol_id: int, date: int, cache: dict):
    """[(second, price)] of the day's real trades (ascending) and the window (first, last second); cached."""
    key = (symbol_id, date)
    if key not in cache:
        tr = G.load_day(db, symbol_id, date, "trades")
        tp = sorted((_sec(r[1]), float(r[2])) for r in (tr or []) if not r[4] and r[2] > 0 and r[3] > 0)
        cache[key] = (tp, (tp[0][0], tp[-1][0]) if tp else None)
    return cache[key]


def clock_check(db, funds: list[tuple[int, str]], start: int | None, end: int | None, trades: list[dict],
                max_days: int = 12) -> dict:
    """How well the NAV dump's last price matches the TSE tape at the same instant — and at shifted instants, to
    expose a clock offset — plus how many dump snapshots / backtest trades fall outside the day's TSE trading window
    (first → last real trade). A trade outside the window was filled against a book that could not trade."""
    cache: dict = {}
    devs: dict[int, list[float]] = {o: [] for o in CLOCK_OFFSETS_MIN}
    rows_in = rows_out = 0
    for sid, _label in funds:
        days = tse_days(db, sid, start, end)
        if not days:
            continue
        pick = days if len(days) <= max_days else [days[int(i * len(days) / max_days)] for i in range(max_days)]
        want = set(pick)
        for r in db.get_nav_intraday(sid, min(pick), max(pick)):
            if r[0] not in want or not r[4] or r[4] <= 0:
                continue
            tp, win = _tape(db, sid, r[0], cache)
            if not tp:
                continue
            s = _sec(r[1])
            if win[0] <= s <= win[1]:
                rows_in += 1
            else:
                rows_out += 1
            secs = [x[0] for x in tp]
            for o in CLOCK_OFFSETS_MIN:
                j = bisect.bisect_right(secs, s + o * 60) - 1
                if j >= 0:
                    devs[o].append(abs(r[4] / tp[j][1] - 1.0) * 100)
    sid_of = {lab: sid for sid, lab in funds}
    t_out, t_n, ex = 0, 0, []
    for t in trades:
        sid = sid_of.get(t["symbol"])
        if sid is None:
            continue
        for d, tm, side in ((t["entry_date"], t["entry_time"], "خرید"), (t["exit_date"], t["exit_time"], "فروش")):
            _tp, win = _tape(db, sid, d, cache)
            if win is None:
                continue
            t_n += 1
            if not (win[0] <= _sec(tm) <= win[1]):
                t_out += 1
                if len(ex) < 8:
                    ex.append({"symbol": t["symbol"], "side": side, "date": d, "time": tm,
                               "window": [_hms(win[0]), _hms(win[1])]})

    def _med(v):
        v = sorted(v)
        return v[len(v) // 2] if v else None
    offs = [{"min": o, "median_dev_pct": round(_med(devs[o]), 4) if devs[o] else None, "n": len(devs[o])}
            for o in CLOCK_OFFSETS_MIN]
    ok = [x for x in offs if x["median_dev_pct"] is not None]
    best = min(ok, key=lambda x: x["median_dev_pct"]) if ok else None
    at0 = next((x for x in offs if x["min"] == 0), None)
    tot = rows_in + rows_out
    aligned = bool(best and at0 and at0["median_dev_pct"] is not None
                   and at0["median_dev_pct"] <= best["median_dev_pct"] * 1.25 + 0.01)
    return {"offsets": offs, "best_offset_min": best["min"] if best else None,
            "dev_at_0_pct": at0["median_dev_pct"] if at0 else None,
            "best_dev_pct": best["median_dev_pct"] if best else None,
            "aligned": aligned if best else None,
            "rows_checked": tot, "rows_outside_window_pct": round(rows_out / tot * 100, 1) if tot else None,
            "trade_legs_checked": t_n, "trade_legs_outside_window": t_out,
            "trade_legs_outside_pct": round(t_out / t_n * 100, 1) if t_n else None, "examples": ex}
