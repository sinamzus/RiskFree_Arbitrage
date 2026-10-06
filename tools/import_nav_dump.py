#!/usr/bin/env python3
"""Import the PostgreSQL ``symbols_symbolintraday`` COPY dump (intraday NAV).

Input
-----
``sample.copy.zst`` — a zstd-compressed ``COPY public.symbols_symbolintraday
(...) FROM stdin;`` text dump (tab separated, ``\\N`` = NULL, ends with ``\\.``).
``schema.sql`` next to it only documents the columns; it is not needed to run.

What it does
------------
Streams the dump (never loads it into memory) and keeps ONLY rows that carry a
NAV (``nav > 0``), writing them to the ``nav_intraday`` table of
``data/arbitrage.db`` with Tehran-local ``date`` (YYYYMMDD) / ``time`` (HHMMSS):

    nav_intraday(symbol_id, date, time, nav, nav_date, nav_time,
                 last, close, vol, value, cnt, ref_price)

The dump identifies funds by ``symbol_id`` only (the ``symbols_symbol`` table is
not part of it), so a second step ``--map`` matches every symbol_id to a ticker
by comparing each day's total traded volume + close price with
``daily_history`` in the same database, and stores the result in
``nav_symbol_map(symbol_id, symbol, matches)``.

Usage (from the project folder)
-------------------------------
    pip install zstandard
    python tools/import_nav_dump.py "C:\\path\\NAV data\\sample.copy.zst"
    python tools/import_nav_dump.py --map            # symbol_id -> ticker
    python tools/import_nav_dump.py --stats          # what is in the table
    python tools/import_nav_dump.py --import-map data/nav_unmatched.csv  # manual names

The import is resumable-by-rerun: it recreates ``nav_intraday`` each time.
"""

from __future__ import annotations

import argparse
import calendar
import io
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB = Path("data") / "arbitrage.db"
TEHRAN_OFFSET = 3 * 3600 + 30 * 60      # Iran: UTC+03:30, no DST since 2022

# Column order of CREATE TABLE in schema.sql — used if the dump has no
# "COPY ... (cols) FROM stdin;" header line.
DEFAULT_COLUMNS = [
    "id", "created", "modified", "last_trade_time", "report_time",
    "range_min", "range_max", "symbol_status", "last", "close", "first",
    "open", "high", "low", "trades_count", "trades_volume", "trades_value",
    "nav", "nav_time", "symbol_id", "event", "reference_price", "group_status",
]

_COPY_RE = re.compile(r"^COPY\s+\S+\s*\((.*?)\)\s+FROM\s+stdin;", re.I)
_TS_RE = re.compile(
    r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?"
    r"(?:([+-])(\d\d)(?::?(\d\d))?)?$"
)

_SCHEMA = """
DROP TABLE IF EXISTS nav_intraday;
CREATE TABLE nav_intraday (
    symbol_id INTEGER NOT NULL,
    date      INTEGER NOT NULL,   -- Tehran-local YYYYMMDD of the snapshot
    time      INTEGER NOT NULL,   -- Tehran-local HHMMSS of the snapshot
    nav       REAL    NOT NULL,
    nav_date  INTEGER DEFAULT 0,  -- YYYYMMDD the NAV refers to (nav_time)
    nav_time  INTEGER DEFAULT 0,  -- HHMMSS of nav_time
    last      REAL    DEFAULT 0,
    close     REAL    DEFAULT 0,
    vol       INTEGER DEFAULT 0,  -- cumulative day volume at this snapshot
    value     REAL    DEFAULT 0,
    cnt       INTEGER DEFAULT 0,
    ref_price REAL    DEFAULT 0
);
"""
_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_navi_sid_date ON nav_intraday(symbol_id, date, time);
"""
_CAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS nav_symbol_category (
    symbol_id INTEGER PRIMARY KEY,
    category  TEXT NOT NULL,          -- fi | equity | gold | other
    source    TEXT DEFAULT 'auto',    -- config | auto | manual
    score     REAL DEFAULT 0
);
"""
_MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS nav_symbol_map (
    symbol_id INTEGER PRIMARY KEY,
    symbol    TEXT    NOT NULL,
    matches   INTEGER DEFAULT 0
);
"""


def parse_ts(s: str):
    """'2026-05-24 09:15:30.12+00' -> (date_int, time_int) in Tehran time, or None."""
    if not s or s == "\\N":
        return None
    m = _TS_RE.match(s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
    off = 0
    if m.group(7):
        off = int(m.group(8)) * 3600 + int(m.group(9) or 0) * 60
        if m.group(7) == "-":
            off = -off
    epoch = calendar.timegm((y, mo, d, h, mi, se)) - off + TEHRAN_OFFSET
    t = time.gmtime(epoch)
    return (t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday,
            t.tm_hour * 10000 + t.tm_min * 100 + t.tm_sec)


def _f(v: str) -> float:
    return 0.0 if v == "\\N" or v == "" else float(v)


def _i(v: str) -> int:
    return 0 if v == "\\N" or v == "" else int(float(v))


def open_dump(path: Path):
    if path.suffix.lower() == ".zst":
        try:
            import zstandard
        except ImportError:
            sys.exit("zstandard is missing — run:  pip install zstandard")
        fh = open(path, "rb")
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        return io.TextIOWrapper(reader, encoding="utf-8", errors="replace",
                                newline="\n")
    return open(path, "r", encoding="utf-8", errors="replace", newline="\n")


def import_dump(path: Path, db_path: Path, batch: int = 50_000) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(_SCHEMA)

    cols = list(DEFAULT_COLUMNS)
    ix = {c: i for i, c in enumerate(cols)}
    in_copy = False
    total = kept = bad = 0
    rows: list[tuple] = []
    t0 = time.time()
    ins = ("INSERT INTO nav_intraday (symbol_id,date,time,nav,nav_date,nav_time,"
           "last,close,vol,value,cnt,ref_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")

    with open_dump(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not in_copy:
                m = _COPY_RE.match(line)
                if m:
                    cols = [c.strip().strip('"') for c in m.group(1).split(",")]
                    ix = {c: i for i, c in enumerate(cols)}
                    in_copy = True
                    print(f"COPY header found: {len(cols)} columns")
                    continue
                if not (line and line[0].isdigit()):
                    continue              # SQL preamble / comments
                in_copy = True            # headerless dump: assume schema order
            if line == "\\.":
                break
            p = line.split("\t")
            total += 1
            if len(p) < len(cols):
                bad += 1
                continue
            try:
                nav = _f(p[ix["nav"]])
                if nav <= 0:
                    continue
                ts = (parse_ts(p[ix["report_time"]])
                      or parse_ts(p[ix["last_trade_time"]])
                      or parse_ts(p[ix["created"]]))
                if ts is None:
                    bad += 1
                    continue
                nts = parse_ts(p[ix["nav_time"]]) or (0, 0)
                rows.append((
                    _i(p[ix["symbol_id"]]), ts[0], ts[1], nav, nts[0], nts[1],
                    _f(p[ix["last"]]), _f(p[ix["close"]]),
                    _i(p[ix["trades_volume"]]), _f(p[ix["trades_value"]]),
                    _i(p[ix["trades_count"]]), _f(p[ix["reference_price"]]),
                ))
                kept += 1
            except (ValueError, KeyError):
                bad += 1
                continue
            if len(rows) >= batch:
                conn.executemany(ins, rows)
                rows.clear()
            if total % 500_000 == 0:
                print(f"  read {total:,} rows · kept {kept:,} · "
                      f"{time.time() - t0:.0f}s", flush=True)

    if rows:
        conn.executemany(ins, rows)
    print("building index ...", flush=True)
    conn.executescript(_INDEXES)
    conn.commit()
    conn.close()
    print(f"done: read {total:,} rows, kept {kept:,} with NAV, "
          f"{bad:,} malformed, {time.time() - t0:.0f}s")


def map_symbols(db_path: Path, min_score: int = 6, margin: float = 1.5) -> None:
    """Match symbol_id -> ticker by comparing each day's end-of-day row with
    ``daily_history`` (volume + closing price), then report what is left.

    Scoring per (symbol_id, ticker) over all common dates:
      +3  day volume equals the dump's volume exactly
      +1  dump volume within 80-100% of the day volume AND last/close price
          within 0.3% of daily_history.close_price
    A pair is accepted if score >= min_score and beats the runner-up by
    ``margin``x.  Each ticker is assigned to a single symbol_id.
    """
    conn = sqlite3.connect(db_path, timeout=60)
    conn.executescript(_MAP_SCHEMA)
    conn.executescript("""
        DROP TABLE IF EXISTS temp._navi_day;
        CREATE TEMP TABLE _navi_day AS
        SELECT symbol_id, date, last, close, vol FROM (
            SELECT symbol_id, date, last, close,
                   MAX(vol) OVER (PARTITION BY symbol_id, date) AS vol,
                   ROW_NUMBER() OVER (PARTITION BY symbol_id, date
                                      ORDER BY time DESC) AS rn
            FROM nav_intraday)
        WHERE rn = 1;
        CREATE INDEX temp.ix_nd ON _navi_day(date);
    """)
    pairs = conn.execute("""
        SELECT n.symbol_id, d.symbol,
               SUM(CASE
                     WHEN d.volume > 0 AND d.volume = n.vol THEN 3
                     WHEN d.volume > 0 AND n.vol <= d.volume AND n.vol >= 0.8 * d.volume
                          AND d.close_price > 0
                          AND (ABS(n.last  / d.close_price - 1) < 0.003
                            OR ABS(n.close / d.close_price - 1) < 0.003) THEN 1
                     ELSE 0 END) AS score
        FROM _navi_day n JOIN daily_history d ON d.date = n.date
        GROUP BY n.symbol_id, d.symbol
        HAVING score > 0
    """).fetchall()

    by_sid: dict[int, list[tuple[int, str]]] = {}
    for sid, sym, sc in pairs:
        by_sid.setdefault(sid, []).append((sc, sym))
    cand: list[tuple[int, int, str]] = []          # (score, sid, symbol)
    for sid, lst in by_sid.items():
        lst.sort(reverse=True)
        top = lst[0]
        second = lst[1][0] if len(lst) > 1 else 0
        if top[0] >= min_score and top[0] >= margin * second:
            cand.append((top[0], sid, top[1]))
    cand.sort(reverse=True)                        # strongest claim wins a ticker
    taken: set[str] = set()
    best: dict[int, tuple[str, int]] = {}
    for sc, sid, sym in cand:
        if sym in taken:
            continue
        taken.add(sym)
        best[sid] = (sym, sc)

    # keep manual rows (matches = -1) that were imported with --import-map
    manual = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol_id, symbol FROM nav_symbol_map WHERE matches = -1")}
    conn.execute("DELETE FROM nav_symbol_map")
    conn.executemany("INSERT INTO nav_symbol_map VALUES (?,?,?)",
                     [(sid, s, c) for sid, (s, c) in best.items() if sid not in manual])
    conn.executemany("INSERT INTO nav_symbol_map VALUES (?,?,-1)", list(manual.items()))
    conn.commit()

    all_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol_id FROM nav_intraday")]
    mapped = set(best) | set(manual)
    print(f"mapped {len(mapped)} of {len(all_ids)} symbol_ids "
          f"({len(manual)} manual)")
    weak = [(sid, s, c) for sid, (s, c) in best.items() if c < 15]
    if weak:
        print("low-confidence (score<15): "
              + ", ".join(f"{s}[{c}]" for _, s, c in sorted(weak, key=lambda x: x[1])))
    try:
        from config import FIXED_INCOME_ETFS
        have = set(best[i][0] for i in best) | set(manual.values())
        missing = [f["symbol"] for f in FIXED_INCOME_ETFS if f["symbol"] not in have]
        print(f"config fixed-income funds NOT mapped ({len(missing)}): "
              + ", ".join(missing))
    except Exception:
        pass
    classify(conn)
    _report_unmatched(conn, [i for i in all_ids if i not in mapped], db_path)
    conn.close()


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((y - mb) ** 2 for y in b)
    if sa <= 0 or sb <= 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb) ** 0.5


def classify(conn, gold_corr: float = 0.97, fi_vol_pct: float = 0.2,
             min_days: int = 60) -> None:
    """Assign every symbol_id to fi / gold / equity (manual rows are kept).

    * fi     : mapped to a fund of config.FIXED_INCOME_ETFS, or the daily NAV
               return is almost flat (std < fi_vol_pct %).
    * gold   : the biggest group of funds whose daily NAV returns are almost
               identical (pairwise correlation >= gold_corr) — they all track
               the same underlying.
    * equity : everything else with a NAV.
    """
    import statistics
    conn.executescript(_CAT_SCHEMA + _MAP_SCHEMA)
    manual = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol_id, category FROM nav_symbol_category WHERE source='manual'")}
    names = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol_id, symbol FROM nav_symbol_map")}
    try:
        from config import FIXED_INCOME_ETFS
        fi_names = {f["symbol"] for f in FIXED_INCOME_ETFS}
    except Exception:
        fi_names = set()

    # last NAV of each (symbol_id, date)
    nav: dict[int, dict[int, float]] = {}
    for sid, d, v in conn.execute(
            "SELECT symbol_id, date, nav FROM nav_intraday ORDER BY symbol_id, date, time"):
        nav.setdefault(sid, {})[d] = v
    dates = sorted({d for m in nav.values() for d in m})
    prev = {d: dates[i - 1] for i, d in enumerate(dates) if i}
    rets: dict[int, dict[int, float]] = {}
    for sid, m in nav.items():
        rets[sid] = {d: m[d] / m[prev[d]] - 1 for d in m
                     if d in prev and prev[d] in m and m[prev[d]] > 0}

    cat: dict[int, tuple[str, str, float]] = {}
    pool: list[int] = []
    for sid, r in rets.items():
        vol = statistics.pstdev(r.values()) * 100 if len(r) > 5 else 0.0
        if names.get(sid) in fi_names:
            cat[sid] = ("fi", "config", vol)
        elif len(r) >= min_days and vol < fi_vol_pct:
            cat[sid] = ("fi", "auto", vol)
        elif len(r) >= min_days:
            pool.append(sid)
        else:
            cat[sid] = ("other", "auto", vol)     # too little history

    # --- gold: largest cluster of near-identical NAV return series -----------
    corr: dict[tuple[int, int], float] = {}
    nbrs: dict[int, set[int]] = {s: set() for s in pool}
    for i, a in enumerate(pool):
        ra = rets[a]
        for b in pool[i + 1:]:
            rb = rets[b]
            common = [d for d in ra if d in rb]
            if len(common) < min_days:
                continue
            c = _pearson([ra[d] for d in common], [rb[d] for d in common])
            corr[(a, b)] = c
            if c >= gold_corr:
                nbrs[a].add(b)
                nbrs[b].add(a)
    gold: set[int] = set()
    if pool:
        seed = max(pool, key=lambda s: len(nbrs[s]))
        if len(nbrs[seed]) >= 2:
            gold = {seed} | nbrs[seed]
    for sid in pool:
        vol = statistics.pstdev(rets[sid].values()) * 100
        cat[sid] = ("gold", "auto", vol) if sid in gold else ("equity", "auto", vol)

    for sid, c in manual.items():
        cat[sid] = (c, "manual", cat.get(sid, ("", "", 0))[2])
    conn.execute("DELETE FROM nav_symbol_category")
    conn.executemany("INSERT INTO nav_symbol_category VALUES (?,?,?,?)",
                     [(sid, c, src, round(v, 3)) for sid, (c, src, v) in cat.items()])
    conn.commit()

    counts: dict[str, int] = {}
    for c, _, _ in cat.values():
        counts[c] = counts.get(c, 0) + 1
    print("categories: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if gold:
        vols = sorted(cat[s][2] for s in gold)
        print(f"gold cluster: {len(gold)} funds, daily NAV vol "
              f"{vols[0]:.2f}-{vols[-1]:.2f}%  (corr>={gold_corr}); "
              + ", ".join(names.get(s, f"#{s}") for s in sorted(gold)[:20]))
    else:
        print("gold cluster: none found at corr>=%.2f — set categories by hand "
              "in data/nav_unmatched.csv and use --import-map" % gold_corr)


def _report_unmatched(conn, miss: list[int], db_path: Path) -> None:
    """Describe the unmatched symbol_ids so they can be identified by hand."""
    import csv
    import statistics
    if not miss:
        print("all symbol_ids mapped")
        return
    rows = []
    for sid in miss:
        navs = conn.execute(
            "SELECT date, nav, last FROM nav_intraday WHERE symbol_id=? "
            "ORDER BY date, time", (sid,)).fetchall()
        if not navs:
            continue
        per_day: dict[int, float] = {}
        for d, nav, _ in navs:
            per_day[d] = nav                      # last NAV of the day
        seq = [per_day[d] for d in sorted(per_day)]
        rets = [(b / a - 1) * 100 for a, b in zip(seq, seq[1:]) if a > 0]
        nav_vol = statistics.pstdev(rets) if len(rets) > 5 else 0.0
        prem = [abs(l / n - 1) * 100 for _, n, l in navs if l and n > 0]
        maxvol = conn.execute(
            "SELECT MAX(vol) FROM nav_intraday WHERE symbol_id=?", (sid,)).fetchone()[0]
        kind = ("fixed-income?" if nav_vol < 0.2 and len(rets) > 5 else "equity/other?")
        crow = conn.execute("SELECT category FROM nav_symbol_category WHERE symbol_id=?",
                            (sid,)).fetchone()
        rows.append({
            "symbol_id": sid, "days": len(per_day),
            "first": min(per_day), "last": max(per_day),
            "median_nav": round(statistics.median(seq), 0),
            "nav_daily_vol_pct": round(nav_vol, 3),
            "median_price_vs_nav_pct": round(statistics.median(prem), 3) if prem else 0,
            "max_day_volume": maxvol or 0, "guess": kind,
            "category": crow[0] if crow else "", "symbol": "",
        })
    rows.sort(key=lambda r: (-r["days"], r["symbol_id"]))
    out = db_path.parent / "nav_unmatched.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    fi = sum(1 for r in rows if r["guess"] == "fixed-income?")
    print(f"unmatched symbol_ids: {len(rows)}  "
          f"(guess: {fi} fixed-income-like, {len(rows) - fi} equity/other)")
    print("top 15 by days of data:")
    for r in rows[:15]:
        print(f"  sid={r['symbol_id']:>6} days={r['days']:>3} "
              f"nav≈{r['median_nav']:>9,.0f} navVol={r['nav_daily_vol_pct']:.3f}% "
              f"maxVol={r['max_day_volume']:>12,} {r['guess']}")
    print(f"full list written to: {out}")


def import_manual_map(db_path: Path, csv_path: Path) -> None:
    """Load hand-filled rows: ``symbol_id`` + ``symbol`` (ticker) and/or
    ``category`` (fi | equity | gold | other) from e.g. nav_unmatched.csv."""
    import csv
    conn = sqlite3.connect(db_path, timeout=60)
    conn.executescript(_MAP_SCHEMA + _CAT_SCHEMA)
    n_sym = n_cat = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("symbol_id") or "").strip()
            if not sid.isdigit():
                continue
            sym = (r.get("symbol") or "").strip()
            cat = (r.get("category") or "").strip().lower()
            if sym:
                conn.execute("INSERT OR REPLACE INTO nav_symbol_map VALUES (?,?,-1)",
                             (int(sid), sym))
                n_sym += 1
            if cat in ("fi", "equity", "gold", "other"):
                row = conn.execute("SELECT category, source FROM nav_symbol_category "
                                   "WHERE symbol_id=?", (int(sid),)).fetchone()
                # a pre-filled auto value is not an edit — only store real changes
                if row is None or row[1] == "manual" or row[0] != cat:
                    conn.execute("INSERT OR REPLACE INTO nav_symbol_category "
                                 "VALUES (?,?, 'manual', 0)", (int(sid), cat))
                    n_cat += 1
    conn.commit()
    conn.close()
    print(f"imported {n_sym} manual names, {n_cat} manual categories "
          f"(run --map to re-classify the rest)")


def stats(db_path: Path) -> None:
    conn = sqlite3.connect(db_path, timeout=60)
    r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT symbol_id), MIN(date), "
                     "MAX(date), COUNT(DISTINCT date) FROM nav_intraday").fetchone()
    print(f"rows={r[0]:,} symbols={r[1]} dates={r[4]} range={r[2]}..{r[3]}")
    print("sample rows:")
    for row in conn.execute("SELECT * FROM nav_intraday ORDER BY date, time LIMIT 5"):
        print("  ", row)
    print("snapshots/day (median-ish) per symbol (top 5):")
    for row in conn.execute(
        "SELECT symbol_id, COUNT(*)*1.0/COUNT(DISTINCT date) FROM nav_intraday "
        "GROUP BY symbol_id ORDER BY 2 DESC LIMIT 5"):
        print("  ", row)
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("dump", nargs="?", help="path to sample.copy.zst (or plain .copy)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="target SQLite db")
    ap.add_argument("--map", action="store_true", help="match symbol_id -> ticker")
    ap.add_argument("--stats", action="store_true", help="print table summary")
    ap.add_argument("--classify", action="store_true",
                    help="(re)assign fi / gold / equity categories")
    ap.add_argument("--import-map", metavar="CSV",
                    help="load hand-filled symbol_id,symbol rows (see nav_unmatched.csv)")
    a = ap.parse_args()
    db = Path(a.db)
    if a.dump:
        import_dump(Path(a.dump), db)
    if a.import_map:
        import_manual_map(db, Path(a.import_map))
    if a.map:
        map_symbols(db)
    if a.classify and not a.map:
        conn = sqlite3.connect(db, timeout=60)
        conn.executescript(_MAP_SCHEMA)
        classify(conn)
        conn.close()
    if a.stats:
        stats(db)
    if not (a.dump or a.map or a.stats or a.classify or a.import_map):
        ap.print_help()


if __name__ == "__main__":
    main()
