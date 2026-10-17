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
CREATE TABLE IF NOT EXISTS tse_window (
    date     INTEGER PRIMARY KEY,
    n_funds  INTEGER NOT NULL,
    first    INTEGER,
    last     INTEGER
);
CREATE TABLE IF NOT EXISTS tse_cache (
    name  TEXT PRIMARY KEY,
    ver   TEXT NOT NULL,
    blob  BLOB
);
CREATE TABLE IF NOT EXISTS tse_hybrid (
    symbol_id INTEGER NOT NULL,
    date      INTEGER NOT NULL,
    sig       TEXT    NOT NULL,
    data      BLOB,
    PRIMARY KEY (symbol_id, date)
);
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


_SCHEMA_DONE: set = set()


def ensure_schema(db) -> None:
    k = str(getattr(db, "path", id(db)))
    if k in _SCHEMA_DONE:                     # once per database and process (it is called in tight loops)
        return
    G.ensure_schema(db)
    with db._conn() as conn:
        conn.executescript(_SCHEMA)
        try:                                  # per-day fingerprint of the tapes a stored window was built from
            conn.execute("ALTER TABLE tse_window ADD COLUMN ver TEXT DEFAULT ''")
        except Exception:
            pass
    _SCHEMA_DONE.add(k)


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


_WINS: dict = {}            # db -> (tse_raw row count, {date: (first, last) | None})


def _all_windows(db) -> dict:
    """{date: (first, last)} for every collected day, read in bulk and kept per process (refreshed whenever the
    collected data changes); days whose cached window is out of date are rebuilt from the tapes."""
    k = _dbkey(db)
    ensure_schema(db)
    gv = _global_version(db)                  # any tape added OR replaced (a count alone misses a re-fetch)
    hit = _WINS.get(k)
    if hit is not None and hit[0] == gv:
        return hit[1]
    with db._conn() as conn:
        cur = {int(d): f"{int(c)}|{int(tot)}|{int(mx)}" for d, c, tot, mx in conn.execute(
            "SELECT date, COUNT(*), TOTAL(n), MAX(rowid) FROM tse_raw WHERE kind='trades' AND n>0 GROUP BY date")}
        have = {int(d): (v or "", lo, hi) for d, v, lo, hi in conn.execute("SELECT date, ver, first, last FROM tse_window")}
    out, rebuild = {}, []
    for d, v in cur.items():
        h = have.get(d)
        if h is not None and h[0] == v:
            out[d] = (h[1], h[2]) if h[1] is not None else None
        else:
            rebuild.append(d)
    for d in rebuild:
        out[d] = _build_window(db, d, cur[d])
    _WINS[k] = (gv, out)
    return out


def _build_window(db, date: int, ver: str = "") -> tuple[int, int] | None:
    with db._conn() as conn:
        sids = [int(r[0]) for r in conn.execute(
            "SELECT symbol_id FROM tse_raw WHERE date=? AND kind='trades' AND n>0", (date,))]
    lo = hi = None
    for sid in sids:
        for row in G.load_day(db, sid, date, "trades") or []:
            if row[4] or row[2] <= 0 or row[3] <= 0:
                continue
            x = _sec(row[1])
            lo = x if lo is None or x < lo else lo
            hi = x if hi is None or x > hi else hi
    with db._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO tse_window (date, n_funds, first, last, ver) VALUES (?,?,?,?,?)",
                     (date, len(sids), lo, hi, ver))
    return (lo, hi) if lo is not None else None


def market_window(db, date: int) -> tuple[int, int] | None:
    """(first, last) second of the day at which ANY collected fund traded on TSE — the market's continuous-trading
    window. Outside it nothing can be bought or sold, whatever the order book shows. Stored in ``tse_window``
    (rebuilt when more funds of that day are collected)."""
    return _all_windows(db).get(date)


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


_LOADS: dict = {}           # (db, kind, fund, start, end, grid, collected-version) -> (rows, book)
# where each load came from (diagnostics shown with the timing): memory / disk cache / built from the raw data
CACHE_STATS = {"mem": 0, "disk": 0, "build": 0, "disk_s": 0.0, "build_s": 0.0, "put_fail": 0}
_LOADS_MAX = 256


def _collected_version(db, symbol_id: int) -> tuple:
    """Changes whenever TSE or NAV-dump data of this fund is added or replaced (so a cached load is never stale).
    The dump part is a fingerprint of the rows' contents (times, NAVs, prices, volumes), not only their count and
    last date: a re-import that corrects times or NAVs in place must invalidate the cache too."""
    with db._conn() as conn:
        # index-only (see _global_version): rows, newest rowid of this fund, and that row's size
        r = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM tse_raw WHERE symbol_id=?",
                         (symbol_id,)).fetchone()
        rl = conn.execute("SELECT n FROM tse_raw WHERE rowid=?", (r[1],)).fetchone() if r[1] else None
        r = (r[0], f"{int(r[1])}:{int(rl[0]) if rl else 0}")
        try:                                  # the NAV dump's rows too (hybrid takes its snapshots from there)
            n = conn.execute("SELECT COUNT(*), COALESCE(MAX(date), 0), TOTAL(date), TOTAL(time), TOTAL(nav), "
                             "TOTAL(nav_date), TOTAL(nav_time), TOTAL(last), TOTAL(vol) FROM nav_intraday "
                             "WHERE symbol_id=?", (symbol_id,)).fetchone()
        except Exception:
            n = (0, 0, 0, 0, 0, 0, 0, 0, 0)
    return (int(r[0]), r[1], int(n[0]), int(n[1])) + tuple(repr(float(x)) for x in n[2:])


def _pc_get(db, name: str, ver: str):
    """Persistent cache (table ``tse_cache``): the stored object when its version matches, else None."""
    import pickle
    try:
        with db._conn() as conn:
            r = conn.execute("SELECT ver, blob FROM tse_cache WHERE name=?", (name,)).fetchone()
        if r is None or r[0] != ver:
            return None
        return pickle.loads(zlib.decompress(r[1]))
    except Exception:
        return None


def _pc_put(db, name: str, ver: str, obj) -> bool:
    import pickle
    try:
        blob = zlib.compress(pickle.dumps(obj, protocol=4), 3)
        with db._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO tse_cache VALUES (?,?,?)", (name, ver, blob))
        return True
    except Exception:
        return False


_GV: dict = {}              # db -> (monotonic time, version): the global version re-read at most every second


def _global_version(db) -> tuple:
    """Changes whenever any TSE data is added or replaced (the market windows depend on every fund).

    Read from the table's index only — row count, highest rowid (an INSERT OR REPLACE gets a new rowid) and the size
    of that newest row — never from ``fetched_at``: that column sits after the big data blob, and reading it walks
    every blob's overflow pages (the whole TSE store) — which made each call cost seconds on a real database. Memoised
    for one second, since it is asked for in tight loops."""
    import time as _t
    k = _dbkey(db)
    now = _t.monotonic()
    hit = _GV.get(k)
    if hit is not None and now - hit[0] < 1.0 and hit[2] == G.WRITES[0]:
        return hit[1]
    with db._conn() as conn:
        n, mx = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM tse_raw").fetchone()
        last = conn.execute("SELECT n FROM tse_raw WHERE rowid=?", (mx,)).fetchone() if mx else None
    v = (int(n), int(mx), int(last[0]) if last else 0)
    _GV[k] = (now, v, G.WRITES[0])
    return v


def _memo_load(db, kind: str, symbol_id: int, start, end, grid: int, fn):
    """The order book of a fund depends only on the dataset and the period, not on the strategy parameters: load
    it once and share it between all backtests / optimizer configurations — in memory within a process, and on
    disk (``tse_cache``) across restarts, so reopening the app does not prepare the data again."""
    cv = _collected_version(db, symbol_id)
    gv = _global_version(db)                  # other funds' tapes move the market windows this load is filtered by
    key = (str(getattr(db, "path", id(db))), kind, symbol_id, start, end, grid, cv, gv)
    hit = _LOADS.get(key)
    if hit is None:
        import time as _t
        t0 = _t.time()
        name = f"load|{kind}|{symbol_id}|{start}|{end}|{grid}"
        ver = f"{HYBRID_VERSION}|{GRID_VERSION}|{LOAD_VERSION}|{cv}|{gv}"
        got = _pc_get(db, name, ver)
        if got is not None:
            rows, items = got
            book = Book(db, symbol_id)
            book.hybrid = kind == "hybrid"
            dict.update(book, items)
            hit = (rows, book)
            CACHE_STATS["disk"] += 1
            CACHE_STATS["disk_s"] += _t.time() - t0
        else:
            hit = fn()
            if not _pc_put(db, name, ver, (hit[0], dict(hit[1]))):
                CACHE_STATS["put_fail"] += 1
            CACHE_STATS["build"] += 1
            CACHE_STATS["build_s"] += _t.time() - t0
        if len(_LOADS) >= _LOADS_MAX:
            _LOADS.pop(next(iter(_LOADS)))
        _LOADS[key] = hit
    else:
        CACHE_STATS["mem"] += 1
    rows, book = hit
    return list(rows), book


def load_raw(db, symbol_id: int, start: int | None, end: int | None, grid_sec: int) -> tuple[list[tuple], dict]:
    ensure_schema(db)
    return _memo_load(db, "tse", symbol_id, start, end, grid_sec,
                      lambda: _load_raw(db, symbol_id, start, end, grid_sec))


def load_hybrid(db, symbol_id: int, start: int | None, end: int | None) -> tuple[list[tuple], dict]:
    ensure_schema(db)
    return _memo_load(db, "hybrid", symbol_id, start, end, 0, lambda: _load_hybrid(db, symbol_id, start, end))


def _load_raw(db, symbol_id: int, start: int | None, end: int | None, grid_sec: int) -> tuple[list[tuple], dict]:
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


def add_instants(db, symbol_id: int, raw: list[tuple], book: "Book", instants: list[tuple[int, int]]):
    """(raw, book) with extra instants (date, HHMMSS) inserted between the grid's: the real book at that second
    (replayed from the event stream), its mid, the volume traded so far that day and the dump's NAV then (the feed
    overlay replaces it afterwards). Instants already on the grid, outside the collected days or with a one-sided
    book are skipped. ``raw`` is not modified (it may be a cached list)."""
    have = {(r[0], r[1]) for r in raw}
    days = set(r[0] for r in raw)
    want = sorted((d, t) for d, t in instants if d in days and (d, t) not in have)
    if not want:
        return raw, book
    navs = db.get_nav_intraday(symbol_id, None, None)
    nkey = [(r[0], r[1]) for r in navs]
    tape: dict[int, tuple] = {}
    add = []
    for d, t in want:
        if d not in tape:
            tr = sorted((_sec(r[1]), r[3]) for r in (G.load_day(db, symbol_id, d, "trades") or [])
                        if not r[4] and r[2] > 0 and r[3] > 0)
            cum, acc = [], 0
            for _s, v in tr:
                acc += v
                cum.append(acc)
            tape[d] = ([x[0] for x in tr], cum)
        bids, asks = book.at(d, t)
        if not bids or not asks or asks[0][0] <= bids[0][0]:
            continue
        j = bisect.bisect_right(nkey, (d, t)) - 1
        if j < 0:
            continue
        _dd, _tt, nav, nav_d, _px, _v, nav_t = navs[j]
        if not nav or nav <= 0:
            continue
        secs, cum = tape[d]
        k = bisect.bisect_right(secs, _sec(t)) - 1
        add.append((d, t, nav, nav_d or 0, (bids[0][0] + asks[0][0]) / 2.0, cum[k] if k >= 0 else 0, nav_t or 0))
        book[(d, t)] = (bids, asks)
    if not add:
        return raw, book
    return sorted(list(raw) + add, key=lambda r: (r[0], r[1])), book


class Book(dict):
    """{(date, time): (bids, asks)} at the dataset's own instants, plus ``at(date, time)``: the real book at ANY
    instant — e.g. the instant a held fund is sold to fund a switch. Such an instant is another fund's snapshot, so
    the hybrid dataset has the book of every fund precomputed at all of them (``tse_hybrid``); anything else is
    replayed from the collected event stream."""

    _DAYS_KEPT = 8                    # days kept in memory (the simulation walks forward in time)

    def __init__(self, db, symbol_id: int):
        super().__init__()
        self.db, self.sid = db, symbol_id
        self._ev: dict[int, list] = {}
        self._pre: dict[int, dict] = {}
        self._memo: dict[tuple[int, int], tuple[list, list]] = {}
        self.hybrid = False

    def at(self, date: int, time: int) -> tuple[list, list]:
        key = (date, time)
        st = dict.get(self, key) or self._memo.get(key)
        if st is not None:
            return st
        if self.hybrid:
            pre = self._pre.get(date)
            if pre is None:
                if len(self._pre) >= self._DAYS_KEPT:
                    self._pre.pop(next(iter(self._pre)))
                pre = self._pre[date] = _hybrid_day_states(self.db, self.sid, date) or {}
            st = pre.get(time)
            if st is not None:
                self._memo[key] = st          # (the optimizer replays the same switch instants run after run)
                return st
        ev = self._ev.get(date)
        if ev is None:
            if len(self._ev) >= self._DAYS_KEPT:
                self._ev.pop(next(iter(self._ev)))
            ev = self._ev[date] = sorted(G.load_day(self.db, self.sid, date, "book") or [],
                                         key=lambda r: (r[0], r[2]))
        st = self._memo[key] = _book_states(ev, [_sec(time)])[0]
        return st


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


# --------------------------------------------------------------------------- #
#  Hybrid dataset: book states precomputed once and stored (tse_hybrid)         #
# --------------------------------------------------------------------------- #

HYBRID_VERSION = 1
LOAD_VERSION = 2            # bump when the rows a load returns change meaning (2: ghost rows, crossed books dropped)
_UNION: dict = {}             # (db, date) -> sorted snapshot times (HHMMSS) of every collected fund that day
_TSE_SIDS: dict = {}          # db -> (collected fund ids, row count of tse_raw when read)


def _dbkey(db) -> str:
    return str(getattr(db, "path", id(db)))


def _tse_sids(db) -> list[int]:
    with db._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM tse_raw").fetchone()[0]
        hit = _TSE_SIDS.get(_dbkey(db))
        if hit is None or hit[1] != n:
            sids = [int(r[0]) for r in conn.execute("SELECT DISTINCT symbol_id FROM tse_raw")]
            hit = _TSE_SIDS[_dbkey(db)] = (sids, n)
    return hit[0]


def _union_times(db, dates: list[int]) -> dict[int, list[int]]:
    """Snapshot instants of all collected funds on each day (one query for the missing days)."""
    k = _dbkey(db)
    sids = _tse_sids(db)
    tag = len(sids)
    miss = [d for d in dates if (k, d, tag) not in _UNION]
    if miss and sids:
        q = ",".join("?" * len(sids))
        got: dict[int, set] = {d: set() for d in miss}
        with db._conn() as conn:
            for d, t in conn.execute(f"SELECT date, time FROM nav_intraday WHERE symbol_id IN ({q}) AND date>=? AND "
                                     f"date<=?", (*sids, min(miss), max(miss))):
                if d in got:
                    got[d].add(int(t))
        for d in miss:
            _UNION[(k, d, tag)] = sorted(got[d])
    return {d: _UNION.get((k, d, tag), []) for d in dates}


def _hybrid_sig(instants: list[int], win, book_fetched: str) -> str:
    h = zlib.crc32(",".join(map(str, instants)).encode())
    return f"{HYBRID_VERSION}|{len(instants)}|{h}|{win}|{book_fetched}"


def _hybrid_day_states(db, symbol_id: int, date: int) -> dict | None:
    """{time: (bids, asks)} of one fund on one day from the stored table (None when not built yet)."""
    with db._conn() as conn:
        r = conn.execute("SELECT data FROM tse_hybrid WHERE symbol_id=? AND date=?", (symbol_id, date)).fetchone()
    if r is None:
        return None
    d = _unpack(r[0])
    return {t: (b, a) for t, b, a in d}


def _load_hybrid(db, symbol_id: int, start: int | None, end: int | None) -> tuple[list[tuple], dict]:
    """Signal from the NAV dump, execution against the TSE book: the dump's own rows (on the days that have TSE
    data, inside the market's trading window) and the real book as it stood at each dump snapshot instant.
    Snapshots with no two-sided book are dropped (nothing could be executed there).

    The book of the fund at EVERY collected fund's snapshot instant of the day is replayed once and stored in
    ``tse_hybrid`` (rebuilt only when the day's book, the market window or the set of instants changes), so later
    backtests — and every optimizer configuration — read small precomputed blobs instead of replaying events."""
    ensure_schema(db)
    days = tse_days(db, symbol_id, start, end)
    if not days:
        return [], Book(db, symbol_id)
    dset = set(days)
    raw = [r for r in db.get_nav_intraday(symbol_id, start, end) if r[0] in dset]
    by_day: dict[int, list] = {}
    for r in raw:
        by_day.setdefault(r[0], []).append(r)
    dl = sorted(by_day)
    unions = _union_times(db, dl)
    with db._conn() as conn:
        fetched = {int(d): f for d, f in conn.execute(
            "SELECT date, fetched_at FROM tse_raw WHERE symbol_id=? AND kind='book' AND date>=? AND date<=?",
            (symbol_id, dl[0] if dl else 0, dl[-1] if dl else 0))}
        stored = {int(d): (sig, data) for d, sig, data in conn.execute(
            "SELECT date, sig, data FROM tse_hybrid WHERE symbol_id=? AND date>=? AND date<=?",
            (symbol_id, dl[0] if dl else 0, dl[-1] if dl else 0))}
    rows, book = [], Book(db, symbol_id)
    book.hybrid = True
    new = []
    wins = _all_windows(db)                   # once, not per day
    for d in dl:
        win = wins.get(d)
        if win is None:
            continue
        # only snapshots taken while the market was trading (a snapshot after the close still shows a book, but
        # nothing could be executed against it)
        rr = [r for r in by_day[d] if win[0] <= _sec(r[1]) <= win[1]]
        if not rr:
            continue
        inst = sorted({t for t in unions.get(d, []) if win[0] <= _sec(t) <= win[1]} | {int(r[1]) for r in rr})
        sig = _hybrid_sig(inst, win, fetched.get(d, ""))
        st = stored.get(d)
        if st is not None and st[0] == sig:
            states = {t: (b, a) for t, b, a in _unpack(st[1])}
        else:
            bk = G.load_day(db, symbol_id, d, "book") or []
            ss = _book_states(bk, [_sec(t) for t in inst])
            states = {t: s2 for t, s2 in zip(inst, ss)}
            new.append((symbol_id, d, sig, _pack([[t, b, a] for t, (b, a) in states.items()])))
        if len(book._pre) < book._DAYS_KEPT:
            book._pre[d] = states
        for r in by_day[d]:
            bids, asks = states.get(int(r[1]), ([], [])) if win[0] <= _sec(r[1]) <= win[1] else ([], [])
            if bids and asks and bids[0][0] < asks[0][0]:
                rows.append(r)
                dict.__setitem__(book, (r[0], r[1]), (bids, asks))
            else:
                # not tradable (outside the window, one-sided or crossed book) — kept as a "ghost" row without price
                # or NAV, which the preparation skips, so its traded volume still counts: the next snapshot is
                # "fresh" only if something traded AFTER this one
                rows.append((r[0], r[1], 0.0, r[3], 0.0, r[5], r[6]))
    if new:
        with db._conn() as conn:
            conn.executemany("INSERT OR REPLACE INTO tse_hybrid VALUES (?,?,?,?)", new)
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
    (first → last real trade of any collected fund). A trade outside the window was filled against a book that
    could not trade (the hybrid dataset drops such snapshots, so it should report none)."""
    ensure_schema(db)
    cache: dict = {}
    sids = tuple(sorted(sid for sid, _l in funds))
    with db._conn() as conn:
        n_raw = conn.execute("SELECT COUNT(*) FROM tse_raw").fetchone()[0]
        try:
            # a fingerprint of the rows' contents: a re-import correcting times / prices in place must show up
            n_nav = tuple(repr(x) for x in conn.execute(
                f"SELECT COUNT(*), COALESCE(MAX(date), 0), TOTAL(date), TOTAL(time), TOTAL(nav), TOTAL(nav_time), "
                f"TOTAL(last) FROM nav_intraday WHERE symbol_id IN ({','.join('?' * len(sids))})", sids).fetchone()) \
                if sids else ()
        except Exception:
            n_nav = ()
    gv = _global_version(db)
    mkey = (_dbkey(db), sids, start, end, max_days, n_raw, n_nav, gv)
    hit = _CLOCK.get(mkey)
    if hit is None:
        name, ver = f"clock|{sids}|{start}|{end}|{max_days}", f"{n_raw}|{n_nav}|{gv}"
        hit = _pc_get(db, name, ver)
        if hit is None:
            devs, rows_in, rows_out = _clock_offsets(db, funds, start, end, max_days, cache)
            med = {}
            for o, v in devs.items():
                v = sorted(v)
                med[o] = (v[len(v) // 2] if v else None, len(v))
            hit = (med, rows_in, rows_out)
            _pc_put(db, name, ver, hit)
        _CLOCK[mkey] = hit
    devs, rows_in, rows_out = hit
    return _clock_result(db, funds, trades, devs, rows_in, rows_out, cache)


_CLOCK: dict = {}


def _clock_offsets(db, funds, start, end, max_days, cache):
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
            tp, _w = _tape(db, sid, r[0], cache)
            win = market_window(db, r[0])
            if not tp or win is None:
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
    return devs, rows_in, rows_out


def _clock_result(db, funds, trades, devs, rows_in, rows_out, cache) -> dict:
    sid_of = {lab: sid for sid, lab in funds}
    t_out, t_n, ex = 0, 0, []
    for t in trades:
        sid = sid_of.get(t["symbol"])
        if sid is None:
            continue
        for d, tm, side in ((t["entry_date"], t["entry_time"], "خرید"), (t["exit_date"], t["exit_time"], "فروش")):
            win = market_window(db, d)
            if win is None:
                continue
            t_n += 1
            if not (win[0] <= _sec(tm) <= win[1]):
                t_out += 1
                if len(ex) < 8:
                    ex.append({"symbol": t["symbol"], "side": side, "date": d, "time": tm,
                               "window": [_hms(win[0]), _hms(win[1])]})

    # devs: {offset: (median deviation %, n)} (reduced once, stored with the persistent cache)
    offs = [{"min": o, "median_dev_pct": round(devs[o][0], 4) if devs[o][0] is not None else None, "n": devs[o][1]}
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


# --------------------------------------------------------------------------- #
#  Sharing the prepared data with the optimizer's worker processes            #
# --------------------------------------------------------------------------- #

def export_loads(path: str) -> int:
    """Write every prepared fund history held in memory to ONE file (plain pickle, no compression): the optimizer's
    workers read it once at start instead of each querying / decompressing the disk cache fund by fund."""
    import pickle
    items = [(k, rows, dict(book), bool(getattr(book, "hybrid", False)), book.sid)
             for k, (rows, book) in _LOADS.items() if isinstance(book, Book)]
    with open(path, "wb") as f:
        pickle.dump(items, f, protocol=pickle.HIGHEST_PROTOCOL)
    return len(items)


def import_loads(path: str, db) -> int:
    """Counterpart of ``export_loads`` in a worker: fill the in-memory cache (same keys: same database file)."""
    import pickle
    import time as _t
    t0 = _t.time()
    with open(path, "rb") as f:
        items = pickle.load(f)
    for k, rows, d, hyb, sid in items:
        book = Book(db, sid)
        book.hybrid = hyb
        dict.update(book, d)
        _LOADS[k] = (rows, book)
    CACHE_STATS["shared"] = CACHE_STATS.get("shared", 0) + len(items)
    CACHE_STATS["shared_s"] = CACHE_STATS.get("shared_s", 0.0) + (_t.time() - t0)
    return len(items)
