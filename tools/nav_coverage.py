#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""How many intraday-NAV data points does a fund have, and at which hours?

    python tools\\nav_coverage.py کهربا                    # last 7 days of the data
    python tools\\nav_coverage.py کهربا --days 14
    python tools\\nav_coverage.py کهربا --from 1405/05/10 --to 1405/05/17
    python tools\\nav_coverage.py "#2276" --times           # list every snapshot time
    python tools\\nav_coverage.py کهربا --db path\\to\\other.db

Prints, per day: number of snapshots, how many are pre-open (before the first volume
growth), how many have a fresh price (volume grew), the real trading window
(first -> last volume growth) and the first/last snapshot, then an hour histogram.
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


def hours_report(con, days: int, end_s) -> int:
    """Histogram (30-minute buckets) of ALL snapshots and of snapshots where the day's
    cumulative volume grew, over every fund.  Trading happens where volume grows: its
    first/last bucket is the real session, so open-time minus 09:00 is any time-zone shift."""
    last = con.execute("SELECT MAX(date) FROM nav_intraday").fetchone()[0]
    end = parse_day(end_s) if end_s else last
    e = dt.date(end // 10000, end // 100 % 100, end % 100)
    s0 = e - dt.timedelta(days=days - 1)
    start = s0.year * 10000 + s0.month * 100 + s0.day
    rows = con.execute("SELECT symbol_id, date, time, vol FROM nav_intraday "
                       "WHERE date BETWEEN ? AND ? ORDER BY symbol_id, date, time", (start, end)).fetchall()
    if not rows:
        print("دادهای در این بازه نیست.")
        return 1
    allc, grow = Counter(), Counter()
    first_grow, last_grow = [], []
    key, prev, fg, lg = None, 0, None, None
    for sid, d, t, vol in rows:
        k = (sid, d)
        if k != key:
            if fg is not None:
                first_grow.append(fg); last_grow.append(lg)
            key, prev, fg, lg = k, 0, None, None
        b = int(t) // 10000 * 60 + int(t) // 100 % 100 // 30 * 30
        allc[b] += 1
        v = vol or 0
        if v > prev:
            grow[b] += 1
            if fg is None:
                fg = int(t)
            lg = int(t)
        prev = max(prev, v)
    if fg is not None:
        first_grow.append(fg); last_grow.append(lg)
    print(f"\n{jal(start)} تا {jal(end)} — {len(rows)} نقطه، همهٔ صندوق‌ها (بازه‌های ۳۰ دقیقه‌ای)")
    print(f"{'ساعت':<8}{'همهٔ نقطه‌ها':>14}{'حجم رشد کرده':>14}")
    mx = max(grow.values()) if grow else 1
    for b in sorted(allc):
        print(f"{b // 60:02d}:{b % 60:02d}   {allc[b]:>12}{grow[b]:>14}  {'█' * (grow[b] * 40 // mx)}")
    if first_grow:
        fgs, lgs = sorted(first_grow), sorted(last_grow)
        pick = lambda xs, q: xs[min(len(xs) - 1, int(len(xs) * q))]
        print("\nاولین رشد حجم در هر (صندوق، روز): میانه", hhmm(pick(fgs, .5)), "· صدک ۱۰", hhmm(pick(fgs, .1)), "· صدک ۹۰", hhmm(pick(fgs, .9)))
        print("آخرین رشد حجم در هر (صندوق، روز): میانه", hhmm(pick(lgs, .5)), "· صدک ۱۰", hhmm(pick(lgs, .1)), "· صدک ۹۰", hhmm(pick(lgs, .9)))
    print("\nبازار معمولاً ~۰۹:۰۰ باز و ~۱۲:۳۰ بسته می‌شود؛ اگر ساعت‌های بالا جابه‌جاست، خطای ساعت در وارد کردن داده است.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fund", nargs="?", help="ticker (e.g. کهربا) or #symbol_id")
    ap.add_argument("--hours", action="store_true",
                    help="all funds: when does the traded volume really grow? (finds the true trading hours / time-zone shift)")
    ap.add_argument("--days", type=int, default=7, help="window length in calendar days (default 7)")
    ap.add_argument("--from", dest="start", help="start day, Jalali 1405/05/10 or 20260801")
    ap.add_argument("--to", dest="end", help="end day (default: last day with data)")
    ap.add_argument("--times", action="store_true", help="list every snapshot time per day")
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    if a.hours:
        return hours_report(con, a.days if a.days != 7 else 20, a.end)
    if not a.fund:
        ap.error("fund name is required (or use --hours)")
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
        print(f"{'تاریخ':<11}{'میلادی':<10}{'نقطه':>5}{'پیش‌گشایش':>11}{'تازه':>6}{'جلسهٔ واقعی (از رشد حجم)':>28}   اولین → آخرین نقطه")
        for d in sorted(by_day):
            rs = by_day[d]
            prev = 0
            fresh = pre = 0
            first_g = last_g = None
            for _d, t, nav, nav_d, last_p, vol in rs:
                if (vol or 0) > prev:
                    fresh += 1
                    if first_g is None:
                        first_g = t
                    last_g = t
                prev = max(prev, vol or 0)
                if first_g is None:
                    pre += 1                      # before the first volume growth = pre-open
                hours[int(t) // 10000] += 1
            usable = fresh
            tot += len(rs)
            win = f"{hhmm(first_g)[:5]} → {hhmm(last_g)[:5]}" if first_g is not None else "— (حجمی نبود)"
            print(f"{jal(d):<11}{d:<10}{len(rs):>5}{pre:>11}{fresh:>6}{win:>28}   {hhmm(rs[0][1])[:5]} → {hhmm(rs[-1][1])[:5]}")
            if a.times:
                print("    " + "  ".join(hhmm(r[1])[:5] for r in rs))
        print(f"\nجمع: {tot} نقطه در {len(by_day)} روز (میانگین {tot / len(by_day):.1f} در روز)")
        print("توزیع ساعتی (نقطه در هر ساعت، جمع همهٔ روزها):")
        for h in sorted(hours):
            print(f"  {h:02d}:00–{h:02d}:59  {hours[h]:>4}  {'█' * min(60, hours[h] * 60 // max(hours.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
