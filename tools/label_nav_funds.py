#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Name EVERY fund of the intraday-NAV dump once and for all.

The dump only knows ``symbol_id``.  The real names live on TSETMC, so this tool

  1. discovers all tradeable funds on TSETMC (market-watch, plus a keyword sweep
     as a fallback) with their ticker and FULL name            -> nav_fund_meta
  2. downloads each fund's daily history (volume + close)      -> nav_ref_daily
     (stored in the database: re-running never downloads it twice)
  3. matches every symbol_id to a ticker by comparing, for every day, the
     end-of-day volume (exact) and closing price with that history
  4. derives the category (fi / gold / equity / other) from the full name and
     stores everything permanently (nav_symbol_map, nav_symbol_category)

Run on your own machine (TSETMC blocks datacenter IPs) from the project folder:

    python tools\\label_nav_funds.py            # everything
    python tools\\label_nav_funds.py --match    # only re-match (no network)
    python tools\\label_nav_funds.py --export   # write data\\nav_unmatched.csv

Afterwards open the fund list in the web UI; anything still unnamed is listed
with its closest candidates so it can be fixed by hand ONCE (manual labels are
never overwritten).
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from import_nav_dump import (  # noqa: E402
    DEFAULT_DB, _CAT_SCHEMA, _MAP_SCHEMA, _META_SCHEMA, _fa, classify,
    name_category,
)

FUND_NAME_HINTS = ("صندوق", "ص.س", "ص. س")

# Keywords for the fallback search sweep (TSETMC search returns ≤ ~50 hits each)
SWEEP_TERMS = [
    "صندوق", "طلا", "سکه", "عیار", "کهربا", "زر", "گوهر", "اهرم", "سهام", "بخشی",
    "شاخص", "ثابت", "درآمد", "پایدار", "اوراق", "نوع دوم", "مختلط", "زعفران",
    "نقره", "کالا", "املاک", "پالایش", "خودرو", "بانک", "فلزات", "نفت", "دارو",
    "ETF", "اطمینان", "یکم", "دوم", "سوم", "پارس", "آگاه", "آفرین", "آتی",
]


# --------------------------------------------------------------------------- #
#  1) discovery                                                                #
# --------------------------------------------------------------------------- #

def _looks_like_fund(name: str) -> bool:
    n = name or ""
    return any(h in n for h in FUND_NAME_HINTS)


def discover(fetcher, conn: sqlite3.Connection, min_funds: int = 150) -> int:
    """Fill nav_fund_meta with every tradeable fund (ticker + full name)."""
    found: dict[str, dict] = {}
    try:
        for r in fetcher.get_market_watch():
            if r.get("type") == "fund" or _looks_like_fund(r.get("name", "")):
                found[r["ins_code"]] = {"symbol": r["symbol"], "name": r.get("name", ""),
                                        "market": r.get("market", "")}
    except Exception as e:                                   # noqa: BLE001
        print(f"  market-watch failed: {e}")
    print(f"  market-watch: {len(found)} funds")

    if len(found) < min_funds:
        print(f"  fewer than {min_funds} funds — sweeping TSETMC search "
              f"({len(SWEEP_TERMS)} terms) ...")
        for i, term in enumerate(SWEEP_TERMS, 1):
            try:
                for r in fetcher.search_instrument(term):
                    if r["ins_code"] and _looks_like_fund(r.get("full_name", "")):
                        found.setdefault(r["ins_code"], {
                            "symbol": r["symbol"], "name": r.get("full_name", ""),
                            "market": ""})
            except Exception:                                # noqa: BLE001
                continue
            if i % 10 == 0:
                print(f"    {i}/{len(SWEEP_TERMS)} terms · {len(found)} funds")

    conn.executescript(_META_SCHEMA)
    for code, f in found.items():
        conn.execute(
            "INSERT INTO nav_fund_meta (ins_code, symbol, name, market) VALUES (?,?,?,?) "
            "ON CONFLICT(ins_code) DO UPDATE SET symbol=excluded.symbol, "
            "name=excluded.name, market=excluded.market",
            (code, f["symbol"], f["name"], f["market"]))
    conn.commit()
    return len(found)


# --------------------------------------------------------------------------- #
#  2) daily history                                                            #
# --------------------------------------------------------------------------- #

def fetch_history(fetcher, conn: sqlite3.Connection, days: int, workers: int = 5,
                  refresh: bool = False) -> int:
    """Download daily volume/close for every fund not downloaded yet."""
    codes = [r[0] for r in conn.execute("SELECT ins_code FROM nav_fund_meta")]
    done = {r[0] for r in conn.execute("SELECT DISTINCT ins_code FROM nav_ref_daily")}
    todo = codes if refresh else [c for c in codes if c not in done]
    print(f"  history: {len(todo)} to download, {len(codes) - len(todo)} already stored")
    if not todo:
        return 0

    def _job(code):
        try:
            return code, fetcher.get_historical_daily(code, days)
        except Exception:                                    # noqa: BLE001
            return code, []

    n_rows = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (code, rows) in enumerate(ex.map(_job, todo), 1):
            conn.executemany(
                "INSERT OR REPLACE INTO nav_ref_daily (ins_code,date,close,vol) VALUES (?,?,?,?)",
                [(code, r["date"], r["close_price"], r["volume"]) for r in rows
                 if r.get("date")])
            n_rows += len(rows)
            if i % 25 == 0 or i == len(todo):
                conn.commit()
                print(f"    {i}/{len(todo)} funds · {n_rows:,} rows · {time.time() - t0:.0f}s",
                      flush=True)
    conn.commit()
    return n_rows


# --------------------------------------------------------------------------- #
#  3) matching                                                                 #
# --------------------------------------------------------------------------- #

def match(conn: sqlite3.Connection, min_score: int = 6, margin: float = 1.5) -> dict:
    """symbol_id -> ins_code by end-of-day volume (exact) / near volume+price."""
    conn.executescript(_META_SCHEMA + _MAP_SCHEMA + _CAT_SCHEMA)
    conn.executescript("""
        DROP TABLE IF EXISTS temp._nd;
        CREATE TEMP TABLE _nd AS
        SELECT symbol_id, date, last, close, vol FROM (
            SELECT symbol_id, date, last, close,
                   MAX(vol) OVER (PARTITION BY symbol_id, date) AS vol,
                   ROW_NUMBER() OVER (PARTITION BY symbol_id, date ORDER BY time DESC) AS rn
            FROM nav_intraday) WHERE rn = 1 AND vol > 0;
        CREATE INDEX temp.ix_nd ON _nd(date, vol);
    """)
    # exact day volume: +3 each
    score: dict[tuple[int, str], int] = {}
    for sid, code, c in conn.execute("""
            SELECT n.symbol_id, r.ins_code, COUNT(*)
            FROM _nd n JOIN nav_ref_daily r ON r.date = n.date AND r.vol = n.vol
            GROUP BY n.symbol_id, r.ins_code"""):
        score[(sid, code)] = 3 * c
    # near volume (80-100%) AND price within 0.3%: +1 each (only for sids that
    # the exact join did not settle with a clear winner)
    settled = {}
    for (sid, code), sc in score.items():
        settled.setdefault(sid, []).append(sc)
    weak = [r[0] for r in conn.execute("SELECT DISTINCT symbol_id FROM _nd")
            if max(settled.get(r[0], [0])) < min_score]
    if weak:
        conn.execute("DROP TABLE IF EXISTS temp._weak")
        conn.execute("CREATE TEMP TABLE _weak(symbol_id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO _weak VALUES (?)", [(w,) for w in weak])
        for sid, code, c in conn.execute("""
                SELECT n.symbol_id, r.ins_code, COUNT(*)
                FROM _nd n JOIN _weak w ON w.symbol_id = n.symbol_id
                JOIN nav_ref_daily r ON r.date = n.date
                WHERE r.vol > 0 AND n.vol <= r.vol AND n.vol >= 0.8 * r.vol AND r.close > 0
                  AND (ABS(n.last / r.close - 1) < 0.003 OR ABS(n.close / r.close - 1) < 0.003)
                GROUP BY n.symbol_id, r.ins_code"""):
            score[(sid, code)] = score.get((sid, code), 0) + c

    by_sid: dict[int, list[tuple[int, str]]] = {}
    for (sid, code), sc in score.items():
        by_sid.setdefault(sid, []).append((sc, code))
    cands = []
    for sid, lst in by_sid.items():
        lst.sort(reverse=True)
        second = lst[1][0] if len(lst) > 1 else 0
        if lst[0][0] >= min_score and lst[0][0] >= margin * second:
            cands.append((lst[0][0], sid, lst[0][1]))
    cands.sort(reverse=True)
    taken: set[str] = set()
    best: dict[int, tuple[str, int]] = {}
    for sc, sid, code in cands:
        if code in taken:
            continue
        taken.add(code)
        best[sid] = (code, sc)

    meta = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT ins_code, symbol, name FROM nav_fund_meta")}
    manual = {r[0] for r in conn.execute(
        "SELECT symbol_id FROM nav_symbol_map WHERE matches = -1")}
    conn.execute("UPDATE nav_fund_meta SET symbol_id = NULL")
    for sid, (code, sc) in best.items():
        conn.execute("UPDATE nav_fund_meta SET symbol_id=? WHERE ins_code=?", (sid, code))
        if sid not in manual:
            conn.execute("INSERT OR REPLACE INTO nav_symbol_map VALUES (?,?,?)",
                         (sid, meta[code][0], sc))
    conn.commit()
    return {"matched": len(best), "scores": score, "best": best, "meta": meta}


# --------------------------------------------------------------------------- #
#  report                                                                      #
# --------------------------------------------------------------------------- #

def report(conn: sqlite3.Connection, res: dict, out_dir: Path) -> None:
    ids = [r[0] for r in conn.execute("SELECT DISTINCT symbol_id FROM nav_intraday")]
    named = {r[0] for r in conn.execute("SELECT symbol_id FROM nav_symbol_map")}
    left = [i for i in ids if i not in named]
    print(f"\nnamed {len(ids) - len(left)} of {len(ids)} symbol_ids; "
          f"{len(left)} still without a name")
    cats = {}
    for c, in conn.execute("SELECT category FROM nav_symbol_category"):
        cats[c] = cats.get(c, 0) + 1
    print("categories: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))

    rows = []
    for sid in left:
        d = conn.execute("SELECT COUNT(DISTINCT date), MIN(date), MAX(date), MAX(vol), AVG(nav) "
                         "FROM nav_intraday WHERE symbol_id=?", (sid,)).fetchone()
        cand = sorted(((sc, code) for (s2, code), sc in res["scores"].items() if s2 == sid),
                      reverse=True)[:3]
        hint = " | ".join(f"{res['meta'].get(code, ('?', ''))[0]}[{sc}]" for sc, code in cand)
        crow = conn.execute("SELECT category FROM nav_symbol_category WHERE symbol_id=?",
                            (sid,)).fetchone()
        rows.append({"symbol_id": sid, "days": d[0], "first": d[1], "last": d[2],
                     "max_day_volume": d[3] or 0, "avg_nav": round(d[4] or 0),
                     "category": crow[0] if crow else "", "closest_candidates": hint,
                     "symbol": ""})
    rows.sort(key=lambda r: -r["days"])
    if rows:
        out = out_dir / "nav_unmatched.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"unmatched list: {out}  (fill 'symbol'/'category' and load it with "
              f"python tools\\import_nav_dump.py --import-map {out})")
        for r in rows[:10]:
            print(f"  sid={r['symbol_id']:>6} days={r['days']:>3} avgNAV={r['avg_nav']:>9,} "
                  f"maxVol={r['max_day_volume']:>12,} cat={r['category']:<7} "
                  f"candidates: {r['closest_candidates'] or '—'}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more in the CSV")


# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--match", action="store_true", help="only re-match from stored history (no network)")
    ap.add_argument("--refresh", action="store_true", help="re-download every fund's history")
    ap.add_argument("--min-funds", type=int, default=150,
                    help="below this many market-watch funds, run the keyword sweep too")
    a = ap.parse_args()

    db = Path(a.db)
    if not db.exists():
        sys.exit(f"{db} not found — run from the project folder")
    conn = sqlite3.connect(db, timeout=60)
    conn.executescript(_META_SCHEMA + _MAP_SCHEMA + _CAT_SCHEMA)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='nav_intraday'").fetchone():
        sys.exit("nav_intraday missing — run tools/import_nav_dump.py first")

    if not a.match:
        from data_fetcher import TSETMCFetcher
        fetcher = TSETMCFetcher()
        first, last = conn.execute("SELECT MIN(date), MAX(date) FROM nav_intraday").fetchone()
        import datetime as dt
        span = (dt.date.today() - dt.date(first // 10000, first // 100 % 100, first % 100)).days + 30
        print("1/4 discovering funds on TSETMC ...")
        n = discover(fetcher, conn, a.min_funds)
        if n == 0:
            sys.exit("no funds found — TSETMC unreachable from this machine/IP?")
        print(f"2/4 downloading daily history (last {span} days) ...")
        fetch_history(fetcher, conn, span, refresh=a.refresh)

    print("3/4 matching symbol_ids to tickers ...")
    res = match(conn)
    print(f"  matched {res['matched']} symbol_ids")
    print("4/4 categories from full names ...")
    classify(conn)
    report(conn, res, db.parent)
    conn.close()


if __name__ == "__main__":
    main()
