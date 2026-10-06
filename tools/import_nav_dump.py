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


def map_symbols(db_path: Path, min_matches: int = 3) -> None:
    """Match symbol_id -> ticker by (date, day volume, close) vs daily_history."""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.executescript(_MAP_SCHEMA)
    # one row per (symbol_id, date): the last snapshot of the day
    conn.executescript("""
        DROP TABLE IF EXISTS _navi_day;
        CREATE TEMP TABLE _navi_day AS
        SELECT symbol_id, date, MAX(vol) AS vol
        FROM nav_intraday GROUP BY symbol_id, date;
    """)
    rows = conn.execute("""
        SELECT n.symbol_id, d.symbol, COUNT(*) AS c
        FROM _navi_day n
        JOIN daily_history d ON d.date = n.date AND d.volume = n.vol AND d.volume > 0
        GROUP BY n.symbol_id, d.symbol
    """).fetchall()
    best: dict[int, tuple[str, int]] = {}
    for sid, sym, c in rows:
        if c >= min_matches and (sid not in best or c > best[sid][1]):
            best[sid] = (sym, c)
    conn.execute("DELETE FROM nav_symbol_map")
    conn.executemany("INSERT INTO nav_symbol_map VALUES (?,?,?)",
                     [(sid, s, c) for sid, (s, c) in best.items()])
    conn.commit()
    all_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol_id FROM nav_intraday")]
    conn.close()
    print(f"mapped {len(best)} of {len(all_ids)} symbol_ids to tickers")
    for sid, (s, c) in sorted(best.items(), key=lambda x: x[1][0]):
        print(f"  {sid:>8} -> {s}  ({c} matching days)")
    miss = [i for i in all_ids if i not in best]
    if miss:
        print(f"unmatched symbol_ids ({len(miss)}): {miss[:30]}"
              + (" ..." if len(miss) > 30 else ""))


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
    a = ap.parse_args()
    db = Path(a.db)
    if a.dump:
        import_dump(Path(a.dump), db)
    if a.map:
        map_symbols(db)
    if a.stats:
        stats(db)
    if not (a.dump or a.map or a.stats):
        ap.print_help()


if __name__ == "__main__":
    main()
