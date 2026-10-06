#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""How many intraday-NAV data points does a fund have, and at which hours?

    python tools\\nav_coverage.py کهربا                    # last 7 days of the data
    python tools\\nav_coverage.py کهربا --days 14
    python tools\\nav_coverage.py کهربا --from 1405/05/10 --to 1405/05/17
    python tools\\nav_coverage.py "#2276" --times           # list every snapshot time
    python tools\\nav_coverage.py کهربا --db path\\to\\other.db

Prints, per day: number of snapshots, first/last time, how many have a fresh price
(volume grew since the previous snapshot) and the usable ones the backtest sees
(inside the session, positive price and NAV), then an hour histogram.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import DB_PATH  # noqa: E402


def parse_day(s: str) -> int:
    """'1405/05/17' (Jalali) or '20260808' / '2026-08-08' (Gregorian) -> YYYYMMDD int."""
    s = s.strip().replace("-", "/")
    if "/" in s:
        y, m, d = (int(x) for x in s.split("/"))
        if y < 1700:
            import jdatetime
            g = jdatetime.date(y, m, d).togregorian()
            return g.year * 10000 + g.month * 100 + g.day
        return y * 10000 + m * 100 + d
    return int(s)


def jal(d: int) -> str:
    try:
        import jdatetime
        j = jdatetime.date.fromgregorian(year=d // 10000, month=d // 100 % 100, day=d % 100)
        return f"{j.year}/{j.month:02d}/{j.day:02d}"
    except Exception:
        return str(d)


def hhmm(t: int) -> str:
    x = f"{int(t):06d}"
    return f"{x[:2]}:{x[2:4]}:{x[4:]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fund", help="ticker (e.g. کهربا) or #symbol_id")
    ap.add_argument("--days", type=int, default=7, help="window length in calendar days (default 7)")
    ap.add_argument("--from", dest="start", help="start day, Jalali 1405/05/10 or 20260801")
    ap.add_argument("--to", dest="end", help="end day (default: last day with data)")
    ap.add_argument("--times", action="store_true", help="list every snapshot time per day")
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    q = a.fund.strip()
    if q.startswith("#") or q.isdigit():
        ids = [(int(q.lstrip("#")), q)]
    else:
        ids = con.execute("SELECT symbol_id, symbol FROM nav_symbol_map WHERE symbol = ?", (q,)).fetchall()
        if not ids:
            ids = con.execute("SELECT symbol_id, symbol FROM nav_symbol_map WHERE symbol LIKE ?",
                              (f"%{q}%",)).fetchall()
    if not ids:
        print(f"صندوقی با نام «{q}» پیدا نشد. فهرست نام‌ها: SELECT symbol_id, symbol FROM nav_symbol_map;")
        return 1
    for sid, name in ids:
        last = con.execute("SELECT MAX(date) FROM nav_intraday WHERE symbol_id = ?", (sid,)).fetchone()[0]
        if not last:
            print(f"{name} (#{sid}): هیچ دادهٔ لحظه‌ای ندارد.")
            continue
        end = parse_day(a.end) if a.end else last
        if a.start:
            start = parse_day(a.start)
        else:
            e = dt.date(end // 10000, end // 100 % 100, end % 100)
            s = e - dt.timedelta(days=a.days - 1)
            start = s.year * 10000 + s.month * 100 + s.day
        rows = con.execute(
            "SELECT date, time, nav, nav_date, last, vol FROM nav_intraday "
            "WHERE symbol_id = ? AND date BETWEEN ? AND ? ORDER BY date, time", (sid, start, end)).fetchall()
        print(f"\n=== {name} (#{sid}) — {jal(start)} تا {jal(end)}  (آخرین روز داده: {jal(last)}) ===")
        if not rows:
            print("در این بازه هیچ نقطه‌ای نیست.")
            continue
        by_day = defaultdict(list)
        for r in rows:
            by_day[r[0]].append(r)
        hours = Counter()
        tot = 0
        print(f"{'تاریخ':<11}{'میلادی':<10}{'نقطه':>5}{'تازه':>6}{'قابل‌استفاده':>13}   اولین → آخرین")
        for d in sorted(by_day):
            rs = by_day[d]
            prev = 0
            fresh = usable = 0
            for _d, t, nav, nav_d, last_p, vol in rs:
                if (vol or 0) > prev:
                    fresh += 1
                prev = max(prev, vol or 0)
                if 90000 <= t <= 123000 and last_p and last_p > 0 and nav and nav > 0:
                    usable += 1
                hours[int(t) // 10000] += 1
            tot += len(rs)
            print(f"{jal(d):<11}{d:<10}{len(rs):>5}{fresh:>6}{usable:>13}   {hhmm(rs[0][1])} → {hhmm(rs[-1][1])}")
            if a.times:
                print("    " + "  ".join(hhmm(r[1])[:5] for r in rs))
        print(f"\nجمع: {tot} نقطه در {len(by_day)} روز (میانگین {tot / len(by_day):.1f} در روز)")
        print("توزیع ساعتی (نقطه در هر ساعت، جمع همهٔ روزها):")
        for h in sorted(hours):
            print(f"  {h:02d}:00–{h:02d}:59  {hours[h]:>4}  {'█' * min(60, hours[h] * 60 // max(hours.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
