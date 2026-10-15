#!/usr/bin/env python3
"""Survey every file of a data folder and write a compact report (structure, not the data itself).

    python tools/survey_data_folder.py "C:\\...\\NAV data"            # report in analysis_results/data_survey.*
    python tools/survey_data_folder.py "C:\\...\\NAV data" --push     # ... and push it to the analysis-results branch
    python tools/survey_data_folder.py "C:\\...\\NAV data" --max-rows 2000000   # faster: stats from the first rows

For every file: size, kind, and
  * CSV / TSV (plain or .gz / .zip): header, row count, per column the empty share, distinct values (capped), min /
    max, a few sample values, plus 5 sample rows;
  * NAV-like files (a fund column + nav + a time column): per fund rows / first / last time, message types, and the
    question that matters for the backtest — when the NAV did NOT change, does its time stamp still move (so a
    fresh-but-unchanged NAV can be told from a stale one)? Reported as: share of consecutive rows with the same NAV
    and a newer time, gaps between successive time stamps, gaps between NAV value changes;
  * README / text / markdown: the text (first 20 kB); JSON: top-level structure; Excel: sheets, header, first rows
    (needs openpyxl); Parquet: schema and row count (needs pyarrow); SQLite: tables, columns, row counts.
Only structure, statistics and at most 5 sample rows per file leave the computer.
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import io
import json
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISTINCT_CAP = 5000
SAMPLE_ROWS = 5
csv.field_size_limit(10_000_000)

_TS = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?([eE][-+]?\d+)?\s*$")


def _ts(s: str):
    """'YYYY-MM-DD HH:MM[:SS]...' -> seconds (naive, for differences only) or None."""
    m = _TS.match(s or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    se = int(m.group(6) or 0)
    try:
        import datetime as dt
        return dt.date(y, mo, d).toordinal() * 86400 + h * 3600 + mi * 60 + se
    except ValueError:
        return None


def _open_text(path: Path):
    """[(name, text stream)] for a plain / gzip / zip file."""
    low = path.name.lower()
    if low.endswith(".gz"):
        return [(path.name, gzip.open(path, "rt", encoding="utf-8-sig", errors="replace", newline=""))]
    if low.endswith(".zip"):
        z = zipfile.ZipFile(path)
        return [(f"{path.name}:{n}", io.TextIOWrapper(z.open(n), encoding="utf-8-sig", errors="replace", newline=""))
                for n in z.namelist() if not n.endswith("/")]
    return [(path.name, open(path, encoding="utf-8-sig", errors="replace", newline=""))]


def _inner_name(path: Path) -> str:
    n = path.name.lower()
    return n[:-3] if n.endswith(".gz") else n


class _Col:
    def __init__(self, name):
        self.name, self.empty, self.num, self.ts = name, 0, 0, 0
        self.distinct: set = set()
        self.capped = False
        self.nr = self.tr = None          # (min, max) of the numeric / date-time values
        self.samples: list = []

    def add(self, v: str):
        if v is None or v == "":
            self.empty += 1
            return
        if not self.capped:
            self.distinct.add(v)
            if len(self.distinct) > DISTINCT_CAP:
                self.capped = True
        if len(self.samples) < 4 and v not in self.samples:
            self.samples.append(v[:60])
        if _NUM.match(v):
            self.num += 1
            x = float(v)
            self.nr = (x, x) if self.nr is None else (min(self.nr[0], x), max(self.nr[1], x))
        elif _TS.match(v):
            self.ts += 1
            x = v[:19]
            self.tr = (x, x) if self.tr is None else (min(self.tr[0], x), max(self.tr[1], x))

    def out(self, n: int) -> dict:
        kind = "number" if self.num and self.num >= 0.9 * (n - self.empty) else (
            "datetime" if self.ts and self.ts >= 0.9 * (n - self.empty) else "text")
        rng = self.nr if kind == "number" else (self.tr if kind == "datetime" else None)
        return {"column": self.name, "kind": kind, "empty_pct": round(self.empty / n * 100, 2) if n else 0,
                "distinct": (f">{DISTINCT_CAP}" if self.capped else len(self.distinct)),
                "min": rng[0] if rng else None, "max": rng[1] if rng else None, "samples": self.samples,
                **({"values": sorted(self.distinct)[:40]} if not self.capped and len(self.distinct) <= 40 else {})}


def _pick(cols_low: list[str], *names) -> int | None:
    for n in names:
        if n in cols_low:
            return cols_low.index(n)
    for n in names:                                   # loose: a column containing the name
        for i, c in enumerate(cols_low):
            if n in c:
                return i
    return None


class _NavStats:
    """Per fund: does the NAV time stamp move while the NAV value stays the same?"""

    def __init__(self, i_sym, i_nav, i_time, i_rep, i_ev):
        self.i_sym, self.i_nav, self.i_time, self.i_rep, self.i_ev = i_sym, i_nav, i_time, i_rep, i_ev
        self.last: dict = {}          # fund -> (nav, time s, report s, time of the last VALUE change)
        self.rows = collections.Counter()
        self.first: dict = {}
        self.lastrep: dict = {}
        self.events = collections.Counter()
        self.same_nav_newer_time = 0
        self.same_nav_same_time = 0
        self.nav_changed = 0
        self.time_back = 0
        self.pairs = 0
        self.gap_time: list = []      # successive nav_time gaps within a day (s), sampled
        self.gap_change: list = []    # gaps between NAV VALUE changes within a day (s), sampled
        self.lag: list = []           # report − nav_time (s), sampled
        self.unordered = 0
        self.by_event = collections.Counter()     # (message type, what happened vs the fund's previous row)

    def add(self, row: list):
        try:
            sym = row[self.i_sym]
            nav = float(row[self.i_nav]) if row[self.i_nav] else None
        except (ValueError, IndexError):
            return
        if nav is None:
            return
        t = _ts(row[self.i_time]) if self.i_time is not None else None
        r = _ts(row[self.i_rep]) if self.i_rep is not None else None
        if self.i_ev is not None:
            self.events[row[self.i_ev]] += 1
        self.rows[sym] += 1
        stamp = r if r is not None else t
        if stamp is not None:
            self.first.setdefault(sym, row[self.i_rep if self.i_rep is not None else self.i_time][:19])
            self.lastrep[sym] = row[self.i_rep if self.i_rep is not None else self.i_time][:19]
        if r is not None and t is not None and len(self.lag) < 200_000:
            self.lag.append(r - t)
        prev = self.last.get(sym)
        if prev is not None:
            pnav, pt, pr, pchg = prev
            if r is not None and pr is not None and r < pr:
                self.unordered += 1
            self.pairs += 1
            ev = row[self.i_ev] if self.i_ev is not None else ""
            if nav != pnav:
                self.by_event[(ev, "nav_changed")] += 1
            elif t is not None and pt is not None:
                self.by_event[(ev, "same_nav_newer_time" if t > pt else ("same_nav_older_time" if t < pt
                                                                           else "same_nav_same_time"))] += 1
            if nav == pnav:
                if t is not None and pt is not None and t > pt:
                    self.same_nav_newer_time += 1
                    if t // 86400 == pt // 86400 and len(self.gap_time) < 400_000:
                        self.gap_time.append(t - pt)
                elif t is not None and pt is not None and t < pt:
                    self.time_back += 1
                else:
                    self.same_nav_same_time += 1
                self.last[sym] = (nav, max(t or 0, pt or 0) or None, r, pchg)
            else:
                self.nav_changed += 1
                if t is not None and pt is not None and t // 86400 == pt // 86400 and len(self.gap_time) < 400_000:
                    self.gap_time.append(t - pt)
                if t is not None and pchg is not None and t // 86400 == pchg // 86400 and len(self.gap_change) < 400_000:
                    self.gap_change.append(t - pchg)
                self.last[sym] = (nav, t, r, t)
        else:
            self.last[sym] = (nav, t, r, t)

    @staticmethod
    def _q(v: list) -> dict | None:
        if not v:
            return None
        v = sorted(v)
        n = len(v)
        return {"n": n, "p10": v[n // 10], "median": v[n // 2], "p90": v[int(n * 0.9)], "p99": v[int(n * 0.99)],
                "max": v[-1]}

    def out(self) -> dict:
        p = self.pairs or 1
        top = self.rows.most_common()
        return {
            "funds": len(self.rows),
            "message_types": dict(self.events.most_common(20)),
            "consecutive_rows_of_a_fund": self.pairs,
            "same_nav_newer_time_pct": round(self.same_nav_newer_time / p * 100, 2),
            "same_nav_same_time_pct": round(self.same_nav_same_time / p * 100, 2),
            "nav_changed_pct": round(self.nav_changed / p * 100, 2),
            "time_went_back_pct": round(self.time_back / p * 100, 3),
            "by_message_type": {f"{e}|{k}": n for (e, k), n in sorted(self.by_event.items())},
            "report_out_of_order_pct": round(self.unordered / p * 100, 3),
            "gap_between_nav_times_s": self._q(self.gap_time),
            "gap_between_nav_value_changes_s": self._q(self.gap_change),
            "report_minus_nav_time_s": self._q(self.lag),
            "per_fund": [{"fund": s, "rows": n, "first": self.first.get(s), "last": self.lastrep.get(s)}
                         for s, n in top[:400]],
        }


def survey_table(name: str, stream, max_rows: int | None) -> dict:
    head = stream.readline()
    if not head:
        return {"file": name, "kind": "empty"}
    delim = "\t" if head.count("\t") > head.count(",") else (";" if head.count(";") > head.count(",") else ",")
    header = next(csv.reader([head], delimiter=delim))
    low = [h.strip().lower() for h in header]
    cols = [_Col(h) for h in header]
    i_sym = _pick(low, "symbol_id", "code_fa", "symbol", "isin", "ticker", "نماد")
    i_nav = _pick(low, "nav")
    i_time = _pick(low, "nav_time", "nav_datetime", "navtime")
    i_rep = _pick(low, "report_time", "received_at", "created_at", "time", "timestamp")
    if i_rep == i_time:
        i_rep = None
    i_ev = _pick(low, "event", "type", "msg_type")
    navs = _NavStats(i_sym, i_nav, i_time, i_rep, i_ev) if (i_sym is not None and i_nav is not None
                                                            and low[i_nav] == "nav"
                                                            and (i_time is not None or i_rep is not None)) else None
    rd = csv.reader(stream, delimiter=delim)
    n = 0
    samples = []
    t0 = time.time()
    for row in rd:
        n += 1
        if len(samples) < SAMPLE_ROWS:
            samples.append(row)
        for c, v in zip(cols, row):
            c.add(v)
        if navs is not None:
            navs.add(row)
        if n % 2_000_000 == 0:
            print(f"    {name}: {n:,} rows ({time.time() - t0:.0f}s)", flush=True)
        if max_rows and n >= max_rows:
            break
    truncated = bool(max_rows and n >= max_rows)
    if truncated:                                     # finish counting the rows without statistics
        for _row in rd:
            n += 1
    out = {"file": name, "kind": "table", "delimiter": delim, "rows": n, "stats_from_rows": min(n, max_rows or n),
           "columns": [c.out(min(n, max_rows or n)) for c in cols], "sample_rows": samples,
           "seconds": round(time.time() - t0, 1)}
    if navs is not None:
        out["nav_analysis"] = {"fund_column": header[i_sym], "nav_column": header[i_nav],
                               "nav_time_column": header[i_time] if i_time is not None else None,
                               "report_column": header[i_rep] if i_rep is not None else None,
                               **navs.out()}
    return out


def survey_sqlite(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tabs = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info('{t}')")]
        try:
            n = con.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            sample = [list(map(str, r)) for r in con.execute(f"SELECT * FROM '{t}' LIMIT 3")]
        except sqlite3.Error as e:
            n, sample = str(e), []
        tabs.append({"table": t, "columns": cols, "rows": n, "sample_rows": [[x[:60] for x in r] for r in sample]})
    return {"file": path.name, "kind": "sqlite", "tables": tabs}


def survey_file(path: Path, max_rows: int | None) -> list[dict]:
    low = path.name.lower()
    inner = _inner_name(path)
    size = path.stat().st_size
    base = {"size_mb": round(size / 1e6, 2), "path": str(path)}
    try:
        if inner.endswith((".csv", ".tsv", ".txt")) and not inner.endswith(("readme.txt",)) and size > 0 \
                and not inner.startswith("readme"):
            out = []
            for name, st in _open_text(path):
                with st:
                    out.append({**base, **survey_table(name, st, max_rows)})
            return out
        if low.endswith(".zip"):
            out = []
            for name, st in _open_text(path):
                with st:
                    if name.lower().endswith((".csv", ".tsv")):
                        out.append({**base, **survey_table(name, st, max_rows)})
                    else:
                        out.append({**base, "file": name, "kind": "text", "text": st.read(20_000)})
            return out
        if inner.endswith((".md", ".txt", ".sql", ".log", ".ini", ".yaml", ".yml")):
            with _open_text(path)[0][1] as st:
                return [{**base, "file": path.name, "kind": "text", "text": st.read(20_000)}]
        if inner.endswith(".json"):
            with _open_text(path)[0][1] as st:
                txt = st.read(50_000_000)
            obj = json.loads(txt)

            def shape(o, d=0):
                if d > 3:
                    return type(o).__name__
                if isinstance(o, dict):
                    return {k: shape(v, d + 1) for k, v in list(o.items())[:30]}
                if isinstance(o, list):
                    return [len(o), shape(o[0], d + 1) if o else None]
                return type(o).__name__
            return [{**base, "file": path.name, "kind": "json", "structure": shape(obj)}]
        if low.endswith((".xlsx", ".xlsm")):
            try:
                import openpyxl
            except ImportError:
                return [{**base, "file": path.name, "kind": "excel", "error": "pip install openpyxl"}]
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheets = []
            for ws in wb.worksheets:
                rows = []
                for i, r in enumerate(ws.iter_rows(values_only=True)):
                    if i > SAMPLE_ROWS:
                        break
                    rows.append([str(x)[:60] if x is not None else "" for x in r])
                sheets.append({"sheet": ws.title, "max_row": ws.max_row, "max_column": ws.max_column, "first_rows": rows})
            return [{**base, "file": path.name, "kind": "excel", "sheets": sheets}]
        if low.endswith(".parquet"):
            try:
                import pyarrow.parquet as pq
            except ImportError:
                return [{**base, "file": path.name, "kind": "parquet", "error": "pip install pyarrow"}]
            f = pq.ParquetFile(path)
            return [{**base, "file": path.name, "kind": "parquet", "rows": f.metadata.num_rows,
                     "schema": [str(x) for x in f.schema_arrow]}]
        if low.endswith((".db", ".sqlite", ".sqlite3")):
            return [{**base, **survey_sqlite(path)}]
        with open(path, "rb") as fh:
            head = fh.read(400)
        return [{**base, "file": path.name, "kind": "other", "head_hex": head[:64].hex(),
                 "head_text": head.decode("utf-8", "replace")[:200]}]
    except Exception as e:                               # one bad file never stops the survey
        return [{**base, "file": path.name, "kind": "error", "error": f"{type(e).__name__}: {e}"}]


def to_markdown(rep: dict) -> str:
    L = [f"# Data folder survey — {rep['folder']}", f"{rep['created']} · {len(rep['files'])} files", ""]
    for f in rep["files"]:
        L.append(f"## {f.get('file')}  ({f.get('kind')}, {f.get('size_mb')} MB)")
        if f.get("error"):
            L.append(f"error: {f['error']}")
        if f["kind"] == "text":
            L += ["```", f.get("text", "")[:6000], "```"]
        if f["kind"] == "table":
            L.append(f"rows: {f['rows']:,} (stats from {f['stats_from_rows']:,}) · delimiter `{f['delimiter']}`")
            L.append("| column | kind | empty % | distinct | min | max | samples |")
            L.append("|---|---|---|---|---|---|---|")
            for c in f["columns"]:
                L.append(f"| {c['column']} | {c['kind']} | {c['empty_pct']} | {c['distinct']} | {c['min']} | {c['max']} | "
                         f"{', '.join(map(str, c['samples']))} |")
            na = f.get("nav_analysis")
            if na:
                L.append("")
                L.append(f"**NAV analysis** ({na['funds']} funds): same NAV + newer time {na['same_nav_newer_time_pct']}%, "
                         f"same NAV + same time {na['same_nav_same_time_pct']}%, NAV changed {na['nav_changed_pct']}%, "
                         f"time went back {na['time_went_back_pct']}%; messages {na['message_types']}")
                L.append(f"by message type: {na['by_message_type']}")
                L.append(f"gap between NAV times (s): {na['gap_between_nav_times_s']}")
                L.append(f"gap between NAV value changes (s): {na['gap_between_nav_value_changes_s']}")
                L.append(f"report − NAV time (s): {na['report_minus_nav_time_s']}")
        if f["kind"] == "sqlite":
            for t in f["tables"]:
                L.append(f"- {t['table']} ({t['rows']} rows): {', '.join(t['columns'])}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--max-rows", type=int, default=0, help="statistics from the first N rows of each table (0 = all)")
    ap.add_argument("--push", action="store_true", help="push the report to the analysis-results branch")
    a = ap.parse_args()
    folder = Path(a.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}")
        return 2
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    rep = {"folder": str(folder), "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": []}
    for p in files:
        print(f"  {p.relative_to(folder)} ({p.stat().st_size / 1e6:.1f} MB)", flush=True)
        rep["files"].extend(survey_file(p, a.max_rows or None))
    md = to_markdown(rep)
    js = json.dumps(rep, ensure_ascii=False, default=str, indent=1)
    out = ROOT / "analysis_results"
    out.mkdir(exist_ok=True)
    (out / "data_survey.json").write_text(js, encoding="utf-8")
    (out / "data_survey.md").write_text(md, encoding="utf-8")
    print(f"\nreport: {out / 'data_survey.md'}")
    if a.push:
        sys.path.insert(0, str(ROOT))
        import result_export as E
        r = E.push_files(ROOT, {"results/data_survey.json": js.encode("utf-8"),
                                "results/data_survey.md": md.encode("utf-8")},
                         message=f"data survey {rep['created']}")
        print("push:", "ok " + str(r.get("commit", ""))[:10] if r.get("ok") else r.get("message"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
