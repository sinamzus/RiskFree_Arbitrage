#!/usr/bin/env python3
"""Import the fine-grained ETF NAV export (etf_nav_all.csv / etf_nav_changes.csv) and check the old NAV dump with it.

The export comes from the same source table as the NAV dump (``symbols_symbolintraday``) but complete (no
sampling) and with ``report_time`` — the moment our system RECEIVED each message — next to ``nav_time`` (the time
the NAV refers to). That is exactly what an honest backtest needs: at instant T only NAVs reported at or before T
are known.

Steps (from the project folder):

    python tools/import_nav_feed.py --inspect "C:\\...\\NAV data\\etf_nav_all.csv"
        shows the columns found and a few parsed rows — nothing is written.
    python tools/import_nav_feed.py "C:\\...\\NAV data\\etf_nav_all.csv"
        imports into the table ``nav_feed`` of data/arbitrage.db: one row per NAV change of a fund (the first report
        of every (nav, nav_time); the repeats of MW snapshots are dropped).
    python tools/import_nav_feed.py --compare
        compares the old dump (nav_intraday) with the feed where they overlap: is each dump snapshot's NAV the one
        the feed had at that instant, an older one, or one the feed only reported LATER (= look-ahead in the dump)?

Times in the file are Tehran local (README); a value with an explicit +00 offset is converted to Tehran.
"""

from __future__ import annotations

import argparse
import bisect
import calendar
import collections
import csv
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path("data") / "arbitrage.db"
TEHRAN_OFFSET = 3 * 3600 + 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS nav_feed (
    symbol_id INTEGER NOT NULL,
    rdate     INTEGER NOT NULL,   -- Tehran YYYYMMDD the message was received (report_time)
    rtime     INTEGER NOT NULL,   -- Tehran HHMMSS of report_time
    nav       REAL    NOT NULL,
    nav_date  INTEGER DEFAULT 0,  -- YYYYMMDD the NAV refers to (nav_time)
    nav_time  INTEGER DEFAULT 0,  -- HHMMSS of nav_time
    event     TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_navfeed ON nav_feed(symbol_id, rdate, rtime);
"""

# column names accepted for each field (lower-case; the first found is used)
CANDIDATES = {
    "symbol_id": ("symbol_id", "symbolid", "sid", "id_symbol"),
    "symbol": ("symbol", "ticker", "name", "symbol_name", "نماد", "symbol_fa"),
    "report": ("report_time",),
    "nav": ("nav",),
    "nav_time": ("nav_time",),
    "event": ("event",),
}

_TS = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*"
                 r"(?:([+-])(\d{2}):?(\d{2})?)?\s*$")


def parse_ts(s: str):
    """'2026-09-23 09:15:30[.123][+03:30]' -> (YYYYMMDD, HHMMSS) Tehran time, or None (Jalali columns are ignored)."""
    if not s:
        return None
    m = _TS.match(s)
    if not m:
        return None
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    se = int(m.group(6) or 0)
    if m.group(7):
        off = int(m.group(8)) * 3600 + int(m.group(9) or 0) * 60
        if m.group(7) == "-":
            off = -off
        if off != TEHRAN_OFFSET:              # explicit non-Tehran offset: convert to Tehran
            epoch = calendar.timegm((y, mo, d, h, mi, se)) - off + TEHRAN_OFFSET
            t = time.gmtime(epoch)
            return t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday, t.tm_hour * 10000 + t.tm_min * 100 + t.tm_sec
    return y * 10000 + mo * 100 + d, h * 10000 + mi * 100 + se


def _norm(s: str) -> str:
    return (s or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").strip()


def _columns(header: list[str]) -> dict:
    low = [h.strip().lower() for h in header]
    out = {}
    for k, names in CANDIDATES.items():
        for n in names:
            if n in low:
                out[k] = low.index(n)
                break
    return out


def _secs(t: int) -> int:
    return (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _ord(d: int) -> int:
    import datetime as _dt
    return _dt.date(d // 10000, d // 100 % 100, d % 100).toordinal()


def _ts(d: int, t: int) -> int:
    return _ord(d) * 86400 + _secs(t)


def inspect(path: Path, n: int = 8) -> None:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        cols = _columns(header)
        print("columns in the file:", header)
        print("used:", {k: header[v] for k, v in cols.items()})
        missing = [k for k in ("report", "nav", "nav_time") if k not in cols]
        if missing or ("symbol_id" not in cols and "symbol" not in cols):
            print("MISSING required columns:", missing or ["symbol_id / symbol"])
        for i, row in enumerate(rd):
            if i >= n:
                break
            print(" ", {k: row[v] for k, v in cols.items()},
                  "->", parse_ts(row[cols["report"]]) if "report" in cols else None,
                  parse_ts(row[cols["nav_time"]]) if "nav_time" in cols else None)


def import_feed(path: Path, db_path: Path) -> None:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    names = {}
    try:
        names = {_norm(s): int(i) for i, s in conn.execute("SELECT symbol_id, symbol FROM nav_symbol_map")}
    except sqlite3.Error:
        pass
    known = {int(r[0]) for r in conn.execute("SELECT DISTINCT symbol_id FROM nav_intraday")} \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='nav_intraday'").fetchone() else set()
    by_sid: dict[int, list] = collections.defaultdict(list)
    total = bad = unmatched = 0
    unmatched_names = collections.Counter()
    t0 = time.time()
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        cols = _columns(header)
        if not {"report", "nav", "nav_time"} <= set(cols) or not ({"symbol_id", "symbol"} & set(cols)):
            sys.exit(f"required columns not found in {header} — run with --inspect")
        for row in rd:
            total += 1
            try:
                nav = float(row[cols["nav"]] or 0)
                rep = parse_ts(row[cols["report"]])
                nt = parse_ts(row[cols["nav_time"]]) or (0, 0)
            except (ValueError, IndexError):
                bad += 1
                continue
            if nav <= 0 or rep is None:
                bad += 1
                continue
            sid = None
            if "symbol_id" in cols and row[cols["symbol_id"]].strip():
                try:
                    sid = int(float(row[cols["symbol_id"]]))
                except ValueError:
                    sid = None
            if sid is None and "symbol" in cols:
                sid = names.get(_norm(row[cols["symbol"]]))
            if sid is None:
                unmatched += 1
                if "symbol" in cols:
                    unmatched_names[row[cols["symbol"]]] += 1
                continue
            ev = row[cols["event"]] if "event" in cols else ""
            by_sid[sid].append((rep[0], rep[1], nav, nt[0], nt[1], ev))
            if total % 500_000 == 0:
                print(f"  {total:,} rows read ({time.time() - t0:.0f}s)", flush=True)
    kept = 0
    conn.execute("DELETE FROM nav_feed WHERE symbol_id IN (%s)" % ",".join(str(s) for s in by_sid) if by_sid else
                 "SELECT 1")
    for sid, rows in by_sid.items():
        rows.sort(key=lambda r: (r[0], r[1]))
        out, prev = [], None
        for r in rows:
            key = (r[2], r[3], r[4])
            if key != prev:                   # first report of each (nav, nav_time): when it became known
                out.append((sid,) + r)
                prev = key
        conn.executemany("INSERT INTO nav_feed VALUES (?,?,?,?,?,?,?)", out)
        kept += len(out)
    conn.commit()
    print(f"read {total:,} rows ({bad:,} without NAV / unreadable), {unmatched:,} of unknown funds; "
          f"{len(by_sid)} funds, {kept:,} NAV changes stored in nav_feed ({time.time() - t0:.0f}s)")
    if known:
        print(f"funds also in the old dump: {len(set(by_sid) & known)} of {len(by_sid)}")
    if unmatched_names:
        print("unknown names (most frequent):", unmatched_names.most_common(15))


def compare(db_path: Path, max_examples: int = 8) -> None:
    """Old dump vs feed where they overlap. For every dump snapshot (fund, T): the feed's NAV known at T (latest
    change reported at or before T) vs the dump's NAV. A dump NAV the feed only reported AFTER T is look-ahead."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    sids = [int(r[0]) for r in conn.execute("SELECT DISTINCT symbol_id FROM nav_feed")]
    tot = collections.Counter()
    ahead_s, examples = [], []
    upd = []
    for sid in sids:
        feed = conn.execute("SELECT rdate, rtime, nav, nav_date, nav_time FROM nav_feed WHERE symbol_id=? "
                            "ORDER BY rdate, rtime", (sid,)).fetchall()
        if not feed:
            continue
        fts = [_ts(r[0], r[1]) for r in feed]
        for a, b, ra, rb in zip(fts, fts[1:], feed, feed[1:]):          # update spacing inside a day
            if ra[0] == rb[0] and 0 < b - a < 4 * 3600:
                upd.append(b - a)
        d0, d1 = feed[0][0], feed[-1][0]
        dump = conn.execute("SELECT date, time, nav, nav_date, nav_time FROM nav_intraday WHERE symbol_id=? AND "
                            "date>=? AND date<=? AND nav>0 ORDER BY date, time", (sid, d0, d1)).fetchall()
        for d, t, nav, nd, nt in dump:
            T = _ts(d, t)
            if T < fts[0]:
                continue
            j = bisect.bisect_right(fts, T) - 1
            cur = feed[j]
            tot["rows"] += 1
            if abs(cur[2] - nav) <= 1e-9 * max(1.0, nav):
                tot["same"] += 1
                continue
            later = next((k for k in range(j + 1, min(len(feed), j + 400)) if abs(feed[k][2] - nav) <= 1e-9 * nav), None)
            earlier = next((k for k in range(j - 1, max(-1, j - 400), -1) if abs(feed[k][2] - nav) <= 1e-9 * nav), None)
            if later is not None and (earlier is None or fts[later] - T < T - fts[earlier]):
                tot["ahead"] += 1                 # the dump shows a NAV the feed only received later
                ahead_s.append(fts[later] - T)
                if len(examples) < max_examples:
                    examples.append(f"fund {sid} {d} {t}: dump NAV {nav} first reported {feed[later][0]} "
                                    f"{feed[later][1]} ({fts[later] - T}s later)")
            elif earlier is not None:
                tot["older"] += 1                 # the dump is behind the feed (missed an update)
            else:
                tot["other"] += 1
    n = tot["rows"] or 1
    print(f"funds compared: {len(sids)}; dump snapshots in the overlap: {tot['rows']:,}")
    print(f"  same NAV as the feed at that instant: {tot['same'] / n * 100:.2f}%")
    print(f"  dump behind the feed (older NAV):      {tot['older'] / n * 100:.2f}%")
    print(f"  dump AHEAD of the feed (look-ahead):   {tot['ahead'] / n * 100:.2f}%"
          + (f" — median {sorted(ahead_s)[len(ahead_s) // 2]}s, max {max(ahead_s)}s" if ahead_s else ""))
    print(f"  NAV not found in the feed nearby:      {tot['other'] / n * 100:.2f}%")
    if upd:
        upd.sort()
        print(f"feed: time between NAV updates within a day — median {upd[len(upd) // 2]}s, "
              f"p90 {upd[int(len(upd) * 0.9)]}s")
    for e in examples:
        print("   ", e)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="etf_nav_all.csv or etf_nav_changes.csv")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--inspect", action="store_true", help="show the columns and a few parsed rows only")
    ap.add_argument("--compare", action="store_true", help="compare the old NAV dump with the imported feed")
    a = ap.parse_args()
    if a.inspect:
        inspect(Path(a.csv))
        return
    if a.csv:
        import_feed(Path(a.csv), Path(a.db))
    if a.compare:
        compare(Path(a.db))
    if not a.csv and not a.compare:
        ap.print_help()


if __name__ == "__main__":
    main()
