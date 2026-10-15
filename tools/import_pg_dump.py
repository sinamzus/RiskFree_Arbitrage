#!/usr/bin/env python3
"""Read the raw PostgreSQL dump of ``symbols_symbolintraday`` (folder with 00_schema.sql, 01_etf_symbols.sql.gz,
NN_intraday_*.sql.gz in ``COPY ... FROM stdin`` text format) WITHOUT PostgreSQL, and:

  1. store the fund list (``etf_symbols``: id, code_fa, ISIN, TSE code, type, group) in data/arbitrage.db;
  2. import every NAV message into ``nav_feed`` (same table the backtest reads, see nav_feed.py): the first report of
     each (nav, nav_time) of each fund — the moment it became known — keyed by the source's own ``symbol_id``;
  3. measure what matters for a backtest's NAV freshness, per message type and for the gold funds:
       * when the NAV value does NOT change, does ``nav_time`` still advance (heartbeat) — so an unchanged NAV can
         be told from a stale one?
       * age of the NAV during trading hours measured two ways: from ``nav_time`` (when the NAV was computed) and from
         the last VALUE change — the second is what a data set that only records changes would show;
       * delay report_time − nav_time; spacing of N2 updates; how dense the price snapshots are vs the old dump;
  4. write the report to analysis_results/pg_dump_report.{md,json} and (with --push) push it for analysis.

    python tools/import_pg_dump.py "C:\\...\\NAV data\\database" --push
    python tools/import_pg_dump.py "C:\\...\\NAV data\\database" --stats-only --push     # nothing written to the DB
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

TEHRAN = 3 * 3600 + 30 * 60
SAMPLE_CAP = 300_000
_ORD: dict = {}


def _ordinal(d: int) -> int:
    o = _ORD.get(d)
    if o is None:
        o = _ORD[d] = dt.date(d // 10000, d // 100 % 100, d % 100).toordinal()
    return o


def pts(s: str):
    """Postgres timestamptz text -> (YYYYMMDD, HHMMSS, epoch-like seconds) in Tehran time; None for \\N / empty."""
    if not s or s == "\\N":
        return None
    try:
        d = int(s[0:4]) * 10000 + int(s[5:7]) * 100 + int(s[8:10])
        h, m, se = int(s[11:13]), int(s[14:16]), int(s[17:19])
    except (ValueError, IndexError):
        return None
    off = TEHRAN
    tz = re.search(r"([+-])(\d{2}):?(\d{2})?$", s[19:])
    if tz:
        off = (int(tz.group(2)) * 3600 + int(tz.group(3) or 0) * 60) * (1 if tz.group(1) == "+" else -1)
    secs = _ordinal(d) * 86400 + h * 3600 + m * 60 + se
    if off != TEHRAN:                                  # convert to Tehran
        secs += TEHRAN - off
        day = dt.date.fromordinal(secs // 86400)
        r = secs % 86400
        return day.year * 10000 + day.month * 100 + day.day, (r // 3600) * 10000 + (r // 60 % 60) * 100 + r % 60, secs
    return d, h * 10000 + m * 100 + se, secs


def copy_blocks(path: Path):
    """Yield (table, columns, row-iterator) for every COPY block of a (gzipped) SQL dump."""
    op = gzip.open if path.name.lower().endswith(".gz") else open
    with op(path, "rt", encoding="utf-8", errors="replace", newline="\n") as f:
        for line in f:
            if line.startswith("COPY "):
                m = re.match(r"COPY\s+([\w.\"]+)\s*\(([^)]*)\)\s+FROM\s+stdin", line)
                if not m:
                    continue
                table = m.group(1).split(".")[-1].strip('"')
                cols = [c.strip().strip('"') for c in m.group(2).split(",")]

                def rows(f=f):
                    for ln in f:
                        if ln.startswith("\\."):
                            return
                        yield ln.rstrip("\n").split("\t")
                yield table, cols, rows()


def _q(v: list) -> dict | None:
    if not v:
        return None
    v = sorted(v)
    n = len(v)
    return {"n": n, "p10": v[n // 10], "p25": v[n // 4], "median": v[n // 2], "p75": v[int(n * .75)],
            "p90": v[int(n * .9)], "p99": v[int(n * .99)], "max": v[-1]}


class _Res:
    """Bounded sample (the first SAMPLE_CAP values, then every k-th) — enough for quantiles."""

    def __init__(self):
        self.v, self.n = [], 0

    def add(self, x):
        self.n += 1
        if len(self.v) < SAMPLE_CAP:
            self.v.append(x)
        elif self.n % 50 == 0:
            self.v[self.n // 50 % SAMPLE_CAP] = x


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--db", default=str(ROOT / "data" / "arbitrage.db"))
    ap.add_argument("--stats-only", action="store_true", help="measure only; write nothing to the database")
    ap.add_argument("--push", action="store_true", help="push the report to the analysis-results branch")
    ap.add_argument("--age-min", type=int, default=5, help="NAV age limit (minutes) used in the freshness comparison")
    a = ap.parse_args()
    folder = Path(a.folder)
    parts = sorted(folder.glob("*intraday*.sql*"))
    sym_file = next(iter(sorted(folder.glob("*etf_symbols*.sql*"))), None)
    if not parts:
        print("no *intraday*.sql(.gz) files in", folder)
        return 2
    t0 = time.time()

    # ---- funds ----
    funds: dict[int, dict] = {}
    if sym_file:
        for table, cols, rows in copy_blocks(sym_file):
            if table != "etf_symbols":
                continue
            for r in rows:
                d = dict(zip(cols, r))
                try:
                    funds[int(d["id"])] = {k: (None if v == "\\N" else v) for k, v in d.items()}
                except (KeyError, ValueError):
                    pass
    gold = {i for i, f in funds.items() if f.get("group_code") == "QS"}
    print(f"funds in the dump: {len(funds)} (gold group QS: {len(gold)})", flush=True)

    conn = None
    names_map: dict[int, str] = {}
    if not a.stats_only:
        import import_nav_feed as INF
        conn = sqlite3.connect(a.db, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(INF.SCHEMA)
        conn.execute("CREATE TABLE IF NOT EXISTS etf_symbols (id INTEGER PRIMARY KEY, code_fa TEXT, isin TEXT, "
                     "code_tse TEXT, name_fa TEXT, symbol_type TEXT, group_code TEXT, active INTEGER)")
        conn.executemany("INSERT OR REPLACE INTO etf_symbols VALUES (?,?,?,?,?,?,?,?)",
                         [(i, f.get("code_fa"), f.get("code_12"), f.get("code_tse"), f.get("name_fa"),
                           f.get("symbol_type"), f.get("group_code"), 1 if f.get("active") == "t" else 0)
                          for i, f in funds.items()])
        conn.execute("DROP TABLE IF EXISTS temp.stage")
        conn.execute("CREATE TEMP TABLE stage (symbol_id INTEGER, rk INTEGER, nav REAL, nav_date INTEGER, "
                     "nav_time INTEGER, event TEXT)")
        try:
            names_map = {int(i): s for i, s in conn.execute("SELECT symbol_id, symbol FROM nav_symbol_map")}
        except sqlite3.Error:
            names_map = {}
    old_rows: dict[int, int] = {}
    if conn is not None:
        try:
            old_rows = {int(s): int(n) for s, n in conn.execute(
                "SELECT symbol_id, COUNT(*) FROM nav_intraday GROUP BY symbol_id")}
        except sqlite3.Error:
            old_rows = {}

    # ---- stream every part ----
    st = {"rows": 0, "with_nav": 0, "bad": 0}
    by_event = collections.Counter()           # (event, outcome vs the fund's previous NAV row)
    events = collections.Counter()
    lag = collections.defaultdict(_Res)        # event -> report − nav_time (s)
    n2_gap = _Res()                            # successive nav_time of a gold fund's N2 messages, same day (s)
    chg_gap = _Res()                           # successive NAV VALUE changes of a gold fund, same day (s)
    age_cmp = collections.Counter()            # gold funds, trading hours: stale by nav_time / by last change
    age_nt, age_chg = _Res(), _Res()
    per_fund = collections.defaultdict(lambda: {"rows": 0, "nav_rows": 0, "first": None, "last": None,
                                                "days": set(), "events": collections.Counter()})
    last: dict[int, tuple] = {}                # sid -> (nav, nav_ts, report_ts, report_ts of last value change)
    last_key: dict[int, tuple] = {}
    buf = []
    staged = 0
    for part in parts:
        print(f"  {part.name}", flush=True)
        for table, cols, rows in copy_blocks(part):
            if table != "symbols_symbolintraday":
                continue
            ix = {c: i for i, c in enumerate(cols)}
            i_rep, i_nav, i_nt, i_sid, i_ev = ix["report_time"], ix["nav"], ix["nav_time"], ix["symbol_id"], ix["event"]
            for r in rows:
                st["rows"] += 1
                try:
                    sid = int(r[i_sid])
                except (ValueError, IndexError):
                    st["bad"] += 1
                    continue
                rep = pts(r[i_rep])
                ev = r[i_ev] if r[i_ev] != "\\N" else ""
                events[ev] += 1
                pf = per_fund[sid]
                pf["rows"] += 1
                pf["events"][ev] += 1
                if rep is not None:
                    if pf["first"] is None or rep[2] < pf["first"][2]:
                        pf["first"] = rep
                    if pf["last"] is None or rep[2] > pf["last"][2]:
                        pf["last"] = rep
                    pf["days"].add(rep[0])
                nv = r[i_nav]
                if nv == "\\N" or not nv or rep is None:
                    continue
                try:
                    nav = float(nv)
                except ValueError:
                    continue
                if nav <= 0:
                    continue
                st["with_nav"] += 1
                pf["nav_rows"] += 1
                nt = pts(r[i_nt])
                nts = nt[2] if nt else None
                if nts is not None:
                    lag[ev].add(rep[2] - nts)
                prev = last.get(sid)
                chg_at = rep[2]
                if prev is not None:
                    pnav, pnt, prep, pchg = prev
                    if nav != pnav:
                        by_event[(ev, "nav_changed")] += 1
                        if sid in gold and pchg is not None and rep[2] // 86400 == pchg // 86400:
                            chg_gap.add(rep[2] - pchg)
                    else:
                        chg_at = pchg
                        if nts is not None and pnt is not None:
                            by_event[(ev, "same_nav_newer_time" if nts > pnt else
                                      ("same_nav_older_time" if nts < pnt else "same_nav_same_time"))] += 1
                    if sid in gold and ev == "N2" and nts is not None and pnt is not None and nts > pnt \
                            and nts // 86400 == pnt // 86400:
                        n2_gap.add(nts - pnt)
                    best_nt = max(x for x in (nts, pnt) if x is not None) if (nts or pnt) else None
                else:
                    best_nt = nts
                last[sid] = (nav, best_nt, rep[2], chg_at)
                # freshness during trading hours (gold funds): what each kind of data would say
                if sid in gold and 90000 <= rep[1] <= 180000 and best_nt is not None:
                    lim = a.age_min * 60
                    a_nt, a_chg = rep[2] - best_nt, rep[2] - chg_at
                    age_nt.add(a_nt)
                    age_chg.add(a_chg)
                    age_cmp[("stale_by_nav_time" if a_nt > lim else "fresh_by_nav_time",
                             "stale_by_last_change" if a_chg > lim else "fresh_by_last_change")] += 1
                # nav_feed: drop a fund's consecutive repeats now, keep the first report of each (nav, nav_time)
                if conn is not None:
                    key = (nav, nt[0] if nt else 0, nt[1] if nt else 0)
                    if last_key.get(sid) != key:
                        last_key[sid] = key
                        buf.append((sid, rep[0] * 1000000 + rep[1], nav, key[1], key[2], ev))
                        if len(buf) >= 200_000:
                            conn.executemany("INSERT INTO stage VALUES (?,?,?,?,?,?)", buf)
                            staged += len(buf)
                            buf.clear()
                if st["rows"] % 1_000_000 == 0:
                    print(f"    {st['rows']:,} rows ({time.time() - t0:.0f}s)", flush=True)

    kept = 0
    if conn is not None:
        if buf:
            conn.executemany("INSERT INTO stage VALUES (?,?,?,?,?,?)", buf)
        sids = sorted({s for s in per_fund})
        if sids:
            conn.execute("DELETE FROM nav_feed WHERE symbol_id IN (%s)" % ",".join(map(str, sids)))
        conn.execute("INSERT INTO nav_feed SELECT symbol_id, rk / 1000000, rk % 1000000, nav, nav_date, nav_time, "
                     "event FROM (SELECT symbol_id, MIN(rk) AS rk, nav, nav_date, nav_time, event FROM stage "
                     "GROUP BY symbol_id, nav, nav_date, nav_time) ORDER BY symbol_id, rk")
        kept = conn.execute("SELECT COUNT(*) FROM nav_feed").fetchone()[0]
        conn.execute("DROP TABLE stage")
        conn.commit()

    # ---- report ----
    def label(sid):
        f = funds.get(sid) or {}
        return f.get("code_fa") or names_map.get(sid) or str(sid)

    gold_rows = []
    for sid in sorted(gold, key=lambda s: -per_fund[s]["rows"] if s in per_fund else 0):
        pf = per_fund.get(sid)
        if not pf:
            gold_rows.append({"id": sid, "fund": label(sid), "rows": 0})
            continue
        nd = len(pf["days"])
        gold_rows.append({"id": sid, "fund": label(sid), "rows": pf["rows"], "nav_rows": pf["nav_rows"],
                          "days": nd, "rows_per_day": round(pf["rows"] / nd, 1) if nd else 0,
                          "old_dump_rows_per_day": (round(old_rows.get(sid, 0) / nd, 1) if nd and old_rows else None),
                          "first": pf["first"][:2] if pf["first"] else None,
                          "last": pf["last"][:2] if pf["last"] else None,
                          "events": dict(pf["events"].most_common(6)),
                          "name_in_app": names_map.get(sid)})
    mism = [{"id": i, "dump": f.get("code_fa"), "app": names_map[i]} for i, f in funds.items()
            if i in names_map and (f.get("code_fa") or "").replace("ي", "ی").replace("ك", "ک").strip()
            != (names_map[i] or "").replace("ي", "ی").replace("ك", "ک").strip()]
    tot_age = sum(age_cmp.values()) or 1
    rep = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "folder": str(folder), "parts": [p.name for p in parts],
        "rows": st["rows"], "rows_with_nav": st["with_nav"], "bad_rows": st["bad"], "seconds": round(time.time() - t0),
        "funds_in_dump": len(funds), "gold_funds": len(gold), "funds_with_rows": len(per_fund),
        "imported_nav_feed_rows": kept if conn is not None else None,
        "message_types": dict(events.most_common(20)),
        "nav_rows_vs_previous_row_of_the_fund": {f"{e}|{k}": n for (e, k), n in sorted(by_event.items())},
        "report_minus_nav_time_s": {e: _q(r.v) for e, r in lag.items() if r.v},
        "gold_n2_nav_time_spacing_s": _q(n2_gap.v),
        "gold_nav_value_change_spacing_s": _q(chg_gap.v),
        "gold_trading_hours_freshness": {
            "age_limit_min": a.age_min,
            "share_pct": {f"{x}&{y}": round(n / tot_age * 100, 2) for (x, y), n in sorted(age_cmp.items())},
            "age_by_nav_time_s": _q(age_nt.v), "age_by_last_value_change_s": _q(age_chg.v)},
        "ids_whose_name_differs_from_the_app": mism[:50],
        "gold": gold_rows,
    }
    js = json.dumps(rep, ensure_ascii=False, default=str, indent=1)
    L = [f"# PostgreSQL dump report — {rep['created']}",
         f"rows {rep['rows']:,} · with NAV {rep['rows_with_nav']:,} · funds {rep['funds_in_dump']} (gold {rep['gold_funds']})"
         f" · nav_feed rows {rep['imported_nav_feed_rows']} · {rep['seconds']} s", "",
         f"message types: {rep['message_types']}", "",
         "NAV rows vs the fund's previous NAV row (event|outcome):", "```",
         json.dumps(rep["nav_rows_vs_previous_row_of_the_fund"], ensure_ascii=False, indent=1), "```",
         "report − nav_time (s) per message type:", "```",
         json.dumps(rep["report_minus_nav_time_s"], indent=1), "```",
         f"gold: N2 nav_time spacing (s) {rep['gold_n2_nav_time_spacing_s']}",
         f"gold: NAV value change spacing (s) {rep['gold_nav_value_change_spacing_s']}", "",
         f"gold, 09:00–18:00, NAV age limit {a.age_min} min: {rep['gold_trading_hours_freshness']['share_pct']}",
         f"age by nav_time (s): {rep['gold_trading_hours_freshness']['age_by_nav_time_s']}",
         f"age by last value change (s): {rep['gold_trading_hours_freshness']['age_by_last_value_change_s']}", "",
         f"ids whose name differs from the app: {rep['ids_whose_name_differs_from_the_app']}", "",
         "| id | fund | app name | rows | rows/day | old dump rows/day | days | first | last | events |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for g in gold_rows:
        L.append(f"| {g['id']} | {g['fund']} | {g.get('name_in_app')} | {g.get('rows')} | {g.get('rows_per_day')} | "
                 f"{g.get('old_dump_rows_per_day')} | {g.get('days')} | {g.get('first')} | {g.get('last')} | {g.get('events')} |")
    md = "\n".join(L)
    out = ROOT / "analysis_results"
    out.mkdir(exist_ok=True)
    (out / "pg_dump_report.json").write_text(js, encoding="utf-8")
    (out / "pg_dump_report.md").write_text(md, encoding="utf-8")
    print(md[:3000])
    print(f"\nreport: {out / 'pg_dump_report.md'}")
    if a.push:
        import result_export as E
        r = E.push_files(ROOT, {"results/pg_dump_report.json": js.encode("utf-8"),
                                "results/pg_dump_report.md": md.encode("utf-8")},
                         message=f"pg dump report {rep['created']}")
        print("push:", "ok " + str(r.get("commit", ""))[:10] if r.get("ok") else r.get("message"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
