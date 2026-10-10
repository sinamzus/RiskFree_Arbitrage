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
    rows, book = [], {}
    for d in days:
        g = grid_day(db, symbol_id, d, grid_sec)
        if not g:
            continue
        for t, mid, cum, _last, bids, asks in g:
            j = bisect.bisect_right(nkey, (d, t)) - 1          # the dump's latest snapshot at or before (d, t)
            if j < 0:
                continue
            _dd, _tt, nav, nav_d, _px, _v, nav_t = navs[j]
            if not nav or nav <= 0 or mid <= 0:
                continue
            rows.append((d, t, nav, nav_d or 0, mid, cum, nav_t or 0))
            book[(d, t)] = (bids, asks)
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
