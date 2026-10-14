"""NAV known at each instant, from the fine-grained NAV feed (table ``nav_feed``, see tools/import_nav_feed.py).

The NAV dump (``nav_intraday``) is a sampled copy of the source table: its snapshot can show a NAV our system only
received later (look-ahead) or miss an update. The feed has every NAV message with ``report_time`` — the moment it
was RECEIVED — so the NAV known at instant T is exactly the one reported at or before T. When several are known,
the one that refers to the latest moment (``nav_time``) wins: an MW snapshot re-sending an older NAV after a newer N2
update does not take the newer value back.

``overlay`` replaces the NAV, its date and time in a fund's dataset rows (any dataset: dump, TSE, hybrid) by the
feed's. Before the fund's first feed report no NAV is known: those rows become ghost rows (not tradable; counted in
the statistics). Ghost rows (no price / NAV) are left alone. Funds the feed does not have keep the dump's NAV.
"""

from __future__ import annotations

import bisect
import datetime as _dt

_FEED: dict = {}          # (db, fund) -> (version, report instants, best NAV per prefix)
_OVER: dict = {}          # (db, fund, dataset key, version) -> (rows, stats)
_OVER_MAX = 512


def _ord(d: int) -> int:
    return _dt.date(d // 10000, d // 100 % 100, d % 100).toordinal()


def _ts(d: int, t: int) -> int:
    return _ord(d) * 86400 + (t // 10000) * 3600 + (t // 100 % 100) * 60 + t % 100


def _real(db):
    return getattr(db, "_db", db)


def _dbkey(db) -> str:
    return str(getattr(_real(db), "path", id(_real(db))))


_VER: dict = {}           # (db, fund) -> (monotonic time, version): re-read at most every 2 seconds


def version(db, sid: int) -> tuple | None:
    """Fingerprint of the fund's feed rows (count, newest rowid, sums of the values — a re-import that corrects
    rows in place changes it too), None when the feed has nothing for it. Memoised for 2 seconds: it is asked for
    on every load, and the optimizer loads in tight loops."""
    import time as _t
    k = (_dbkey(db), sid)
    now = _t.monotonic()
    hit = _VER.get(k)
    if hit is not None and now - hit[0] < 2.0:
        return hit[1]
    try:
        with _real(db)._conn() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nav_feed'").fetchone():
                v = None
            else:
                r = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0), TOTAL(nav), TOTAL(rtime), "
                                 "TOTAL(nav_time) FROM nav_feed WHERE symbol_id=?", (sid,)).fetchone()
                v = (int(r[0]), int(r[1])) + tuple(repr(float(x)) for x in r[2:]) if r[0] else None
    except Exception:
        v = None
    _VER[k] = (now, v)
    return v


def _load(db, sid: int, ver: tuple):
    k = (_dbkey(db), sid)
    hit = _FEED.get(k)
    if hit is not None and hit[0] == ver:
        return hit
    with _real(db)._conn() as conn:
        rows = conn.execute("SELECT rdate, rtime, nav, nav_date, nav_time FROM nav_feed WHERE symbol_id=? "
                            "ORDER BY rdate, rtime", (sid,)).fetchall()
    rep, best = [], []
    cur, cur_key = None, None
    for rd, rt, nav, nd, nt in rows:
        if not nav or nav <= 0:
            continue
        key = _ts(nd, nt) if nd else -1          # the moment the NAV refers to (unknown = oldest)
        if cur is None or key >= cur_key:
            cur, cur_key = (float(nav), int(nd or 0), int(nt or 0)), key
        rep.append(_ts(rd, rt))
        best.append(cur)
    hit = _FEED[k] = (ver, rep, best)
    return hit


def known_at(db, sid: int, d: int, t: int):
    """(nav, nav_date, nav_time) the feed had for the fund at (d, t), or None."""
    ver = version(db, sid)
    if ver is None:
        return None
    _v, rep, best = _load(db, sid, ver)
    j = bisect.bisect_right(rep, _ts(d, t)) - 1
    return best[j] if j >= 0 else None


def overlay(db, sid: int, raw: list, key: tuple) -> tuple[list, dict]:
    """(rows with the feed's NAV, statistics). ``key`` identifies ``raw`` (dataset, grid, period, length…) so the
    result is reused across loads of the same data. Rows: (date, time, nav, nav_date, last, vol, nav_time)."""
    ver = version(db, sid)
    if ver is None:
        return raw, {"feed": False}
    mk = (_dbkey(db), sid, key, ver)
    hit = _OVER.get(mk)
    if hit is not None:
        return list(hit[0]), hit[1]
    _v, rep, best = _load(db, sid, ver)
    out, n_feed, n_before, n_diff = [], 0, 0, 0
    for r in raw:
        if not r[2] or r[2] <= 0:
            out.append(r)
            continue
        j = bisect.bisect_right(rep, _ts(r[0], r[1])) - 1
        if j < 0:
            # before the fund's first feed report no NAV is known (the dump's copy is exactly what the feed
            # replaces): a "ghost" row — not tradable, its volume still counts
            n_before += 1
            out.append((r[0], r[1], 0.0, r[3], 0.0, r[5], r[6]))
            continue
        nav, nd, nt = best[j]
        n_feed += 1
        if abs(nav - float(r[2])) > 1e-9 * nav:
            n_diff += 1
        out.append((r[0], r[1], nav, nd, r[4], r[5], nt))
    st = {"feed": True, "rows_feed": n_feed, "rows_before_feed": n_before, "rows_nav_changed": n_diff,
          "first_report": (_dt.date.fromordinal(rep[0] // 86400).strftime("%Y%m%d") if rep else None)}
    if len(_OVER) >= _OVER_MAX:
        _OVER.pop(next(iter(_OVER)))
    _OVER[mk] = (out, st)
    return list(out), st
