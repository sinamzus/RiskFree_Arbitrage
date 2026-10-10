"""Tick-level TSE data for the gold funds of the NAV dump: every trade and the full order-book event stream.

What is collected, per (fund, trading day of the NAV dump):
  * ``trades`` — ``Trade/GetTradeHistory/{insCode}/{date}/false``: every trade (seq, time, price, volume, canceled)
  * ``book``   — ``BestLimits/{insCode}/{date}``: the order-book DELTA stream (refID, time, level 1-5, bid/ask
                 price, volume, order count).  Replayed in refID order it gives the exact top-5 book at any instant.

Storage (lossless for those fields, compact): one zlib-compressed JSON blob per (fund, day, kind) in ``tse_raw``.
A stored row means "done" — also when the day was empty (n = 0) — so collection is resumable and never fetches a
day twice; failed requests are NOT stored and are retried on the next run.

Funds are identified by the NAV dump's ``symbol_id`` (the ticker comes from ``nav_symbol_map``); the TSE instrument
code of each ticker is discovered once (exact ticker + "صندوق" in the name) and kept in ``tse_ins`` — it can be
corrected by hand from the UI.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

logger = logging.getLogger(__name__)

KINDS = ("trades", "book")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tse_ins (
    symbol_id INTEGER PRIMARY KEY,
    symbol    TEXT NOT NULL,
    ins_code  TEXT DEFAULT '',
    tse_name  TEXT DEFAULT '',
    source    TEXT DEFAULT '',      -- auto | manual | notfound
    updated   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tse_raw (
    symbol_id  INTEGER NOT NULL,
    date       INTEGER NOT NULL,    -- YYYYMMDD (Gregorian, as in the NAV dump)
    kind       TEXT    NOT NULL,    -- trades | book
    ins_code   TEXT    DEFAULT '',
    n          INTEGER DEFAULT 0,   -- rows in the blob (0 = the day was fetched and is empty)
    data       BLOB,
    fetched_at TEXT    DEFAULT '',
    PRIMARY KEY (symbol_id, date, kind)
);
"""

# fields kept from the API rows (in this order)
_TRADE_FIELDS = ("nTran", "hEven", "pTran", "qTitTran", "canceled")
_BOOK_FIELDS = ("refID", "hEven", "number", "pMeDem", "qTitMeDem", "zOrdMeDem", "pMeOf", "qTitMeOf", "zOrdMeOf")


def ensure_schema(db) -> None:
    with db._conn() as conn:
        conn.executescript(_SCHEMA)


def pack(rows: list[list]) -> bytes:
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode("utf-8"), 6)


def unpack(blob: bytes | None) -> list[list]:
    if not blob:
        return []
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _row(item: dict, fields: tuple) -> list:
    out = []
    for f in fields:
        v = item.get(f, 0)
        if f == "canceled":
            v = 1 if v else 0
        out.append(v if v is not None else 0)
    return out


# --------------------------------------------------------------------------- #
#  Which funds / days                                                          #
# --------------------------------------------------------------------------- #

def gold_funds(db) -> list[dict]:
    """Gold funds of the NAV dump that have a ticker: [{symbol_id, symbol}] (duplicates of another id excluded)."""
    names = db.get_nav_symbol_map()
    cats = db.get_nav_symbol_category()
    dups = db.get_nav_dup_ids()
    return [{"symbol_id": sid, "symbol": names[sid]} for sid in sorted(cats)
            if cats[sid] == "gold" and sid in names and sid not in dups and names[sid].strip()]


def dump_dates(db, symbol_id: int, start: int | None = None, end: int | None = None) -> list[int]:
    """Days on which the NAV dump has rows for this fund (the days worth fetching)."""
    with db._conn() as conn:
        return [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT date FROM nav_intraday WHERE symbol_id=? AND date>=? AND date<=? ORDER BY date",
            (symbol_id, start or 0, end or 99999999))]


def done_set(db, symbol_id: int) -> set[tuple[int, str]]:
    with db._conn() as conn:
        return {(int(r[0]), r[1]) for r in conn.execute(
            "SELECT date, kind FROM tse_raw WHERE symbol_id=?", (symbol_id,))}


# --------------------------------------------------------------------------- #
#  Instrument codes                                                            #
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    try:
        from data_fetcher import _normalize
        return _normalize(s or "")
    except Exception:                                     # pragma: no cover
        return (s or "").replace("ي", "ی").replace("ك", "ک").strip()


def get_ins_map(db) -> dict[int, dict]:
    ensure_schema(db)
    with db._conn() as conn:
        return {int(r["symbol_id"]): dict(r) for r in conn.execute("SELECT * FROM tse_ins")}


def set_ins(db, symbol_id: int, symbol: str, ins_code: str, tse_name: str = "", source: str = "manual") -> None:
    ensure_schema(db)
    with db._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO tse_ins VALUES (?,?,?,?,?,?)",
                     (symbol_id, symbol, (ins_code or "").strip(), tse_name, source,
                      datetime.now().strftime("%Y-%m-%d %H:%M")))


def find_ins_code(fetcher, symbol: str) -> tuple[str, str]:
    """(ins_code, tse_name) for a fund ticker: exact ticker AND «صندوق» in the name; else exact ticker; else ('','')."""
    from data_fetcher import TSETMC_CDN
    from urllib.parse import quote
    data = fetcher._get(f"{TSETMC_CDN}/Instrument/GetInstrumentSearch/{quote(symbol)}", silent=True)
    items = (data or {}).get("instrumentSearch") or []
    want = _norm(symbol)
    exact = [i for i in items if _norm(i.get("lVal18AFC", "")) == want and i.get("insCode")]
    for i in exact:
        if "صندوق" in _norm(i.get("lVal30", "")):
            return str(i["insCode"]), i.get("lVal30", "")
    if exact:
        return str(exact[0]["insCode"]), exact[0].get("lVal30", "")
    return "", ""


def resolve_ins_codes(db, fetcher, funds: list[dict], refresh: bool = False) -> dict[int, str]:
    """Discover (once) the TSE instrument code of every fund; manual entries are never overwritten."""
    known = get_ins_map(db)
    out = {}
    for f in funds:
        k = known.get(f["symbol_id"])
        if k and k.get("ins_code") and (not refresh or k.get("source") == "manual"):
            out[f["symbol_id"]] = k["ins_code"]
            continue
        code, name = find_ins_code(fetcher, f["symbol"])
        set_ins(db, f["symbol_id"], f["symbol"], code, name, "auto" if code else "notfound")
        if code:
            out[f["symbol_id"]] = code
    return out


# --------------------------------------------------------------------------- #
#  Fetch + store one (fund, day, kind)                                         #
# --------------------------------------------------------------------------- #

def fetch_day(fetcher, ins_code: str, date: int, kind: str) -> list[list] | None:
    """Rows for one day, [] when the day is empty, None when the request failed (retry later)."""
    from data_fetcher import TSETMC_CDN
    if kind == "trades":
        data = fetcher._get(f"{TSETMC_CDN}/Trade/GetTradeHistory/{ins_code}/{date}/false", silent=True)
        if data is None:
            return None
        items = data.get("tradeHistory") if isinstance(data, dict) else None
        if items is None:
            return None
        rows = [_row(t, _TRADE_FIELDS) for t in items]
        rows.sort(key=lambda r: r[0])
        return rows
    if kind == "book":
        data = fetcher._get(f"{TSETMC_CDN}/BestLimits/{ins_code}/{date}", silent=True)
        if data is None:
            return None
        items = data.get("bestLimitsHistory") if isinstance(data, dict) else None
        if items is None:
            return None
        rows = [_row(b, _BOOK_FIELDS) for b in items]
        rows.sort(key=lambda r: (r[0], r[2]))
        return rows
    raise ValueError(kind)


def store_day(db, symbol_id: int, date: int, kind: str, ins_code: str, rows: list[list]) -> None:
    with db._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO tse_raw VALUES (?,?,?,?,?,?,?)",
                     (symbol_id, date, kind, ins_code, len(rows), pack(rows),
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def load_day(db, symbol_id: int, date: int, kind: str) -> list[list] | None:
    with db._conn() as conn:
        r = conn.execute("SELECT data FROM tse_raw WHERE symbol_id=? AND date=? AND kind=?",
                         (symbol_id, date, kind)).fetchone()
    return None if r is None else unpack(r[0])


# --------------------------------------------------------------------------- #
#  Orchestrator                                                                #
# --------------------------------------------------------------------------- #

def plan(db, symbol_ids: list[int] | None = None, start: int | None = None, end: int | None = None,
         kinds: tuple = KINDS) -> list[tuple[int, str, int, str]]:
    """[(symbol_id, symbol, date, kind)] still to fetch, most recent days first (the newest data is the most useful)."""
    ensure_schema(db)
    funds = [f for f in gold_funds(db) if not symbol_ids or f["symbol_id"] in symbol_ids]
    todo = []
    for f in funds:
        done = done_set(db, f["symbol_id"])
        for d in dump_dates(db, f["symbol_id"], start, end):
            for k in kinds:
                if (d, k) not in done:
                    todo.append((f["symbol_id"], f["symbol"], d, k))
    todo.sort(key=lambda x: (-x[2], x[0], x[3]))
    return todo


def collect(db, fetcher, symbol_ids: list[int] | None = None, start: int | None = None, end: int | None = None,
            kinds: tuple = KINDS, workers: int = 3, progress: dict | None = None,
            stop: threading.Event | None = None, lock: threading.Lock | None = None) -> dict:
    """Fetch every missing (fund, day, kind). Resumable; ``stop`` ends it cleanly between requests."""
    ensure_schema(db)
    lock = lock or threading.Lock()
    funds = [f for f in gold_funds(db) if not symbol_ids or f["symbol_id"] in symbol_ids]
    codes = resolve_ins_codes(db, fetcher, funds)
    todo = [t for t in plan(db, [f["symbol_id"] for f in funds], start, end, kinds) if t[0] in codes]
    missing_code = sorted({f["symbol"] for f in funds if f["symbol_id"] not in codes})
    stats = {"total": len(todo), "done": 0, "rows": 0, "empty": 0, "failed": 0, "current": "",
             "missing_ins_code": missing_code, "started": time.time(), "stopped": False}
    if progress is not None:
        with lock:
            progress.clear()
            progress.update(stats)

    def one(task):
        sid, sym, d, k = task
        if stop is not None and stop.is_set():
            return
        rows = fetch_day(fetcher, codes[sid], d, k)
        with lock:
            if rows is None:
                stats["failed"] += 1
            else:
                store_day(db, sid, d, k, codes[sid], rows)
                stats["rows"] += len(rows)
                stats["empty"] += 0 if rows else 1
            stats["done"] += 1
            stats["current"] = f"{sym} {d} {k}"
            if progress is not None:
                progress.update(stats)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, todo))
    stats["stopped"] = bool(stop is not None and stop.is_set())
    stats["seconds"] = round(time.time() - stats["started"], 1)
    if progress is not None:
        with lock:
            progress.update(stats)
    return stats


def coverage(db, start: int | None = None, end: int | None = None) -> list[dict]:
    """Per gold fund: TSE code, days in the NAV dump, days fetched per kind, rows, last day."""
    ensure_schema(db)
    ins = get_ins_map(db)
    with db._conn() as conn:
        agg = {}
        for r in conn.execute(
                "SELECT symbol_id, kind, COUNT(*) d, SUM(n) n, SUM(CASE WHEN n=0 THEN 1 ELSE 0 END) e, MAX(date) l, "
                "SUM(LENGTH(data)) b FROM tse_raw WHERE date>=? AND date<=? GROUP BY symbol_id, kind",
                (start or 0, end or 99999999)):
            agg[(int(r["symbol_id"]), r["kind"])] = dict(r)
    out = []
    for f in gold_funds(db):
        sid = f["symbol_id"]
        dd = dump_dates(db, sid, start, end)
        row = {"symbol_id": sid, "symbol": f["symbol"], "ins_code": (ins.get(sid) or {}).get("ins_code", ""),
               "tse_name": (ins.get(sid) or {}).get("tse_name", ""), "ins_source": (ins.get(sid) or {}).get("source", ""),
               "dump_days": len(dd), "first": dd[0] if dd else None, "last": dd[-1] if dd else None, "bytes": 0}
        for k in KINDS:
            a = agg.get((sid, k)) or {}
            row[k + "_days"] = a.get("d") or 0
            row[k + "_rows"] = a.get("n") or 0
            row[k + "_empty"] = a.get("e") or 0
            row["bytes"] += a.get("b") or 0
        out.append(row)
    return out
